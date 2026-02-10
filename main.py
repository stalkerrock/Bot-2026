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

# Налаштування MACD для скальпінгу 1хв
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
    logging.info("Loading trade history...")
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        except json.JSONDecodeError as e:
            logging.error(f"Error loading trade history: {e}. Starting with empty history.")
            trade_history = []
    else:
        logging.info("Trade history file not found. Starting with empty history.")


def save_trade(trade_data):
    global trade_history
    logging.info(f"Saving trade: {trade_data}")
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except IOError as e:
        logging.error(f"Error saving trade history to file: {e}")


def get_symbol_filters_info():
    global symbol_filters
    if TRADE_SYMBOL in symbol_filters:
        return symbol_filters[TRADE_SYMBOL]

    max_retries = 3
    for attempt in range(max_retries):
        try:
            exchange_info = client.get_exchange_info()
            found_symbol_info = None
            for s_info in exchange_info['symbols']:
                if s_info['symbol'] == TRADE_SYMBOL:
                    found_symbol_info = s_info
                    break
            
            if not found_symbol_info:
                raise ValueError(f"Symbol '{TRADE_SYMBOL}' not found.")
            
            filters_dict = {f['filterType']: f for f in found_symbol_info['filters']}
            
            if 'NOTIONAL' not in filters_dict: 
                raise ValueError(f"Filter 'NOTIONAL' not found for {TRADE_SYMBOL}.")
            
            lot_size_filter = None
            if 'LOT_SIZE' in filters_dict:
                lot_size_filter = filters_dict['LOT_SIZE']
            elif 'MARKET_LOT_SIZE' in filters_dict:
                lot_size_filter = filters_dict['MARKET_LOT_SIZE']
            
            if not lot_size_filter:
                raise ValueError(f"LOT_SIZE filter not found for {TRADE_SYMBOL}.")
            
            min_notional_filter = filters_dict['NOTIONAL']

            current_filters = {
                'minNotional': Decimal(min_notional_filter['minNotional']),
                'minQty': Decimal(lot_size_filter['minQty']),
                'maxQty': Decimal(lot_size_filter['maxQty']),
                'stepSize': Decimal(lot_size_filter['stepSize']),
            }
            
            step_size_str = str(current_filters['stepSize'])
            if '.' in step_size_str:
                current_filters['quantityPrecision'] = len(step_size_str.split('.')[1].rstrip('0'))
            else:
                current_filters['quantityPrecision'] = 0 

            symbol_filters[TRADE_SYMBOL] = current_filters
            logging.info(f"Symbol filters for {TRADE_SYMBOL}: {symbol_filters[TRADE_SYMBOL]}")
            return symbol_filters[TRADE_SYMBOL]
            
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"Failed to get symbol filters: '{str(e)}'")


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
    max_retries = 3
    logging.info("Calculating MACD signal for 1m...")
    
    for attempt in range(max_retries):
        try:
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(
                symbol=TRADE_SYMBOL, 
                interval=Client.KLINE_INTERVAL_1MINUTE,
                limit=100, 
                startTime=start_time
            )
            
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌ Не визначено", "histogram": []}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка EMA", "trend": "❌ Не визначено", "histogram": []}
            
            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "histogram": []}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка Signal", "trend": "❌ Не визначено", "histogram": []}
            
            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка Histogram", "trend": "❌ Не визначено", "histogram": []}

            current_hist = histogram_values[-1]
            
            if current_hist >= 0:
                signal_action = "BUY"
                trend = "🟢 Позитивний"
            else:
                signal_action = "SELL"
                trend = "🔴 Негативний"
                
            return {
                "signal": signal_action, 
                "trend": trend, 
                "histogram": histogram_values, 
                "klines": klines
            }
            
        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": f"Помилка: {str(e)}", "trend": "❌ Не визначено", "histogram": []}
    
    return {"signal": None, "details": "Всі спроби невдалі", "trend": "❌"}


