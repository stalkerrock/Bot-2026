import asyncio
import logging
import os
import json
import time
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from binance.client import Client
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


# ========== ФУНКЦІЇ З ПРАЦЮЮЧОГО КОДУ ==========

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
    except Exception as e:
        logging.error(f"Error saving trade history: {e}")


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
    """Розрахунок EMA з працюючого коду"""
    if len(prices) < period:
        return []
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    for price in prices[1:]:
        ema_value = (price * alpha) + (ema[-1] * (1 - alpha))
        ema.append(ema_value)
    return ema


def get_macd_signal():
    """MACD з працюючого коду, але адаптований для 1хв"""
    max_retries = 3
    logging.info("Calculating MACD signal for 1m...")
    
    for attempt in range(max_retries):
        try:
            # ВИПРАВЛЕННЯ: Використовуємо 1-хвилинний інтервал
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(
                symbol=TRADE_SYMBOL, 
                interval=Client.KLINE_INTERVAL_1MINUTE,  # ЗМІНА ТУТ!
                limit=100, 
                startTime=start_time
            )
            
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌"}
            
            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка EMA", "trend": "❌"}
            
            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD закоротка", "trend": "❌"}
            
            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка Signal", "trend": "❌"}
            
            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка Histogram", "trend": "❌"}
            
            current_hist = histogram_values[-1]
            
            # ПРОСТЕ ПРАВИЛО: гістограма ≥ 0 = BUY, < 0 = SELL
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
                return {"signal": None, "details": f"Помилка: {str(e)}", "trend": "❌"}
    
    return {"signal": None, "details": "Всі спроби невдалі", "trend": "❌"}


def execute_market_trade(side: str):
    """ТОРГІВЛЯ З ПРАЦЮЮЧОГО КОДУ - ОСНОВНА ФІКСАЦІЯ"""
    global last_buy_price
    max_retries = 3
    logging.info(f"Executing {side} trade...")

    try:
        # ВИПРАВЛЕННЯ: Отримуємо фільтри
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
                # Купівля BTC за всі USDC
                balance_info = client.get_account()
                
                # Шукаємо USDC баланс
                usdc_balance_info = None
                for asset in balance_info['balances']:
                    if asset['asset'] == 'USDC':
                        usdc_balance_info = asset
                        break
                
                usdc_balance = Decimal(usdc_balance_info['free']) if usdc_balance_info else Decimal('0')
                logging.info(f"USDC balance for BUY: {usdc_balance}")

                # Поточна ціна
                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = Decimal(current_price_info['price'])
                logging.info(f"Current price: {current_price}")

                # Перевірка мінімального балансу
                if usdc_balance < min_notional:
                    return f"⚠️ Баланс {usdc_balance:.2f} USDC нижче мінімуму {min_notional:.2f} USDC"

                # Купуємо на весь баланс
                amount_to_spend = usdc_balance

                # Розрахунок кількості BTC
                quantity_btc_raw = amount_to_spend / current_price
                
                # Округлення до stepSize
                rounding_precision = Decimal('1E-%d' % qty_precision)
                quantity_btc_decimal = (quantity_btc_raw / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
                quantity_decimal = quantity_btc_decimal.quantize(rounding_precision, rounding=ROUND_DOWN)

                # Перевірки
                if quantity_btc_decimal < min_qty:
                    return f"⚠️ Кількість {quantity_btc_decimal:.8f} BTC нижче мінімуму {min_qty}"
                
                if quantity_btc_decimal > max_qty:
                    return f"⚠️ Кількість {quantity_btc_decimal:.8f} BTC перевищує максимум {max_qty}"
                
                calculated_notional = quantity_decimal * current_price
                if calculated_notional < min_notional:
                    return f"⚠️ Сума {calculated_notional:.2f} USDC нижче мінімуму {min_notional:.2f}"

                # ВИКОНАННЯ ОРДЕРУ
                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side=Client.SIDE_BUY,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=f"{quantity_decimal:.{qty_precision}f}"
                )

                # Обробка результатів
                filled_qty = sum(Decimal(f['qty']) for f in order['fills'])
                filled_value = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills'])
                filled_price = filled_value / filled_qty if filled_qty > 0 else Decimal('0')

                # Збереження торгівлі
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
                # Продаж всього BTC
                balance_info = client.get_account()
                
                # Шукаємо BTC баланс
                btc_balance_info = None
                for asset in balance_info['balances']:
                    if asset['asset'] == 'BTC':
                        btc_balance_info = asset
                        break
                
                btc_balance = Decimal(btc_balance_info['free']) if btc_balance_info else Decimal('0')
                logging.info(f"BTC balance for SELL: {btc_balance}")

                # Перевірка мінімальної кількості
                if btc_balance < min_qty:
                    return f"⚠️ Баланс {btc_balance:.8f} BTC нижче мінімуму {min_qty}"

                # Округлення
                rounding_precision = Decimal('1E-%d' % qty_precision)
                quantity_btc_decimal = (btc_balance / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
                quantity_decimal = quantity_btc_decimal.quantize(rounding_precision, rounding=ROUND_DOWN)

                # Перевірки
                if quantity_btc_decimal > max_qty:
                    return f"⚠️ Кількість {quantity_btc_decimal:.8f} BTC перевищує максимум {max_qty}"

                # Поточна ціна для перевірки
                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = Decimal(current_price_info['price'])
                
                calculated_notional = quantity_decimal * current_price
                if calculated_notional < min_notional:
                    return f"⚠️ Сума {calculated_notional:.2f} USDC нижче мінімуму {min_notional:.2f}"

                # ВИКОНАННЯ ОРДЕРУ
                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side=Client.SIDE_SELL,
                    type=Client.ORDER_TYPE_MARKET,
                    quantity=f"{quantity_decimal:.{qty_precision}f}"
                )

                # Обробка результатів
                filled_qty = sum(Decimal(f['qty']) for f in order['fills'])
                filled_value = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills'])
                filled_price = filled_value / filled_qty if filled_qty > 0 else Decimal('0')

                # Збереження торгівлі
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
                return f"❌ Помилка: {str(e)}"
    
    return f"❌ Не вдалося виконати угоду {side}"


