import asyncio
import logging
import os
import json
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from binance.client import Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_API_KEY')

if not all([API_KEY, SECRET_KEY, TELEGRAM_TOKEN]):
    logging.error("Missing environment variables")
    exit(1)

client = Client(API_KEY, SECRET_KEY)
SYMBOL = "BTCUSDC"

auto_trading_enabled = False
trade_history = []
HISTORY_FILE = "trade_history.json"


def load_history():
    global trade_history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except Exception:
            trade_history = []


def save_trade(data):
    trade_history.append(data)
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2)
    except Exception as e:
        logging.error("Cannot save history: %s", e)


def get_macd_signal():
    try:
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=Client.KLINE_INTERVAL_1MINUTE,
            limit=100
        )
        closes = [float(k[4]) for k in klines]
        if len(closes) < 26:
            return None

        fast = [closes[0]]
        slow = [closes[0]]
        af = 2 / 13
        as_ = 2 / 27
        for p in closes[1:]:
            fast.append(p * af + fast[-1] * (1 - af))
            slow.append(p * as_ + slow[-1] * (1 - as_))

        macd = [f - s for f, s in zip(fast, slow)]
        sig = [macd[0]]
        as_sig = 2 / 10
        for m in macd[1:]:
            sig.append(m * as_sig + sig[-1] * (1 - as_sig))

        hist = macd[-1] - sig[-1]
        return "BUY" if hist >= 0 else "SELL"
    except Exception as e:
        logging.error("MACD failed: %s", e)
        return None


async def safe_send(update_or_context, text):
    """Безпечна відправка повідомлення без parse_mode"""
    try:
        if hasattr(update_or_context, 'message'):
            await update_or_context.message.reply_text(text[:4000])
        elif hasattr(update_or_context, 'bot'):
            await update_or_context.bot.send_message(
                chat_id=update_or_context.job.chat_id,
                text=text[:4000]
            )
    except Exception as e:
        logging.error("Cannot send message: %s", e)


async def execute_trade(side):
    try:
        if side == "BUY":
            account = client.get_account()
            usdc = float(next((b["free"] for b in account["balances"] if b["asset"] == "USDC"), 0))
            if usdc < 10:
                return "Недостатньо USDC"

            price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
            qty = usdc / price
            qty_str = f"{qty:.8f}"

            order = client.create_order(
                symbol=SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=qty_str
            )

            filled = sum(float(f["qty"]) for f in order["fills"])
            avg = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / filled if filled else 0

            save_trade({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "BUY",
                "amount": filled,
                "price": avg
            })

            return f"🟢 Куплено {filled:.8f} BTC за ~{avg:.2f}"

        elif side == "SELL":
            account = client.get_account()
            btc = float(next((b["free"] for b in account["balances"] if b["asset"] == "BTC"), 0))
            if btc < 0.0001:
                return "Недостатньо BTC"

            qty_str = f"{btc:.8f}"

            order = client.create_order(
                symbol=SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=qty_str
            )

            filled = sum(float(f["qty"]) for f in order["fills"])
            avg = sum(float(f["price"]) * float(f["qty"]) for f in order["fills"]) / filled if filled else 0

            save_trade({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "SELL",
                "amount": filled,
                "price": avg
            })

            return f"🔴 Продано {filled:.8f} BTC за ~{avg:.2f}"

    except Exception as e:
        logging.error(f"Trade failed ({side}): {str(e)}")
        return f"Помилка: {str(e)}"


async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update, "Купівля...")
    result = await asyncio.to_thread(execute_trade, "BUY")
    await safe_send(update, result)


async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(update, "Продаж...")
    result = await asyncio.to_thread(execute_trade, "SELL")
    await safe_send(update, result)


async def check_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return

    signal = await asyncio.to_thread(get_macd_signal)
    if signal:
        result = await asyncio.to_thread(execute_trade, signal)
        await safe_send(context, f"Авто: {signal} → {result}")


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled

    job_queue = context.application.job_queue
    for job in job_queue.get_jobs_by_name("auto"):
        job.schedule_removal()

    if auto_trading_enabled:
        job_queue.run_repeating(
            check_and_trade,
            interval=AUTO_INTERVAL,
            first=5,
            name="auto",
            chat_id=update.effective_chat.id
        )
        await safe_send(update, "Автотрейдинг увімкнено")
    else:
        await safe_send(update, "Автотрейдинг вимкнено")


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    signal = await asyncio.to_thread(get_macd_signal)
    if not signal:
        await safe_send(update, "Не вдалося отримати сигнал MACD")
        return

    price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
    emoji = "🟢" if signal == "BUY" else "🔴"

    text = f"{SYMBOL} @ {price:.2f}\nСигнал: {emoji} {signal}"
    await safe_send(update, text)


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = client.get_account()
        btc = float(next((b["free"] for b in account["balances"] if b["asset"] == "BTC"), 0))
        usdc = float(next((b["free"] for b in account["balances"] if b["asset"] == "USDC"), 0))
        text = f"Баланс:\nBTC: {btc:.8f}\nUSDC: {usdc:.2f}"
        await safe_send(update, text)
    except Exception as e:
        await safe_send(update, f"Помилка: {str(e)}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        await safe_send(update, f"{SYMBOL}: {price:.2f}")
    except Exception as e:
        await safe_send(update, f"Помилка: {str(e)}")


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await safe_send(update, "Історія порожня")
        return

    lines = ["Останні угоди:"]
    for t in trade_history[-10:]:
        lines.append(f"{t['date']} {t['type']} {t['amount']:.8f} @ {t['price']:.2f}")
    await safe_send(update, "\n".join(lines))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Баланс", "📈 Ціна"],
        ["📊 MACD", "🤖 Авто"],
        ["🟢 Купити", "🔴 Продати"],
        ["📊 Історія"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await safe_send(update, "Вибери дію ↓", reply_markup=reply_markup)


def main():
    load_history()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^(💰 Баланс)$"), get_balance))
    app.add_handler(MessageHandler(filters.Regex("^(📈 Ціна)$"), get_price))
    app.add_handler(MessageHandler(filters.Regex("^(📊 MACD)$"), macd_signal_command))
    app.add_handler(MessageHandler(filters.Regex("^(🤖 Авто)$"), toggle_auto_trading))
    app.add_handler(MessageHandler(filters.Regex("^(🟢 Купити)$"), buy_btc_command))
    app.add_handler(MessageHandler(filters.Regex("^(🔴 Продати)$"), sell_btc_command))
    app.add_handler(MessageHandler(filters.Regex("^(📊 Історія)$"), show_statistics))

    logging.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
