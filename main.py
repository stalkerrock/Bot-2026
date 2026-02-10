import asyncio
import logging
import os
import json
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from binance.client import Client

from decimal import Decimal, ROUND_DOWN

# Логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Ключі з середовища
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_API_KEY')

if not all([API_KEY, SECRET_KEY, TELEGRAM_TOKEN]):
    logging.error("Missing environment variables")
    exit(1)

client = Client(API_KEY, SECRET_KEY)
SYMBOL = "BTCUSDC"

# Параметри MACD (1-хвилинний скальпінг)
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
AUTO_INTERVAL = 60  # 60 секунд

auto_trading_enabled = False
trade_history = []
HISTORY_FILE = "trade_history.json"


def load_trade_history():
    global trade_history
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except Exception as e:
            logging.error(f"Помилка завантаження історії: {e}")
            trade_history = []
    else:
        logging.info("Файл історії не знайдено, починаємо з порожнього")
    logging.info(f"Завантажено {len(trade_history)} угод")


def save_trade(trade_data):
    global trade_history
    trade_history.append(trade_data)
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2)
        logging.info(f"Збережено угоду: {trade_data}")
    except Exception as e:
        logging.error(f"Помилка збереження: {e}")


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
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=Client.KLINE_INTERVAL_1MINUTE,
            limit=100
        )
        closes = [float(k[4]) for k in klines]

        if len(closes) < MACD_SLOW:
            return None

        fast = calculate_ema(closes, MACD_FAST)
        slow = calculate_ema(closes, MACD_SLOW)
        macd = [f - s for f, s in zip(fast, slow)]
        signal = calculate_ema(macd, MACD_SIGNAL)
        hist = [m - s for m, s in zip(macd[-len(signal):], signal)]

        current_hist = hist[-1]
        action = "BUY" if current_hist >= 0 else "SELL"
        trend = "🟢 Позитивний" if current_hist >= 0 else "🔴 Негативний"

        return {
            "signal": action,
            "trend": trend,
            "histogram": current_hist
        }
    except Exception as e:
        logging.error(f"MACD failed: {e}")
        return None


def execute_trade(side):
    try:
        account = client.get_account()
        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])

        if side == "BUY":
            usdc = float(next((a["free"] for a in account["balances"] if a["asset"] == "USDC"), 0))
            if usdc < 10:
                return f"Недостатньо USDC: {usdc:.2f}"

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

            return f"🟢 Куплено {filled:.8f} @ {avg:.2f}"

        elif side == "SELL":
            btc = float(next((a["free"] for a in account["balances"] if a["asset"] == "BTC"), 0))
            if btc < 0.0001:
                return f"Недостатньо BTC: {btc:.8f}"

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

            return f"🔴 Продано {filled:.8f} @ {avg:.2f}"

    except Exception as e:
        logging.error(f"Trade failed ({side}): {e}")
        return f"Помилка: {str(e)}"


async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Кнопка Купити натиснута")
    await update.message.reply_text("Купівля...")
    result = await asyncio.to_thread(execute_trade, "BUY")
    await update.message.reply_text(result)


async def sell_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Кнопка Продати натиснута")
    await update.message.reply_text("Продаж...")
    result = await asyncio.to_thread(execute_trade, "SELL")
    await update.message.reply_text(result)


async def check_and_trade(context: ContextTypes.DEFAULT_TYPE):
    logging.info("Запущено перевірку автотрейдингу")
    
    if not auto_trading_enabled:
        logging.info("Автотрейдинг вимкнено, пропускаємо")
        return

    result = await asyncio.to_thread(get_macd_signal)
    if not result:
        logging.warning("MACD сигнал не отримано")
        return

    hist = result["histogram"]
    signal = result["signal"]

    logging.info(f"Signal: {signal}, Hist: {hist:.4f}")

    trade_msg = None
    if signal == "BUY" and hist >= 0:
        trade_msg = await asyncio.to_thread(execute_trade, "BUY")
    elif signal == "SELL" and hist < 0:
        trade_msg = await asyncio.to_thread(execute_trade, "SELL")
    else:
        logging.info("Немає сигналу для угоди")
        return

    price = await asyncio.to_thread(lambda: float(client.get_symbol_ticker(symbol=SYMBOL)["price"]))
    color = "🟢" if hist >= 0 else "🔴"

    text = (
        f"🤖 Авто {datetime.now().strftime('%H:%M:%S')}\n"
        f"{SYMBOL} @ {price:.2f}\n"
        f"MACD: {color} {hist:.4f}\n"
        f"Результат: {trade_msg}"
    )

    try:
        await context.bot.send_message(
            chat_id=context.job.data["chat_id"],
            text=text
        )
        logging.info("Авто-повідомлення надіслано")
    except Exception as e:
        logging.error(f"Помилка надсилання: {e}")


async def toggle_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    logging.info(f"Авто змінено на: {auto_trading_enabled}")

    job_queue = context.application.job_queue

    # Видаляємо старі завдання
    for job in job_queue.get_jobs_by_name("auto"):
        job.schedule_removal()
        logging.info("Видалено старе завдання")

    if auto_trading_enabled:
        logging.info("Запускаємо автотрейдинг")
        job_queue.run_repeating(
            check_and_trade,
            interval=AUTO_INTERVAL,
            first=10,
            name="auto",
            data={"chat_id": update.effective_chat.id}
        )
        await update.message.reply_text("Автотрейдинг УВІМКНЕНО")
    else:
        await update.message.reply_text("Автотрейдинг ВИМКНЕНО")


async def macd_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = await asyncio.to_thread(get_macd_signal)
    if not result:
        await update.message.reply_text("Не вдалося отримати MACD")
        return

    price = await asyncio.to_thread(lambda: float(client.get_symbol_ticker(symbol=SYMBOL)["price"]))
    hist = result["histogram"]
    emoji = "🟢" if hist >= 0 else "🔴"

    text = f"{SYMBOL} @ {price:.2f}\nMACD: {emoji} {hist:.4f}\nТренд: {result['trend']}"
    await update.message.reply_text(text)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = client.get_account()
        btc = float(next((a["free"] for a in account["balances"] if a["asset"] == "BTC"), 0))
        usdc = float(next((a["free"] for a in account["balances"] if a["asset"] == "USDC"), 0))
        text = f"BTC: {btc:.8f}\nUSDC: {usdc:.2f}"
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
        await update.message.reply_text(f"{SYMBOL}: {price:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(
        "Вибери дію",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


def main():
    load_trade_history()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("Баланс"), balance_command))
    app.add_handler(MessageHandler(filters.Regex("Ціна"), price_command))
    app.add_handler(MessageHandler(filters.Regex("MACD"), macd_command))
    app.add_handler(MessageHandler(filters.Regex("Авто"), toggle_auto))
    app.add_handler(MessageHandler(filters.Regex("Купити"), buy_command))
    app.add_handler(MessageHandler(filters.Regex("Продати"), sell_command))
    app.add_handler(MessageHandler(filters.Regex("Історія"), stats_command))

    logging.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
