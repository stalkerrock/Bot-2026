import asyncio
import logging
import os
import json
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from binance.client import Client
from binance.exceptions import BinanceAPIException

from decimal import Decimal, ROUND_DOWN

# Логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

API_KEY = "3v2KzK8lhtYblymiQiRd9aFxuRZXOuv3wdgZnVgPGTWSIw7WQUYxxrPlf9cYQ8ul"
SECRET_KEY = "aFXnMhVhhet45dBQyxVbJzVgJS5pSUsC8P7SvDvGS1Tn0WDkWMKQMD3PdZUOOitR"
TELEGRAM_API_KEY = os.environ.get('TELEGRAM_API_KEY')

if not TELEGRAM_API_KEY:
    logging.error("TELEGRAM_API_KEY not found in environment variables")
    exit(1)

client = Client(API_KEY, SECRET_KEY)
TRADE_SYMBOL = "BTCUSDC"

MACD_FAST = 5
MACD_SLOW = 10
MACD_SIGNAL = 3
AUTO_TRADE_INTERVAL = 60

auto_trading_enabled = False
trade_history = []
TRADE_HISTORY_FILE = "trade_history.json"
last_buy_price = None
symbol_filters = {}


def load_trade_history():
    global trade_history
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except Exception:
            trade_history = []
    logging.info(f"Завантажено {len(trade_history)} угод")


def save_trade(data):
    trade_history.append(data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2)
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
            symbol=TRADE_SYMBOL,
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

        return {"signal": action, "trend": trend, "histogram": current_hist}
    except Exception as e:
        logging.error(f"MACD failed: {e}")
        return None


def get_symbol_filters_info():
    global symbol_filters
    if TRADE_SYMBOL in symbol_filters:
        return symbol_filters[TRADE_SYMBOL]

    try:
        exchange_info = client.get_exchange_info()
        symbol_info = next(s for s in exchange_info['symbols'] if s['symbol'] == TRADE_SYMBOL)
        
        filters_dict = {f['filterType']: f for f in symbol_info['filters']}
        
        lot_size = filters_dict.get('LOT_SIZE') or filters_dict.get('MARKET_LOT_SIZE')
        min_notional = filters_dict['NOTIONAL']
        
        current_filters = {
            'minNotional': Decimal(min_notional['minNotional']),
            'minQty': Decimal(lot_size['minQty']),
            'maxQty': Decimal(lot_size['maxQty']),
            'stepSize': Decimal(lot_size['stepSize']),
        }
        
        step_size_str = str(current_filters['stepSize'])
        if '.' in step_size_str:
            current_filters['quantityPrecision'] = len(step_size_str.split('.')[1].rstrip('0'))
        else:
            current_filters['quantityPrecision'] = 0

        symbol_filters[TRADE_SYMBOL] = current_filters
        logging.info(f"Filters for {TRADE_SYMBOL}: {current_filters}")
        return current_filters
    except Exception as e:
        logging.error(f"Failed to get filters: {e}")
        raise


def execute_market_trade(side: str):
    global last_buy_price
    try:
        filters_info = get_symbol_filters_info()
        min_notional = filters_info['minNotional']
        min_qty = filters_info['minQty']
        max_qty = filters_info['maxQty']
        step_size = filters_info['stepSize']
        qty_precision = filters_info['quantityPrecision']

        account = client.get_account()
        price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price'])

        logging.info(f"Баланс перед {side}: {account['balances']}")

        if side == "BUY":
            usdc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'USDC'), 0))
            logging.info(f"USDC free: {usdc}")

            if usdc < 10:
                return f"Недостатньо USDC: {usdc:.2f} (мін. ~10 USDC)"

            qty = usdc / price
            qty_str = f"{qty:.{qty_precision}f}"

            order = client.create_order(
                symbol=TRADE_SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=qty_str
            )

            filled = sum(float(f['qty']) for f in order['fills'])
            avg = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled if filled else 0

            save_trade({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "BUY",
                "amount": filled,
                "price": avg
            })
            last_buy_price = avg
            return f"🟢 Куплено {filled:.8f} BTC @ {avg:.2f} USDC"

        elif side == "SELL":
            btc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'BTC'), 0))
            logging.info(f"BTC free: {btc:.8f}, min_qty: {min_qty}")

            if btc < min_qty:
                return f"Недостатньо BTC для продажу\nДоступно: {btc:.8f} BTC\nМінімум: {min_qty:.8f} BTC"

            qty_str = f"{btc:.{qty_precision}f}"

            order = client.create_order(
                symbol=TRADE_SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=qty_str
            )

            filled = sum(float(f['qty']) for f in order['fills'])
            avg = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled if filled else 0

            save_trade({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "SELL",
                "amount": filled,
                "price": avg
            })
            last_buy_price = None
            return f"🔴 Продано {filled:.8f} BTC @ {avg:.2f} USDC"

    except BinanceAPIException as e:
        logging.error(f"Binance API error ({side}): {e.code} - {e.message}")
        if e.code == -1013:
            return "Помилка: LOT_SIZE (занадто мала кількість BTC)"
        if e.code == -2010:
            return "Помилка: Недостатньо коштів або інші обмеження"
        return f"Binance помилка: {e.message}"
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

    result = get_macd_signal()
    if not result or not result.get("signal"):
        return

    trade_msg = execute_market_trade(result["signal"])
    price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])

    text = (
        f"🤖 Авто {datetime.now().strftime('%H:%M:%S')}\n"
        f"{TRADE_SYMBOL} @ {price:.2f}\n"
        f"Сигнал: {result['signal']}\n"
        f"Результат: {trade_msg}"
    )

    await context.bot.send_message(
        chat_id=context.job.data["chat_id"],
        text=text
    )


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled

    job_queue = context.application.job_queue

    for job in job_queue.get_jobs_by_name("auto"):
        job.schedule_removal()

    if auto_trading_enabled:
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,
            name="auto",
            data={"chat_id": update.effective_chat.id}
        )
        await update.message.reply_text("Автотрейдинг увімкнено (кожну хвилину)")
    else:
        await update.message.reply_text("Автотрейдинг вимкнено")


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = get_macd_signal()
    if not result or not result.get("histogram"):
        await update.message.reply_text("Не вдалося отримати MACD")
        return

    price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
    hist = result["histogram"]
    emoji = "🟢" if hist >= 0 else "🔴"

    text = f"BTCUSDC @ {price:.2f}\nMACD: {emoji} {hist:.4f}\nТренд: {result['trend']}"
    await update.message.reply_text(text)


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = client.get_account()
        btc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'BTC'), 0))
        usdc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'USDC'), 0))
        await update.message.reply_text(f"BTC: {btc:.8f}\nUSDC: {usdc:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
        await update.message.reply_text(f"BTCUSDC: {price:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {e}")


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
    await update.message.reply_text(
        "Вибери дію",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


def main():
    load_trade_history()

    application = Application.builder().token(TELEGRAM_API_KEY).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("Баланс"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("Ціна"), get_price))
    application.add_handler(MessageHandler(filters.Regex("MACD"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("Авто"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("Купити"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("Продати"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("Історія"), show_statistics))

    logging.info("Bot starting...")
    application.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