def execute_market_trade(side: str):
    global last_buy_price
    max_retries = 3
    logging.info(f"Executing {side} trade...")

    try:
        filters_info = get_symbol_filters_info()
        min_notional = filters_info['minNotional']
        min_qty = filters_info['minQty']
        max_qty = filters_info['maxQty']
        step_size = filters_info['stepSize']
        qty_precision = filters_info['quantityPrecision']
    except Exception as e:
        logging.error(f"Failed to get filters: {e}")
        return f"Помилка: Не вдалося отримати фільтри: {str(e)}"

    for attempt in range(max_retries):
        try:
            if side == "BUY":
                balance_info = client.get_account()
                usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
                usdc_balance = Decimal(usdc_balance_info['free']) if usdc_balance_info else Decimal('0')

                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = Decimal(current_price_info['price'])

                if usdc_balance < min_notional:
                    return f"Balance {usdc_balance:.2f} USDC is below minimum notional {min_notional:.2f} USDC for BUY."

                amount_to_spend = usdc_balance

                if current_price <= 0:
                    return "Помилка: Поточна ціна BTC є нульовою або від'ємною."

                quantity_btc_raw = amount_to_spend / current_price

                rounding_precision = Decimal('1E-%d' % qty_precision)
                quantity_btc_decimal = (quantity_btc_raw / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
                quantity_decimal = quantity_btc_decimal.quantize(rounding_precision, rounding=ROUND_DOWN)

                if quantity_btc_decimal < min_qty:
                    return f"⚠️ Розрахована кількість ({quantity_btc_decimal:.{qty_precision}f} BTC) замала"

                if quantity_btc_decimal > max_qty:
                    return f"⚠️ Розрахована кількість перевищує максимум {max_qty}"

                calculated_notional = quantity_decimal * current_price
                if calculated_notional < min_notional:
                    return f"⚠️ Сума {calculated_notional:.2f} USDC нижче мінімуму"

                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="BUY",
                    type="MARKET",
                    quantity=f"{quantity_decimal:.{qty_precision}f}"
                )

                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills']) / Decimal(str(filled_qty)) if filled_qty > 0 else Decimal('0')

                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "BUY",
                    "amount": float(filled_qty),
                    "price": float(filled_price)
                }
                last_buy_price = float(filled_price)
                save_trade(trade_data)
                return f"🟢 Купівля: {filled_qty:.8f} BTC за {filled_price:.2f} USDC"

            elif side == "SELL":
                balance_info = client.get_account()
                btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
                btc_balance = Decimal(btc_balance_info['free']) if btc_balance_info else Decimal('0')

                if btc_balance < min_qty:
                    return f"⚠️ Недостатньо BTC. Мінімум: {min_qty}"

                rounding_precision = Decimal('1E-%d' % qty_precision)
                quantity_btc_decimal = (btc_balance / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
                quantity_decimal = quantity_btc_decimal.quantize(rounding_precision, rounding=ROUND_DOWN)

                if quantity_btc_decimal > max_qty:
                    return f"⚠️ Кількість перевищує максимум {max_qty}"

                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = Decimal(current_price_info['price'])

                calculated_notional = quantity_decimal * current_price
                if calculated_notional < min_notional:
                    return f"⚠️ Сума {calculated_notional:.2f} USDC нижче мінімуму"

                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="SELL",
                    type="MARKET",
                    quantity=f"{quantity_decimal:.{qty_precision}f}"
                )

                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills']) / Decimal(str(filled_qty)) if filled_qty > 0 else Decimal('0')

                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "SELL",
                    "amount": float(filled_qty),
                    "price": float(filled_price)
                }
                save_trade(trade_data)
                last_buy_price = None
                return f"🔴 Продаж: {filled_qty:.8f} BTC за {filled_price:.2f} USDC"

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return f"Помилка торгівлі: {str(e)}"

    return f"Не вдалося виконати угоду {side}"


async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    result = await asyncio.to_thread(execute_market_trade, "BUY")
    await update.message.reply_text(result)


async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    result = await asyncio.to_thread(execute_market_trade, "SELL")
    await update.message.reply_text(result)


