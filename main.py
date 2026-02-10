import asyncio
import logging
import os
import json
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from decimal import Decimal, ROUND_DOWN

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

# Отримання ключів з змінних середовища
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_API_KEY')

if not all([API_KEY, SECRET_KEY, TELEGRAM_TOKEN]):
    logging.error("Missing environment variables")
    exit(1)

client = Client(API_KEY, SECRET_KEY)
TRADE_SYMBOL = "BTCUSDC"

# Налаштування MACD
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
AUTO_TRADE_INTERVAL = 60  # 60 секунд = 1 хвилина

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
        except json.JSONDecodeError:
            trade_history = []
    logging.info(f"Завантажено {len(trade_history)} угод з історії")


def save_trade(trade_data):
    global trade_history
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
        logging.info(f"Збережено угоду: {trade_data}")
    except Exception as e:
        logging.error(f"Помилка збереження історії: {e}")


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
        return current_filters
    except Exception as e:
        logging.error(f"Помилка отримання фільтрів: {e}")
        raise


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
        
        if len(closes) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
            return None

        fast = calculate_ema(closes, MACD_FAST)
        slow = calculate_ema(closes, MACD_SLOW)
        macd = [f - s for f, s in zip(fast, slow)]
        signal = calculate_ema(macd, MACD_SIGNAL)
        histogram = [m - s for m, s in zip(macd[-len(signal):], signal)]

        current_hist = histogram[-1]
        action = "BUY" if current_hist >= 0 else "SELL"
        trend = "🟢 Позитивний" if current_hist >= 0 else "🔴 Негативний"

        return {
            "signal": action,
            "trend": trend,
            "histogram": histogram,
            "current_hist": current_hist,
            "klines": klines
        }
    except Exception as e:
        logging.error(f"MACD calculation failed: {e}")
        return None


def execute_market_trade(side: str):
    try:
        filters_info = get_symbol_filters_info()
        min_notional = filters_info['minNotional']
        min_qty = filters_info['minQty']
        max_qty = filters_info['maxQty']
        step_size = filters_info['stepSize']
        qty_precision = filters_info['quantityPrecision']

        account = client.get_account()
        price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price'])

        if side == "BUY":
            usdc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'USDC'), 0))
            if usdc < float(min_notional):
                return f"Недостатньо USDC (є {usdc:.2f}, потрібно мінімум {min_notional:.2f})"

            qty = usdc / price
            qty = (qty // step_size) * step_size
            if qty < min_qty:
                return f"Кількість {qty:.8f} менша за мінімум {min_qty}"

            order = client.create_order(
                symbol=TRADE_SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=f"{qty:.{qty_precision}f}"
            )

            filled = sum(float(f['qty']) for f in order['fills'])
            avg_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled if filled else 0

            save_trade({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "BUY",
                "amount": filled,
                "price": avg_price
            })

            return f"🟢 Куплено {filled:.8f} BTC за ~{avg_price:.2f}"

        elif side == "SELL":
            btc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'BTC'), 0))
            if btc < min_qty:
                return f"Недостатньо BTC (є {btc:.8f}, потрібно мінімум {min_qty})"

            qty = (btc // step_size) * step_size
            if qty > max_qty:
                qty = max_qty

            order = client.create_order(
                symbol=TRADE_SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=f"{qty:.{qty_precision}f}"
            )

            filled = sum(float(f['qty']) for f in order['fills'])
            avg_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled if filled else 0

            save_trade({
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "SELL",
                "amount": filled,
                "price": avg_price
            })

            return f"🔴 Продано {filled:.8f} BTC за ~{avg_price:.2f}"

    except Exception as e:
        logging.error(f"Trade error ({side}): {e}")
        return f"Помилка торгівлі: {str(e)}"


async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Виконується купівля...")
    result = await asyncio.to_thread(execute_market_trade, "BUY")
    await update.message.reply_text(result)


async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Виконується продаж...")
    result = await asyncio.to_thread(execute_market_trade, "SELL")
    await update.message.reply_text(result)


async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return

    logging.info("Перевірка MACD для автотрейдингу")
    
    result = await asyncio.to_thread(get_macd_signal)
    
    if not result:
        logging.warning("MACD сигнал не отримано")
        return
    
    hist = result["current_hist"]
    signal = result["signal"]
    
    logging.info(f"MACD: hist={hist:.4f}, signal={signal}")
    
    if signal == "BUY" and hist >= 0:
        logging.info("Виконується авто-купівля")
        msg = await asyncio.to_thread(execute_market_trade, "BUY")
    elif signal == "SELL" and hist < 0:
        logging.info("Виконується авто-продаж")
        msg = await asyncio.to_thread(execute_market_trade, "SELL")
    else:
        logging.info("Умови для угоди не виконані")
        return
    
    price = await asyncio.to_thread(lambda: float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price']))
    
    color = "🟢" if hist >= 0 else "🔴"
    
    text = (
        f"🤖 Авто {datetime.now().strftime('%H:%M:%S')}\n"
        f"{TRADE_SYMBOL} @ {price:.2f}\n"
        f"MACD: {color} {hist:.4f}\n"
        f"Результат: {msg}"
    )
    
    try:
        await context.bot.send_message(
            chat_id=context.job.data["chat_id"],
            text=text
        )
        logging.info("Повідомлення про авто-угоду надіслано")
    except Exception as e:
        logging.error(f"Не вдалося надіслати повідомлення: {e}")


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    logging.info(f"Автотрейдинг змінено на: {auto_trading_enabled}")

    job_queue = context.application.job_queue
    
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
        logging.info("Видалено старе завдання автотрейдингу")

    if auto_trading_enabled:
        logging.info("Запускаємо автотрейдинг")
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,
            name="auto_trading",
            data={"chat_id": update.effective_chat.id}
        )
        await update.message.reply_text(
            "✅ Автотрейдинг увімкнено\n"
            f"Перевірка кожні {AUTO_TRADE_INTERVAL} секунд\n"
            "Перша перевірка через ~10 секунд"
        )
    else:
        await update.message.reply_text("⛔ Автотрейдинг вимкнено")


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обчислення MACD (1 хв)...")
    
    result = await asyncio.to_thread(get_macd_signal)
    
    if not result:
        await update.message.reply_text("Не вдалося отримати сигнал")
        return
    
    price = await asyncio.to_thread(lambda: float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price']))
    hist = result["current_hist"]
    emoji = "🟢" if hist >= 0 else "🔴"
    
    text = f"{TRADE_SYMBOL} @ {price:.2f}\nMACD: {emoji} {hist:.4f}\nТренд: {result['trend']}\nСигнал: {result['signal']}"
    await update.message.reply_text(text)


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        account = client.get_account()
        btc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'BTC'), 0))
        usdc = float(next((a['free'] for a in account['balances'] if a['asset'] == 'USDC'), 0))
        await update.message.reply_text(f"BTC: {btc:.8f}\nUSDC: {usdc:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {str(e)}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
        await update.message.reply_text(f"{TRADE_SYMBOL}: {price:.2f} USDC")
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
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Вибери дію ↓",
        reply_markup=reply_markup
    )


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