# ========== ТЕЛЕГРАМ КОМАНДИ ==========

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник кнопки купівлі - ВИПРАВЛЕНА ВЕРСІЯ"""
    logging.info("Buy BTC button pressed")
    await update.message.reply_text("🔄 Спроба купівлі BTC...")
    
    # ВИПРАВЛЕННЯ: Викликаємо sync функцію через thread
    result = await asyncio.to_thread(execute_market_trade, "BUY")
    await update.message.reply_text(result)


async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник кнопки продажу - ВИПРАВЛЕНА ВЕРСІЯ"""
    logging.info("Sell BTC button pressed")
    await update.message.reply_text("🔄 Спроба продажу BTC...")
    
    # ВИПРАВЛЕННЯ: Викликаємо sync функцію через thread
    result = await asyncio.to_thread(execute_market_trade, "SELL")
    await update.message.reply_text(result)


async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    """АВТОТРЕЙДИНГ З ПРАЦЮЮЧОГО КОДУ"""
    if not auto_trading_enabled:
        return
    
    logging.info("🔄 Автоперевірка MACD...")
    
    # Отримуємо сигнал
    result = await asyncio.to_thread(get_macd_signal)
    
    if not result or not result.get("histogram"):
        logging.error("Не вдалося отримати MACD")
        return
    
    signal_action = result["signal"]
    
    if signal_action == "BUY":
        logging.info("📈 MACD сигнал: ПОКУПКА")
        trade_message = await asyncio.to_thread(execute_market_trade, "BUY")
    elif signal_action == "SELL":
        logging.info("📉 MACD сигнал: ПРОДАЖ")
        trade_message = await asyncio.to_thread(execute_market_trade, "SELL")
    else:
        return
    
    if trade_message:
        # Отримуємо поточну ціну
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(price_info['price']) if price_info else 0
        
        hist_color = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        report = (
            f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')})</b>\n"
            f"📊 {TRADE_SYMBOL} @ {current_price:.2f}\n"
            f"📈 MACD: {hist_color} {result['histogram'][-1]:.4f}\n"
            f"📢 Сигнал: {signal_action}\n"
            f"💼 Результат: {trade_message}"
        )
        
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=report,
            parse_mode='HTML'
        )


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Увімкнення автотрейдингу - ВИПРАВЛЕНА ВЕРСІЯ"""
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    
    job_queue = context.application.job_queue
    
    # Видаляємо старі завдання
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
    
    if auto_trading_enabled:
        # ВИПРАВЛЕННЯ: Правильний запуск завдання
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,  # Перша перевірка через 10 сек
            name="auto_trading",
            chat_id=update.effective_chat.id
        )
        
        await update.message.reply_text(
            f"✅ <b>АВТОТРЕЙДИНГ УВІМКНЕНО!</b>\n\n"
            f"⚡ Перевірка кожні {AUTO_TRADE_INTERVAL} секунд\n"
            f"📊 MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n"
            f"📈 Купівля: гістограма ≥ 0\n"
            f"📉 Продаж: гістограма < 0\n\n"
            f"Перша перевірка через 10 секунд...",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("⛔ <b>АВТОТРЕЙДИНГ ВИМКНЕНО</b>", parse_mode='HTML')


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ MACD сигналу"""
    logging.info("MACD button pressed")
    await update.message.reply_text("📊 Отримання MACD сигналу...")
    
    result = await asyncio.to_thread(get_macd_signal)
    
    if not result or not result.get("histogram"):
        await update.message.reply_text("❌ Не вдалося отримати сигнал")
        return
    
    # Поточна ціна
    price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
    current_price = float(price_info['price']) if price_info else 0
    
    hist_color = "🟢" if result["histogram"][-1] >= 0 else "🔴"
    
    message = (
        f"<b>📊 MACD Сигнал (1хв)</b>\n\n"
        f"🔹 Пара: {TRADE_SYMBOL}\n"
        f"🔹 Ціна: {current_price:.2f} USDC\n"
        f"🔹 MACD: {hist_color} {result['histogram'][-1]:.4f}\n"
        f"🔹 Тренд: {result['trend']}\n"
        f"🔹 Сигнал: <b>{result['signal'] or 'НЕЙТРАЛЬНО'}</b>\n"
        f"🔹 Параметри: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}"
    )
    
    await update.message.reply_text(message, parse_mode='HTML')


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Баланс"""
    try:
        balance_info = client.get_account()
        
        btc_balance = 0.0
        usdc_balance = 0.0
        
        for asset in balance_info['balances']:
            if asset['asset'] == 'BTC':
                btc_balance = float(asset['free'])
            elif asset['asset'] == 'USDC':
                usdc_balance = float(asset['free'])
        
        # Поточна ціна
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(price_info['price'])
        
        btc_value = btc_balance * current_price
        total_value = btc_value + usdc_balance
        
        message = (
            f"<b>💰 Баланс</b>\n\n"
            f"🔹 BTC: {btc_balance:.8f} (≈ {btc_value:.2f} USDC)\n"
            f"🔹 USDC: {usdc_balance:.2f}\n"
            f"🔹 Загалом: {total_value:.2f} USDC\n\n"
            f"<i>Ціна BTC: {current_price:.2f} USDC</i>"
        )
        
        await update.message.reply_text(message, parse_mode='HTML')
        
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {str(e)}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ціна"""
    try:
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(price_info['price'])
        await update.message.reply_text(f"📈 {TRADE_SYMBOL}: {current_price:.2f} USDC")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {str(e)}")


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика"""
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня")
        return
    
    lines = ["<b>📊 Останні угоди:</b>"]
    for trade in reversed(trade_history[-10:]):
        emoji = "🟢" if trade['type'] == 'BUY' else "🔴"
        value = trade['amount'] * trade['price']
        lines.append(f"{emoji} {trade['date']} - {trade['type']} {trade['amount']:.8f} BTC @ {trade['price']:.2f} (≈{value:.2f} USDC)")
    
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт бота"""
    keyboard = [
        ["💰 Баланс", "📈 Ціна"],
        ["📊 MACD", "🤖 Авто"],
        ["🟢 Купити", "🔴 Продати"],
        ["📊 Історія"]
    ]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    auto_status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    message = (
        f"<b>🤖 Bitcoin Scalping Bot</b>\n\n"
        f"⚡ Таймфрейм: 1 хвилина\n"
        f"📊 MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n"
        f"🤖 Автотрейдинг: {auto_status}\n"
        f"⏱️ Перевірка: кожні {AUTO_TRADE_INTERVAL} сек\n\n"
        f"<b>Оберіть дію:</b>"
    )
    
    await update.message.reply_text(message, reply_markup=reply_markup, parse_mode='HTML')


