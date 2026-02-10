import asyncio
import socket
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from datetime import datetime, timedelta
import json
import os
import logging
from decimal import Decimal, ROUND_DOWN

log_file = 'trading_bot.log'
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, mode='a'),
        logging.StreamHandler()
    ]
)

API_KEY = "3v2KzK8lhtYblymiQiRd9aFxuRZXOuv3wdgZnVgPGTWSIw7WQUYxxrPlf9cYQ8ul"
SECRET_KEY = "aFXnMhVhhet45dBQyxVbJzVgJS5pSUsC8P7SvDvGS1Tn0WDkWMKQMD3PdZUOOitR"
client = Client(API_KEY, SECRET_KEY)
TRADE_SYMBOL = "BTCUSDC"

MACD_FAST = 5
MACD_SLOW = 10
MACD_SIGNAL = 3
AUTO_TRADE_INTERVAL = 3600

auto_trading_enabled = False
trade_history = []
TRADE_HISTORY_FILE = "trade_history.json"
last_buy_price = None
prev_histogram_value = None
symbol_filters = {}

def load_trade_history():
    global trade_history
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except json.JSONDecodeError:
            trade_history = []

def save_trade(trade_data):
    global trade_history
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error(f"Save error: {e}")

def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    for price in prices[1:]:
        ema_value = (price * alpha) + (ema[-1] * (1 - alpha))
        ema.append(ema_value)
    return ema

def get_macd_signal():
    global prev_histogram_value
    try:
        start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
        klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
        close_prices = [float(k[4]) for k in klines]

        if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
            return {"signal": None, "trend": "❌ Не визначено"}

        fast_ema = calculate_ema(close_prices, MACD_FAST)
        slow_ema = calculate_ema(close_prices, MACD_SLOW)
        macd = [fast_ema[i] - slow_ema[i] for i in range(min(len(fast_ema), len(slow_ema)))]
        signal = calculate_ema(macd, MACD_SIGNAL)
        histogram = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]

        current_hist = histogram[-1]
        trend = "🟢 Позитивний" if current_hist >= 0 else "🔴 Негативний"
        signal_action = "BUY" if current_hist >= 0 else "SELL"

        return {"signal": signal_action, "trend": trend, "histogram": histogram, "klines": klines}
    except Exception as e:
        logging.error(f"MACD error: {e}")
        return None

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обчислення MACD...")
    result = get_macd_signal()

    if not result:
        await update.message.reply_text("Не вдалося отримати MACD сигнал")
        return

    price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
    hist = result["histogram"][-1] if result["histogram"] else 0
    emoji = "🟢" if hist >= 0 else "🔴"

    text = f"{TRADE_SYMBOL} @ {price:.2f}\nMACD: {emoji} {hist:.4f}\nТренд: {result['trend']}\nСигнал: {result['signal']}"
    await update.message.reply_text(text)

async def execute_market_trade(side: str):
    try:
        if side == "BUY":
            balance = client.get_account()
            usdc = float(next((a["free"] for a in balance["balances"] if a["asset"] == "USDC"), 0))
            if usdc < 10:
                return "Недостатньо USDC"

            price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
            qty = usdc / price
            order = client.create_order(
                symbol=TRADE_SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=f"{qty:.8f}"
            )

            filled = sum(float(f["qty"]) for f in order["fills"])
            avg = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / filled if filled else 0

            save_trade({"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "BUY", "amount": filled, "price": avg})
            return f"Куплено {filled:.8f} BTC за ~{avg:.2f}"

        elif side == "SELL":
            balance = client.get_account()
            btc = float(next((a["free"] for a in balance["balances"] if a["asset"] == "BTC"), 0))
            if btc < 0.0001:
                return "Недостатньо BTC"

            order = client.create_order(
                symbol=TRADE_SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=f"{btc:.8f}"
            )

            filled = sum(float(f["qty"]) for f in order["fills"])
            avg = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / filled if filled else 0

            save_trade({"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "type": "SELL", "amount": filled, "price": avg})
            return f"Продано {filled:.8f} BTC за ~{avg:.2f}"

    except Exception as e:
        logging.error(f"Trade error ({side}): {e}")
        return f"Помилка: {str(e)}"

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Купівля...")
    result = execute_market_trade("BUY")
    await update.message.reply_text(result)

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Продаж...")
    result = execute_market_trade("SELL")
    await update.message.reply_text(result)

async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return

    signal = get_macd_signal()
    if signal and signal["signal"]:
        result = execute_market_trade(signal["signal"])
        await context.bot.send_message(
            context.job.data["chat_id"],
            f"Авто: {signal['signal']} → {result}"
        )

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled

    for job in context.application.job_queue.get_jobs_by_name("auto"):
        job.schedule_removal()

    if auto_trading_enabled:
        context.application.job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,
            name="auto",
            data={"chat_id": update.effective_chat.id}
        )
        await update.message.reply_text("Авто увімкнено")
    else:
        await update.message.reply_text("Авто вимкнено")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = client.get_account()
        btc = float(next((a["free"] for a in account["balances"] if a["asset"] == "BTC"), 0))
        usdc = float(next((a["free"] for a in account["balances"] if a["asset"] == "USDC"), 0))
        await update.message.reply_text(f"BTC: {btc:.8f}\nUSDC: {usdc:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
        await update.message.reply_text(f"{TRADE_SYMBOL}: {price:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {str(e)}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("Історія порожня")
        return

    lines = ["Останні угоди:"]
    for t in trade_history[-10:]:
        lines.append(f"{t['date']} {t['type']} {t['amount']:.8f} @ {t['price']:.2f}")
    await update.message.reply_text("\n".join(lines))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Баланс", "📈 Ціна"],
        ["📊 MACD", "🤖 Авто"],
        ["🟢 Купити", "🔴 Продати"],
        ["📊 Історія"]
    ]
    await update.message.reply_text("Вибери дію", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

def main():
    load_trade_history()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("Баланс"), get_balance))
    app.add_handler(MessageHandler(filters.Regex("Ціна"), get_price))
    app.add_handler(MessageHandler(filters.Regex("MACD"), macd_signal_command))
    app.add_handler(MessageHandler(filters.Regex("Авто"), toggle_auto_trading))
    app.add_handler(MessageHandler(filters.Regex("Купити"), buy_btc_command))
    app.add_handler(MessageHandler(filters.Regex("Продати"), sell_btc_command))
    app.add_handler(MessageHandler(filters.Regex("Історія"), show_statistics))

    logging.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