async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        logging.info("Автотрейдинг вимкнено, виходимо")
        return
    
    logging.info("Запущено перевірку автотрейдингу (1хв)")
    
    result = await asyncio.to_thread(get_macd_signal)
    
    if not result or not result.get("histogram"):
        logging.warning("MACD сигнал не отримано або гістограма порожня")
        return
    
    signal_action = result["signal"]
    histogram_value = result["histogram"][-1] if result["histogram"] else 0
    
    logging.info(f"MACD результат: signal={signal_action}, hist={histogram_value:.4f}")
    
    trade_message = None
    
    if signal_action == "BUY" and histogram_value >= 0:
        logging.info("Виявлено BUY сигнал → виконуємо купівлю")
        trade_message = await asyncio.to_thread(execute_market_trade, "BUY")
    elif signal_action == "SELL" and histogram_value < 0:
        logging.info("Виявлено SELL сигнал → виконуємо продаж")
        trade_message = await asyncio.to_thread(execute_market_trade, "SELL")
    else:
        logging.info("Сигнал не відповідає умовам для угоди")
        return
    
    if trade_message:
        logging.info(f"Угода виконана: {trade_message}")
        price_info = await asyncio.to_thread(client.get_symbol_ticker, symbol=TRADE_SYMBOL)
        current_price = float(price_info['price'])
        
        hist_color = "🟢" if histogram_value >= 0 else "🔴"
        
        report = (
            f"<b>🤖 Автотрейдинг 1хв ({datetime.now().strftime('%H:%M:%S')})</b>\n"
            f"{TRADE_SYMBOL} @ {current_price:.2f}\n"
            f"MACD гістограма: {hist_color} {histogram_value:.4f}\n"
            f"Сигнал: {signal_action}\n"
            f"Результат: {trade_message}"
        )
        
        try:
            await context.bot.send_message(
                chat_id=context.job.data["chat_id"],
                text=report,
                parse_mode='HTML'
            )
            logging.info("Повідомлення про автоугоду успішно надіслано")
        except Exception as e:
            logging.error(f"Помилка надсилання повідомлення про автоугоду: {e}")


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    logging.info(f"Кнопка 'Авто' натиснута → автотрейдинг тепер: {auto_trading_enabled}")

    job_queue = context.application.job_queue
    
    # Видаляємо всі попередні завдання з таким іменем
    old_jobs = job_queue.get_jobs_by_name("auto_trading")
    logging.info(f"Видаляємо {len(old_jobs)} старих завдань автотрейдингу")
    for job in old_jobs:
        job.schedule_removal()

    if auto_trading_enabled:
        logging.info("Запускаємо автотрейдинг (інтервал 60с, перша перевірка через 10с)")
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,
            name="auto_trading",
            data={"chat_id": update.effective_chat.id}
        )
        await update.message.reply_text(
            f"✅ <b>АВТОТРЕЙДИНГ УВІМКНЕНО!</b>\n\n"
            f"⚡ Перевірка кожні {AUTO_TRADE_INTERVAL} секунд\n"
            f"📊 MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL} (1 хв)\n"
            f"📈 Купівля: гістограма ≥ 0\n"
            f"📉 Продаж: гістограма < 0\n\n"
            f"Перша перевірка через 10 секунд...",
            parse_mode='HTML'
        )
    else:
        logging.info("Автотрейдинг вимкнено")
        await update.message.reply_text("⛔ <b>АВТОТРЕЙДИНГ ВИМКНЕНО</b>", parse_mode='HTML')


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Обчислення MACD сигналу (1хв)...")
    result = await asyncio.to_thread(get_macd_signal)
    
    if not result or not result.get("histogram"):
        await update.message.reply_text("❌ Не вдалося отримати сигнал")
        return
    
    price_info = await asyncio.to_thread(client.get_symbol_ticker, symbol=TRADE_SYMBOL)
    current_price = float(price_info['price'])
    
    hist = result["histogram"][-1] if result["histogram"] else 0
    emoji = "🟢" if hist >= 0 else "🔴"
    
    text = (
        f"<b>MACD (1хв) - {TRADE_SYMBOL}</b>\n"
        f"Ціна: {current_price:.2f} USDC\n"
        f"Гістограма: {emoji} {hist:.4f}\n"
        f"Тренд: {result['trend']}\n"
        f"Сигнал: {result['signal'] or 'немає'}"
    )
    
    await update.message.reply_text(text, parse_mode='HTML')


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        balance_info = client.get_account()
        btc = float(next((a['free'] for a in balance_info['balances'] if a['asset'] == 'BTC'), 0))
        usdc = float(next((a['free'] for a in balance_info['balances'] if a['asset'] == 'USDC'), 0))
        await update.message.reply_text(f"💰 Баланс:\nBTC: {btc:.8f}\nUSDC: {usdc:.2f}")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {str(e)}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(client.get_symbol_ticker(symbol=TRADE_SYMBOL)["price"])
        await update.message.reply_text(f"📈 {TRADE_SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {str(e)}")


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("📊 Історія порожня")
        return
    
    lines = ["<b>Останні угоди:</b>"]
    for t in reversed(trade_history[-10:]):
        emoji = "🟢" if t['type'] == 'BUY' else "🔴"
        lines.append(f"{emoji} {t['date']} - {t['type']} {t['amount']:.8f} @ {t['price']:.2f}")
    
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["💰 Баланс", "📈 Ціна"],
        ["📊 MACD", "🤖 Авто"],
        ["🟢 Купити", "🔴 Продати"],
        ["📊 Історія"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        f"<b>Bitcoin Scalping Bot (1хв)</b>\n\n"
        f"Автотрейдинг: {status}\n"
        f"Перевірка: кожні {AUTO_TRADE_INTERVAL} сек\n\n"
        f"Оберіть дію:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


def main():
    load_trade_history()
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Regex("^(💰 Баланс)$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^(📈 Ціна)$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^(📊 MACD)$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^(🤖 Авто)$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^(🟢 Купити)$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^(🔴 Продати)$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^(📊 Історія)$"), show_statistics))

    logging.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    main()