def main():
    """Запуск бота"""
    load_trade_history()
    
    logging.info("Starting Bitcoin Scalping Bot...")
    logging.info(f"Symbol: {TRADE_SYMBOL}")
    logging.info(f"MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}")
    logging.info(f"Auto-trading interval: {AUTO_TRADE_INTERVAL}s")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Команди
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refresh", start))
    
    # Кнопки - ВИПРАВЛЕНІ РЕГУЛЯРНІ ВИРАЗИ
    app.add_handler(MessageHandler(filters.Regex(r'^💰 Баланс$'), get_balance))
    app.add_handler(MessageHandler(filters.Regex(r'^📈 Ціна$'), get_price))
    app.add_handler(MessageHandler(filters.Regex(r'^📊 MACD$'), macd_signal_command))
    app.add_handler(MessageHandler(filters.Regex(r'^🤖 Авто$'), toggle_auto_trading))
    app.add_handler(MessageHandler(filters.Regex(r'^🟢 Купити$'), buy_btc_command))
    app.add_handler(MessageHandler(filters.Regex(r'^🔴 Продати$'), sell_btc_command))
    app.add_handler(MessageHandler(filters.Regex(r'^📊 Історія$'), show_statistics))
    
    # Неправильні команди
    async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ Невідома команда. Натисніть /start")
    
    app.add_handler(MessageHandler(filters.ALL, unknown))
    
    logging.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
