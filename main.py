import asyncio
import logging
import os
import json
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from binance.client import Client

# Логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Конфігурація
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_API_KEY')

if not all([API_KEY, SECRET_KEY, TELEGRAM_TOKEN]):
    logging.error("Missing one or more environment variables")
    exit(1)

client = Client(API_KEY, SECRET_KEY)
SYMBOL = "BTCUSDC"

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
AUTO_INTERVAL = 60

auto_trading_enabled = False
trade_history = []
HISTORY_FILE = "trade_history.json"


def load_history():
    global trade_history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except Exception as e:
            logging.error("Cannot load trade history: %s", e)
            trade_history = []


def save_trade(trade_data):
    trade_history.append(trade_data)
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2)
        logging.info("Trade saved: %s", trade_data)
    except Exception as e:
        logging.error("Cannot save trade history: %s", e)


def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    for price in prices[1:]:
        ema.append(price * alpha + ema[-1] * (1 - alpha))
    return ema


def get_macd_signal():
    try:
        start_ts = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=Client.KLINE_INTERVAL_1MINUTE,
            limit=100,
            startTime=start_ts
        )
        closes = [float(k[4]) for k in klines]

        if len(closes) < MACD_SLOW:
            return None

        fast_ema = calculate_ema(closes, MACD_FAST)
        slow_ema = calculate_ema(closes, MACD_SLOW)
        macd = [f - s for f, s in zip(fast_ema, slow_ema)]
        signal = calculate_ema(macd, MACD_SIGNAL)
        histogram = [m - s for m, s in zip(macd[-len(signal):], signal)]

        current_hist = histogram[-1]
        action = "BUY" if current_hist >= 0 else "SELL"
        trend = "🟢 Позитивний" if current_hist >= 0 else "🔴 Негативний"

        return {
            "signal": action,
            "trend": trend,
            "histogram": histogram,
            "current_hist": current_hist
        }

    except Exception as e:
        logging.error("MACD calculation failed: %s", e)
        return None


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обчислення MACD сигналу...")
    result = get_macd_signal()

    if not result:
        await update.message.reply_text("Не вдалося отримати MACD сигнал")
        return

    price = client.get_symbol_ticker(symbol=SYMBOL)["price"]
    price = float(price)

    hist_emoji = "🟢" if result["current_hist"] >= 0 else "🔴"

    text = (
        f"<b>{SYMBOL} @ {price:.2f} (1m)</b>\n"
        f"<b>MACD: {hist_emoji} {result['current_hist']:.4f}</b>\n"
        f"Тренд: {result['trend']}\n"
        f"Сигнал: {result['signal']}"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = client.get_account()
        btc = next((a for a in account["balances"] if a["asset"] == "BTC"), {"free": "0"})
        usdc = next((a for a in account["balances"] if a["asset"] == "USDC"), {"free": "0"})
        text = f"💰 Баланс:\nBTC: {float(btc['free']):.8f}\nUSDC: {float(usdc['free']):.2f}"
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Помилка отримання балансу: {e}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        await update.message.reply_text(f"📈 {SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("Історія торгів порожня")
        return

    lines = ["<b>Останні 10 угод:</b>"]
    for trade in trade_history[-10:]:
        lines.append(
            f"{trade['date']} | {trade['type']} | "
            f"{trade['amount']:.8f} BTC @ {trade['price']:.2f}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def execute_trade(side: str):
    try:
        if side == "BUY":
            account = client.get_account()
            usdc_free = float(next((a["free"] for a in account["balances"] if a["asset"] == "USDC"), 0))
            if usdc_free < 10:
                return "Недостатньо USDC"

            price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
            qty = usdc_free / price

            order = client.create_order(
                symbol=SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=f"{qty:.8f}"
            )

            filled_qty = sum(float(f["qty"]) for f in order["fills"])
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / filled_qty

            data = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "BUY",
                "amount": filled_qty,
                "price": avg_price
            }
            save_trade(data)
            return f"🟢 Куплено {filled_qty:.8f} BTC за ~{avg_price:.2f}"

        elif side == "SELL":
            account = client.get_account()
            btc_free = float(next((a["free"] for a in account["balances"] if a["asset"] == "BTC"), 0))
            if btc_free < 0.0001:
                return "Недостатньо BTC"

            order = client.create_order(
                symbol=SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=f"{btc_free:.8f}"
            )

            filled_qty = sum(float(f["qty"]) for f in order["fills"])
            avg_price = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / filled_qty

            data = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "SELL",
                "amount": filled_qty,
                "price": avg_price
            }
            save_trade(data)
            return f"🔴 Продано {filled_qty:.8f} BTC за ~{avg_price:.2f}"

    except Exception as e:
        logging.error("Trade execution error: %s", e)
        return f"Помилка торгівлі: {str(e)}"


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Виконується купівля...")
    result = await asyncio.to_thread(execute_trade, "BUY")
    await update.message.reply_text(result)


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Виконується продаж...")
    result = await asyncio.to_thread(execute_trade, "SELL")
    await update.message.reply_text(result)


async def check_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return

    result = get_macd_signal()
    if not result:
        return

    if result["signal"] == "BUY":
        msg = await asyncio.to_thread(execute_trade, "BUY")
    elif result["signal"] == "SELL":
        msg = await asyncio.to_thread(execute_trade, "SELL")
    else:
        return

    price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
    emoji = "🟢" if result["current_hist"] >= 0 else "🔴"

    text = (
        f"🤖 Автоугода {datetime.now().strftime('%H:%M:%S')}\n"
        f"{SYMBOL} @ {price:.2f}\n"
        f"MACD: {emoji} {result['current_hist']:.4f}\n"
        f"Результат: {msg}"
    )
    await context.bot.send_message(context.job.chat_id, text)


async def toggle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled

    job_queue = context.application.job_queue
    for job in job_queue.get_jobs_by_name("auto_trade"):
        job.schedule_removal()

    if auto_trading_enabled:
        job_queue.run_repeating(
            check_and_trade,
            interval=AUTO_INTERVAL,
            first=10,
            name="auto_trade",
            chat_id=update.effective_chat.id
        )
        await update.message.reply_text("Автотрейдинг УВІМКНЕНО")
    else:
        await update.message.reply_text("Автотрейдинг ВИМКНЕНО")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Баланс", "📈 Ціна"],
        ["📊 MACD", "🤖 Авто"],
        ["🟢 Купити", "🔴 Продати"],
        ["📊 Історія"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "Bitcoin Scalping Bot\n\n"
        "Виберіть дію:",
        reply_markup=reply_markup
    )


def main():
    load_history()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^(💰 Баланс)$"), get_balance))
    app.add_handler(MessageHandler(filters.Regex("^(📈 Ціна)$"), get_price))
    app.add_handler(MessageHandler(filters.Regex("^(📊 MACD)$"), macd_signal_command))
    app.add_handler(MessageHandler(filters.Regex("^(🤖 Авто)$"), toggle_auto))
    app.add_handler(MessageHandler(filters.Regex("^(🟢 Купити)$"), buy_command))
    app.add_handler(MessageHandler(filters.Regex("^(🔴 Продати)$"), sell_command))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Історія)$"), show_statistics))

    logging.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
