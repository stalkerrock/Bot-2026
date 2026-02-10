```python
import asyncio
import socket
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from binance import AsyncClient
from binance.exceptions import BinanceAPIException, BinanceRequestException
import os
from datetime import datetime, timedelta
import json
import logging
from decimal import Decimal, ROUND_DOWN
import time

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# Отримання ключів з змінних середовища
API_KEY = os.environ.get('API_KEY')
SECRET_KEY = os.environ.get('SECRET_KEY')
TELEGRAM_API_KEY = os.environ.get('TELEGRAM_API_KEY')

# Перевірка ключів
if not API_KEY or not SECRET_KEY or not TELEGRAM_API_KEY:
    logging.error("Missing environment variables! Check Railway Variables.")
    exit(1)

TRADE_SYMBOL = "BTCUSDC"

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
AUTO_TRADE_INTERVAL = 60  # 1 хвилина для скальпінгу

auto_trading_enabled = False
trade_history = []
TRADE_HISTORY_FILE = "trade_history.json"
last_buy_price = None
prev_histogram_value = None
symbol_filters = {}

# Функції бота
def load_trade_history():
    global trade_history
    logging.info("Loading trade history...")
    try:
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        else:
            trade_history = []
    except Exception as e:
        logging.error(f"Error loading trade history: {e}")
        trade_history = []

def save_trade(trade_data):
    global trade_history
    logging.info(f"Saving trade: {trade_data}")
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving trade history: {e}")

def calculate_ema(prices, period):
    if len(prices) < period:
        return []
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    for price in prices[1:]:
        ema_value = (price * alpha) + (ema[-1] * (1 - alpha))
        ema.append(ema_value)
    return ema

async def get_macd_signal(client: AsyncClient):
    global prev_histogram_value
    max_retries = 3
    logging.info("Calculating MACD signal for 1m timeframe...")
    
    for attempt in range(max_retries):
        try:
            # 1-хвилинний таймфрейм для скальпінгу
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = await client.get_klines(symbol=TRADE_SYMBOL, interval=AsyncClient.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка розрахунку EMA", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка розрахунку Signal line", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка розрахунку Histogram", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            current_hist = histogram_values[-1]
            last_macd_value = macd[-1]
            last_signal_value = signal[-1]
            
            # Визначаємо сигнали
            if current_hist >= 0.0:
                signal_action = "BUY"
                trend = "🟢 Позитивний"
            else:
                signal_action = "SELL"
                trend = "🔴 Негативний"
                
            return {"signal": signal_action, "details": f"DIF {last_macd_value:.4f}, DEA {last_signal_value:.4f}", "trend": trend, "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": f"Помилка: {str(e)}", "trend": "❌ Не визначено", "histogram": [], "klines": []}

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data['binance_client']
    logging.info("MACD signal command triggered")
    await update.message.reply_text("Обчислення MACD сигналу на 1хв таймфреймі...")
    result = await get_macd_signal(client)
    
    if not result or not result.get("histogram"):
        await update.message.reply_text(f"Помилка: {result.get('details', 'Невдалося отримати MACD-сигнал')}")
        return

    try:
        current_price_info = await client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'
        hist_color_emoji = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        response = [
            f"<b>{TRADE_SYMBOL} @ {current_price:.2f} (1m)</b>",
            f"<b>MACD (12,26,9): {hist_color_emoji} {result['histogram'][-1]:.4f}</b>",
            f"Тренд: {result['trend']}",
            f"Сигнал: {result['signal']}" if result['signal'] else "Сигналів для дії не виявлено"
        ]
        await update.message.reply_text("\n".join(response), parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error in macd_signal_command: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data['binance_client']
    logging.info("Getting balance...")
    try:
        balance_info = await client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        
        btc_free = float(btc_balance_info['free']) if btc_balance_info else 0.0
        usdc_free = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        await update.message.reply_text(f"💰 Баланс:\nBTC: {btc_free:.8f}\nUSDC: {usdc_free:.2f}")
    except Exception as e:
        logging.error(f"Error getting balance: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data['binance_client']
    logging.info("Getting price...")
    try:
        price_info = await client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        price = float(price_info['price'])
        await update.message.reply_text(f"📈 Поточна ціна {TRADE_SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error getting price: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Showing statistics...")
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня.")
        return
    
    messages = ["<b>📊 Історія торгів:</b>"]
    for trade in reversed(trade_history[-10:]):
        trade_type = trade['type']
        amount = trade['amount']
        price = trade['price']
        date = trade['date']
        
        trade_value = amount * price
        messages.append(f"{date} - {trade_type} {amount:.8f} BTC за {price:.2f} USDC (Сума: {trade_value:.2f} USDC)")
    
    await update.message.reply_text("\n".join(messages), parse_mode='HTML')

async def execute_market_trade(client: AsyncClient, side: str):
    max_retries = 3
    logging.info(f"Executing {side} trade...")

    for attempt in range(max_retries):
        try:
            if side == "BUY":
                balance_info = await client.get_account()
                usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
                usdc_balance = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
                
                if usdc_balance < 10:  # Мінімум 10 USDC
                    return f"⚠️ Недостатньо USDC. Баланс: {usdc_balance:.2f} USDC"
                    
                price_info = await client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(price_info['price'])
                quantity = usdc_balance / current_price
                
                # Купівля
                order = await client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="BUY",
                    type="MARKET",
                    quantity=f"{quantity:.8f}"
                )
                
                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
                
                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "BUY",
                    "amount": filled_qty,
                    "price": filled_price
                }
                save_trade(trade_data)
                
                logging.info(f"Buy order executed: {trade_data}")
                return f"🟢 Купівля: {filled_qty:.8f} BTC за {filled_price:.2f} USDC"

            elif side == "SELL":
                balance_info = await client.get_account()
                btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
                btc_balance = float(btc_balance_info['free']) if btc_balance_info else 0.0
                
                if btc_balance < 0.0001:  # Мінімум 0.0001 BTC
                    return f"⚠️ Недостатньо BTC. Баланс: {btc_balance:.8f} BTC"
                    
                order = await client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="SELL",
                    type="MARKET",
                    quantity=f"{btc_balance:.8f}"
                )
                
                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
                
                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "SELL",
                    "amount": filled_qty,
                    "price": filled_price
                }
                save_trade(trade_data)
                
                logging.info(f"Sell order executed: {trade_data}")
                return f"🔴 Продаж: {filled_qty:.8f} BTC за {filled_price:.2f} USDC"

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for trade: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return f"Помилка торгівлі: {str(e)}"
    
    return f"Помилка: не вдалося виконати угоду {side}"

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data['binance_client']
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    result = await execute_market_trade(client, "BUY")
    await update.message.reply_text(result)

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data['binance_client']
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    result = await execute_market_trade(client, "SELL")
    await update.message.reply_text(result)

# ФУНКЦІЯ ДЛЯ АВТОТРЕЙДИНГУ
async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    client = context.bot_data['binance_client']
    if not auto_trading_enabled:
        return
    
    logging.info("🔄 Автоперевірка MACD сигналу...")
    
    try:
        result = await get_macd_signal(client)
        
        if not result or not result.get("histogram"):
            logging.error("Не вдалося отримати MACD сигнал")
            return
        
        signal_action = result["signal"]
        
        if signal_action == "BUY":
            logging.info("📈 MACD сигнал: ПОКУПКА (гістограма ≥ 0)")
            trade_message = await execute_market_trade(client, "BUY")
            
            if trade_message:
                current_price_info = await client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(current_price_info['price']) if current_price_info else 'N/A'
                
                response = [
                    f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')}):</b>",
                    f"<b>{TRADE_SYMBOL} @ {current_price:.2f}</b>",
                    f"<b>MACD: 🟢 {result['histogram'][-1]:.4f}</b>",
                    f"Тренд: {result['trend']}",
                    f"Дія: ПОКУПКА",
                    f"Результат: {trade_message}"
                ]
                await context.bot.send_message(chat_id=context.job.chat_id, text="\n".join(response), parse_mode='HTML')
                
        elif signal_action == "SELL":
            logging.info("📉 MACD сигнал: ПРОДАЖ (гістограма < 0)")
            trade_message = await execute_market_trade(client, "SELL")
            
            if trade_message:
                current_price_info = await client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(current_price_info['price']) if current_price_info else 'N/A'
                
                response = [
                    f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')}):</b>",
                    f"<b>{TRADE_SYMBOL} @ {current_price:.2f}</b>",
                    f"<b>MACD: 🔴 {result['histogram'][-1]:.4f}</b>",
                    f"Тренд: {result['trend']}",
                    f"Дія: ПРОДАЖ",
                    f"Результат: {trade_message}"
                ]
                await context.bot.send_message(chat_id=context.job.chat_id, text="\n".join(response), parse_mode='HTML')
                
        else:
            logging.info(f"📊 MACD сигнал: НЕЙТРАЛЬНИЙ ({result['histogram'][-1]:.4f}) - жодних дій")
            
    except Exception as e:
        logging.error(f"Помилка в автотрейдингу: {str(e)}")

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    job_queue = context.application.job_queue
    
    auto_trading_enabled = not auto_trading_enabled
    
    # Видалити всі старі завдання
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
    
    if auto_trading_enabled:
        logging.info("✅ Автотрейдинг УВІМКНЕНО")
        
        # Додати нове завдання для перевірки кожні 60 секунд
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,  # Почати через 10 секунд
            name="auto_trading",
            chat_id=update.effective_chat.id
        )
        
        await update.message.reply_text(
            f"✅ <b>АВТОТРЕЙДИНГ УВІМКНЕНО!</b>\n\n"
            f"⚡ Перевірка кожні {AUTO_TRADE_INTERVAL} секунд\n"
            f"📊 MACD параметри: {MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}\n"
            f"📈 Сигнал ПОКУПКИ: гістограма ≥ 0\n"
            f"📉 Сигнал ПРОДАЖУ: гістограма < 0\n\n"
            f"Перша перевірка через 10 секунд...",
            parse_mode='HTML'
        )
    else:
        logging.info("⛔ Автотрейдинг ВИМКНЕНО")
        await update.message.reply_text("⛔ <b>АВТОТРЕЙДИНГ ВИМКНЕНО</b>", parse_mode='HTML')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting bot...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        f"🔷 <b>Bitcoin Scalping Bot</b>\n\n"
        f"⚡ Таймфрейм: 1 хвилина\n"
        f"📊 MACD: {MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}\n"
        f"🤖 Автотрейдинг: {status}\n"
        f"⏱️ Перевірка: кожні {AUTO_TRADE_INTERVAL} сек\n\n"
        f"<b>Правила торгівлі:</b>\n"
        f"• 🟢 Купівля: MACD гістограма ≥ 0\n"
        f"• 🔴 Продаж: MACD гістограма < 0\n\n"
        f"<b>Оберіть дію:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Refreshing keyboard...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        f"✅ <b>Клавіатуру оновлено!</b>\n"
        f"🤖 Автотрейдинг: {status}\n\n"
        f"Оберіть дію:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def post_init(application: Application) -> None:
    client = await AsyncClient.create(API_KEY, SECRET_KEY)
    application.bot_data['binance_client'] = client

async def main():
    logging.info("Starting main function...")
    load_trade_history()
    
    # Створення Telegram Application
    application = Application.builder().token(TELEGRAM_API_KEY).post_init(post_init).build()
    
    # Додавання обробників команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(MessageHandler(filters.Regex("^💰 Перевірити баланс$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^📈 Ціна BTC$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^📊 MACD сигнал$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^🤖 Автотрейдинг$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^🟢 Купити BTC$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^🔴 Продати BTC$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика торгів$"), show_statistics))
    
    logging.info(f"Application started for BTC scalping on 1m timeframe")
    logging.info(f"Auto-trading interval: {AUTO_TRADE_INTERVAL} seconds")
    
    # Запуск бота
    await application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    asyncio.run(main())
```symbol_filters = {}

# Функції бота
def load_trade_history():
    global trade_history
    logging.info("Loading trade history...")
    try:
        if os.path.exists(TRADE_HISTORY_FILE):
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f)
        else:
            trade_history = []
    except Exception as e:
        logging.error("Error loading trade history: {}".format(e))
        trade_history = []

def save_trade(trade_data):
    global trade_history
    logging.info("Saving trade: {}".format(trade_data))
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error("Error saving trade history: {}".format(e))

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
    max_retries = 3
    logging.info("Calculating MACD signal for 1m timeframe...")
    
    for attempt in range(max_retries):
        try:
            # 1-хвилинний таймфрейм для скальпінгу
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка розрахунку EMA", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка розрахунку Signal line", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка розрахунку Histogram", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            current_hist = histogram_values[-1]
            last_macd_value = macd[-1]
            last_signal_value = signal[-1]
            
            # Визначаємо сигнали
            if current_hist >= 0.0:
                signal_action = "BUY"
                trend = "🟢 Позитивний"
            else:
                signal_action = "SELL"
                trend = "🔴 Негативний"
                
            return {"signal": signal_action, "details": "DIF {:.4f}, DEA {:.4f}".format(last_macd_value, last_signal_value), "trend": trend, "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}

        except Exception as e:
            logging.error("Attempt {}/{} failed: {}".format(attempt + 1, max_retries, str(e)))
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": "Помилка: {}".format(str(e)), "trend": "❌ Не визначено", "histogram": [], "klines": []}

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("MACD signal command triggered")
    await update.message.reply_text("Обчислення MACD сигналу на 1хв таймфреймі...")
    result = get_macd_signal()
    
    if not result or not result.get("histogram"):
        await update.message.reply_text("Помилка: {}".format(result.get('details', 'Невдалося отримати MACD-сигнал')))
        return

    try:
        current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'
        hist_color_emoji = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        response = [
            "<b>{} @ {:.2f} (1m)</b>".format(TRADE_SYMBOL, current_price),
            "<b>MACD (12,26,9): {} {:.4f}</b>".format(hist_color_emoji, result['histogram'][-1]),
            "Тренд: {}".format(result['trend']),
            "Сигнал: {}".format(result['signal']) if result['signal'] else "Сигналів для дії не виявлено"
        ]
        await update.message.reply_text("\\n".join(response), parse_mode='HTML')
    except Exception as e:
        logging.error("Error in macd_signal_command: {}".format(str(e)))
        await update.message.reply_text("Помилка: {}".format(str(e)))

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting balance...")
    try:
        balance_info = client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        
        btc_free = float(btc_balance_info['free']) if btc_balance_info else 0.0
        usdc_free = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        await update.message.reply_text("💰 Баланс:\\nBTC: {:.8f}\\nUSDC: {:.2f}".format(btc_free, usdc_free))
    except Exception as e:
        logging.error("Error getting balance: {}".format(str(e)))
        await update.message.reply_text("Помилка: {}".format(str(e)))

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting price...")
    try:
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        price = float(price_info['price'])
        await update.message.reply_text("📈 Поточна ціна {}: {:.2f} USDC".format(TRADE_SYMBOL, price))
    except Exception as e:
        logging.error("Error getting price: {}".format(str(e)))
        await update.message.reply_text("Помилка: {}".format(str(e)))

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Showing statistics...")
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня.")
        return
    
    messages = ["<b>📊 Історія торгів:</b>"]
    for trade in reversed(trade_history[-10:]):
        trade_type = trade['type']
        amount = trade['amount']
        price = trade['price']
        date = trade['date']
        
        trade_value = amount * price
        messages.append("{} - {} {:.8f} BTC за {:.2f} USDC (Сума: {:.2f} USDC)".format(date, trade_type, amount, price, trade_value))
    
    await update.message.reply_text("\\n".join(messages), parse_mode='HTML')

async def execute_market_trade(side: str):
    max_retries = 3
    logging.info("Executing {} trade...".format(side))

    for attempt in range(max_retries):
        try:
            if side == "BUY":
                balance_info = client.get_account()
                usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
                usdc_balance = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
                
                if usdc_balance < 10:  # Мінімум 10 USDC
                    return "⚠️ Недостатньо USDC. Баланс: {:.2f} USDC".format(usdc_balance)
                    
                price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(price_info['price'])
                quantity = usdc_balance / current_price
                
                # Купівля
                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="BUY",
                    type="MARKET",
                    quantity="{:.8f}".format(quantity)
                )
                
                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
                
                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "BUY",
                    "amount": filled_qty,
                    "price": filled_price
                }
                save_trade(trade_data)
                
                logging.info("Buy order executed: {}".format(trade_data))
                return "🟢 Купівля: {:.8f} BTC за {:.2f} USDC".format(filled_qty, filled_price)

            elif side == "SELL":
                balance_info = client.get_account()
                btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
                btc_balance = float(btc_balance_info['free']) if btc_balance_info else 0.0
                
                if btc_balance < 0.0001:  # Мінімум 0.0001 BTC
                    return "⚠️ Недостатньо BTC. Баланс: {:.8f} BTC".format(btc_balance)
                    
                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="SELL",
                    type="MARKET",
                    quantity="{:.8f}".format(btc_balance)
                )
                
                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
                
                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "SELL",
                    "amount": filled_qty,
                    "price": filled_price
                }
                save_trade(trade_data)
                
                logging.info("Sell order executed: {}".format(trade_data))
                return "🔴 Продаж: {:.8f} BTC за {:.2f} USDC".format(filled_qty, filled_price)

        except Exception as e:
            logging.error("Attempt {}/{} failed for trade: {}".format(attempt + 1, max_retries, str(e)))
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return "Помилка торгівлі: {}".format(str(e))
    
    return "Помилка: не вдалося виконати угоду {}".format(side)

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    result = await asyncio.get_event_loop().run_in_executor(None, execute_market_trade, "BUY")
    await update.message.reply_text(result)

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    result = await asyncio.get_event_loop().run_in_executor(None, execute_market_trade, "SELL")
    await update.message.reply_text(result)

# ФУНКЦІЯ ДЛЯ АВТОТРЕЙДИНГУ (якої не було!)
async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return
    
    logging.info("🔄 Автоперевірка MACD сигналу...")
    
    try:
        result = get_macd_signal()
        
        if not result or not result.get("histogram"):
            logging.error("Не вдалося отримати MACD сигнал")
            return
        
        signal_action = result["signal"]
        
        if signal_action == "BUY":
            logging.info("📈 MACD сигнал: ПОКУПКА (гістограма ≥ 0)")
            trade_message = await asyncio.get_event_loop().run_in_executor(None, execute_market_trade, "BUY")
            
            if trade_message:
                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(current_price_info['price']) if current_price_info else 'N/A'
                
                response = [
                    "<b>🤖 АВТОТРЕЙДИНГ ({}):</b>".format(datetime.now().strftime('%H:%M:%S')),
                    "<b>{} @ {:.2f}</b>".format(TRADE_SYMBOL, current_price),
                    "<b>MACD: 🟢 {:.4f}</b>".format(result['histogram'][-1]),
                    "Тренд: {}".format(result['trend']),
                    "Дія: ПОКУПКА",
                    "Результат: {}".format(trade_message)
                ]
                await context.bot.send_message(chat_id=context.job.chat_id, text="\\n".join(response), parse_mode='HTML')
                
        elif signal_action == "SELL":
            logging.info("📉 MACD сигнал: ПРОДАЖ (гістограма < 0)")
            trade_message = await asyncio.get_event_loop().run_in_executor(None, execute_market_trade, "SELL")
            
            if trade_message:
                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(current_price_info['price']) if current_price_info else 'N/A'
                
                response = [
                    "<b>🤖 АВТОТРЕЙДИНГ ({}):</b>".format(datetime.now().strftime('%H:%M:%S')),
                    "<b>{} @ {:.2f}</b>".format(TRADE_SYMBOL, current_price),
                    "<b>MACD: 🔴 {:.4f}</b>".format(result['histogram'][-1]),
                    "Тренд: {}".format(result['trend']),
                    "Дія: ПРОДАЖ",
                    "Результат: {}".format(trade_message)
                ]
                await context.bot.send_message(chat_id=context.job.chat_id, text="\\n".join(response), parse_mode='HTML')
                
        else:
            logging.info("📊 MACD сигнал: НЕЙТРАЛЬНИЙ ({:.4f}) - жодних дій".format(result['histogram'][-1]))
            
    except Exception as e:
        logging.error("Помилка в автотрейдингу: {}".format(str(e)))

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    job_queue = context.application.job_queue
    
    auto_trading_enabled = not auto_trading_enabled
    
    # Видалити всі старі завдання
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
    
    if auto_trading_enabled:
        logging.info("✅ Автотрейдинг УВІМКНЕНО")
        
        # Додати нове завдання для перевірки кожні 60 секунд
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,  # Почати через 10 секунд
            name="auto_trading",
            chat_id=update.effective_chat.id
        )
        
        await update.message.reply_text(
            "✅ <b>АВТОТРЕЙДИНГ УВІМКНЕНО!</b>\\n\\n"
            "⚡ Перевірка кожні {} секунд\\n"
            "📊 MACD параметри: {}, {}, {}\\n"
            "📈 Сигнал ПОКУПКИ: гістограма ≥ 0\\n"
            "📉 Сигнал ПРОДАЖУ: гістограма < 0\\n\\n"
            "Перша перевірка через 10 секунд...".format(AUTO_TRADE_INTERVAL, MACD_FAST, MACD_SLOW, MACD_SIGNAL),
            parse_mode='HTML'
        )
    else:
        logging.info("⛔ Автотрейдинг ВИМКНЕНО")
        await update.message.reply_text("⛔ <b>АВТОТРЕЙДИНГ ВИМКНЕНО</b>", parse_mode='HTML')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting bot...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        "🔷 <b>Bitcoin Scalping Bot</b>\\n\\n"
        "⚡ Таймфрейм: 1 хвилина\\n"
        "📊 MACD: {}, {}, {}\\n"
        "🤖 Автотрейдинг: {}\\n"
        "⏱️ Перевірка: кожні {} сек\\n\\n"
        "<b>Правила торгівлі:</b>\\n"
        "• 🟢 Купівля: MACD гістограма ≥ 0\\n"
        "• 🔴 Продаж: MACD гістограма < 0\\n\\n"
        "<b>Оберіть дію:</b>".format(MACD_FAST, MACD_SLOW, MACD_SIGNAL, status, AUTO_TRADE_INTERVAL),
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Refreshing keyboard...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        "✅ <b>Клавіатуру оновлено!</b>\\n"
        "🤖 Автотрейдинг: {}\\n\\n"
        "Оберіть дію:".format(status),
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

def main():
    logging.info("Starting main function...")
    load_trade_history()
    
    # Створення Telegram Application
    application = Application.builder().token(TELEGRAM_API_KEY).build()
    
    # Додавання обробників команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(MessageHandler(filters.Regex("^💰 Перевірити баланс$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^📈 Ціна BTC$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^📊 MACD сигнал$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^🤖 Автотрейдинг$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^🟢 Купити BTC$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^🔴 Продати BTC$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика торгів$"), show_statistics))
    
    logging.info("Application started for BTC scalping on 1m timeframe")
    logging.info("Auto-trading interval: {} seconds".format(AUTO_TRADE_INTERVAL))
    
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()    logging.info(f"Saving trade: {trade_data}")
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving trade history: {e}")

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
    max_retries = 3
    logging.info("Calculating MACD signal for 1m timeframe...")
    
    for attempt in range(max_retries):
        try:
            # 1-хвилинний таймфрейм для скальпінгу
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка розрахунку EMA", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка розрахунку Signal line", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка розрахунку Histogram", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            current_hist = histogram_values[-1]
            last_macd_value = macd[-1]
            last_signal_value = signal[-1]
            
            # Визначаємо сигнали
            if current_hist >= 0.0:
                signal_action = "BUY"
                trend = "🟢 Позитивний"
            else:
                signal_action = "SELL"
                trend = "🔴 Негативний"
                
            return {"signal": signal_action, "details": f"DIF {last_macd_value:.4f}, DEA {last_signal_value:.4f}", "trend": trend, "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": f"Помилка: {str(e)}", "trend": "❌ Не визначено", "histogram": [], "klines": []}

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("MACD signal command triggered")
    await update.message.reply_text("Обчислення MACD сигналу на 1хв таймфреймі...")
    result = get_macd_signal()
    
    if not result or not result.get("histogram"):
        await update.message.reply_text(f"Помилка: {result.get('details', 'Невдалося отримати MACD-сигнал')}")
        return

    try:
        current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'
        hist_color_emoji = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        response = [
            f"<b>{TRADE_SYMBOL} @ {current_price:.2f} (1m)</b>",
            f"<b>MACD (12,26,9): {hist_color_emoji} {result['histogram'][-1]:.4f}</b>",
            f"Тренд: {result['trend']}",
            f"Сигнал: {result['signal']}" if result['signal'] else "Сигналів для дії не виявлено"
        ]
        await update.message.reply_text("\n".join(response), parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error in macd_signal_command: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting balance...")
    try:
        balance_info = client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        
        btc_free = float(btc_balance_info['free']) if btc_balance_info else 0.0
        usdc_free = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        await update.message.reply_text(f"💰 Баланс:\nBTC: {btc_free:.8f}\nUSDC: {usdc_free:.2f}")
    except Exception as e:
        logging.error(f"Error getting balance: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting price...")
    try:
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        price = float(price_info['price'])
        await update.message.reply_text(f"📈 Поточна ціна {TRADE_SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error getting price: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Showing statistics...")
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня.")
        return
    
    messages = ["<b>📊 Історія торгів:</b>"]
    for trade in reversed(trade_history[-10:]):
        trade_type = trade['type']
        amount = trade['amount']
        price = trade['price']
        date = trade['date']
        
        trade_value = amount * price
        messages.append(f"{date} - {trade_type} {amount:.8f} BTC за {price:.2f} USDC (Сума: {trade_value:.2f} USDC)")
    
    await update.message.reply_text("\n".join(messages), parse_mode='HTML')

async def execute_market_trade(side: str):
    max_retries = 3
    logging.info(f"Executing {side} trade...")

    for attempt in range(max_retries):
        try:
            if side == "BUY":
                balance_info = client.get_account()
                usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
                usdc_balance = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
                
                if usdc_balance < 10:  # Мінімум 10 USDC
                    return f"⚠️ Недостатньо USDC. Баланс: {usdc_balance:.2f} USDC"
                    
                price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(price_info['price'])
                quantity = usdc_balance / current_price
                
                # Купівля
                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="BUY",
                    type="MARKET",
                    quantity=f"{quantity:.8f}"
                )
                
                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
                
                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "BUY",
                    "amount": filled_qty,
                    "price": filled_price
                }
                save_trade(trade_data)
                
                logging.info(f"Buy order executed: {trade_data}")
                return f"🟢 Купівля: {filled_qty:.8f} BTC за {filled_price:.2f} USDC"

            elif side == "SELL":
                balance_info = client.get_account()
                btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
                btc_balance = float(btc_balance_info['free']) if btc_balance_info else 0.0
                
                if btc_balance < 0.0001:  # Мінімум 0.0001 BTC
                    return f"⚠️ Недостатньо BTC. Баланс: {btc_balance:.8f} BTC"
                    
                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="SELL",
                    type="MARKET",
                    quantity=f"{btc_balance:.8f}"
                )
                
                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
                
                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "type": "SELL",
                    "amount": filled_qty,
                    "price": filled_price
                }
                save_trade(trade_data)
                
                logging.info(f"Sell order executed: {trade_data}")
                return f"🔴 Продаж: {filled_qty:.8f} BTC за {filled_price:.2f} USDC"

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for trade: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return f"Помилка торгівлі: {str(e)}"
    
    return f"Помилка: не вдалося виконати угоду {side}"

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    result = await execute_market_trade("BUY")
    await update.message.reply_text(result)

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    result = await execute_market_trade("SELL")
    await update.message.reply_text(result)

# ФУНКЦІЯ ДЛЯ АВТОТРЕЙДИНГУ (якої не було!)
async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return
    
    logging.info("🔄 Автоперевірка MACD сигналу...")
    
    try:
        result = get_macd_signal()
        
        if not result or not result.get("histogram"):
            logging.error("Не вдалося отримати MACD сигнал")
            return
        
        signal_action = result["signal"]
        
        if signal_action == "BUY":
            logging.info("📈 MACD сигнал: ПОКУПКА (гістограма ≥ 0)")
            trade_message = await execute_market_trade("BUY")
            
            if trade_message:
                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(current_price_info['price']) if current_price_info else 'N/A'
                
                response = [
                    f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')}):</b>",
                    f"<b>{TRADE_SYMBOL} @ {current_price:.2f}</b>",
                    f"<b>MACD: 🟢 {result['histogram'][-1]:.4f}</b>",
                    f"Тренд: {result['trend']}",
                    f"Дія: ПОКУПКА",
                    f"Результат: {trade_message}"
                ]
                await context.bot.send_message(chat_id=context.job.chat_id, text="\n".join(response), parse_mode='HTML')
                
        elif signal_action == "SELL":
            logging.info("📉 MACD сигнал: ПРОДАЖ (гістограма < 0)")
            trade_message = await execute_market_trade("SELL")
            
            if trade_message:
                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = float(current_price_info['price']) if current_price_info else 'N/A'
                
                response = [
                    f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')}):</b>",
                    f"<b>{TRADE_SYMBOL} @ {current_price:.2f}</b>",
                    f"<b>MACD: 🔴 {result['histogram'][-1]:.4f}</b>",
                    f"Тренд: {result['trend']}",
                    f"Дія: ПРОДАЖ",
                    f"Результат: {trade_message}"
                ]
                await context.bot.send_message(chat_id=context.job.chat_id, text="\n".join(response), parse_mode='HTML')
                
        else:
            logging.info(f"📊 MACD сигнал: НЕЙТРАЛЬНИЙ ({result['histogram'][-1]:.4f}) - жодних дій")
            
    except Exception as e:
        logging.error(f"Помилка в автотрейдингу: {str(e)}")

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    job_queue = context.application.job_queue
    
    auto_trading_enabled = not auto_trading_enabled
    
    # Видалити всі старі завдання
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
    
    if auto_trading_enabled:
        logging.info("✅ Автотрейдинг УВІМКНЕНО")
        
        # Додати нове завдання для перевірки кожні 60 секунд
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,  # Почати через 10 секунд
            name="auto_trading",
            chat_id=update.effective_chat.id
        )
        
        await update.message.reply_text(
            f"✅ <b>АВТОТРЕЙДИНГ УВІМКНЕНО!</b>\n\n"
            f"⚡ Перевірка кожні {AUTO_TRADE_INTERVAL} секунд\n"
            f"📊 MACD параметри: {MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}\n"
            f"📈 Сигнал ПОКУПКИ: гістограма ≥ 0\n"
            f"📉 Сигнал ПРОДАЖУ: гістограма < 0\n\n"
            f"Перша перевірка через 10 секунд...",
            parse_mode='HTML'
        )
    else:
        logging.info("⛔ Автотрейдинг ВИМКНЕНО")
        await update.message.reply_text("⛔ <b>АВТОТРЕЙДИНГ ВИМКНЕНО</b>", parse_mode='HTML')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting bot...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        f"🔷 <b>Bitcoin Scalping Bot</b>\n\n"
        f"⚡ Таймфрейм: 1 хвилина\n"
        f"📊 MACD: {MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}\n"
        f"🤖 Автотрейдинг: {status}\n"
        f"⏱️ Перевірка: кожні {AUTO_TRADE_INTERVAL} сек\n\n"
        f"<b>Правила торгівлі:</b>\n"
        f"• 🟢 Купівля: MACD гістограма ≥ 0\n"
        f"• 🔴 Продаж: MACD гістограма < 0\n\n"
        f"<b>Оберіть дію:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Refreshing keyboard...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    
    status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    await update.message.reply_text(
        f"✅ <b>Клавіатуру оновлено!</b>\n"
        f"🤖 Автотрейдинг: {status}\n\n"
        f"Оберіть дію:",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

def main():
    logging.info("Starting main function...")
    load_trade_history()
    
    # Створення Telegram Application
    application = Application.builder().token(TELEGRAM_API_KEY).build()
    
    # Додавання обробників команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(MessageHandler(filters.Regex("^💰 Перевірити баланс$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^📈 Ціна BTC$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^📊 MACD сигнал$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^🤖 Автотрейдинг$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^🟢 Купити BTC$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^🔴 Продати BTC$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика торгів$"), show_statistics))
    
    logging.info(f"Application started for BTC scalping on 1m timeframe")
    logging.info(f"Auto-trading interval: {AUTO_TRADE_INTERVAL} seconds")
    
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    logging.info(f"Saving trade: {trade_data}")
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving trade history: {e}")

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
    max_retries = 3
    logging.info("Calculating MACD signal for 1m timeframe...")
    
    for attempt in range(max_retries):
        try:
            # 1-хвилинний таймфрейм для скальпінгу
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка розрахунку EMA", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка розрахунку Signal line", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка розрахунку Histogram", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            current_hist = histogram_values[-1]
            last_macd_value = macd[-1]
            last_signal_value = signal[-1]
            
            # Визначаємо сигнали
            if current_hist >= 0.0:
                signal_action = "BUY"
                trend = "🟢 Позитивний"
            else:
                signal_action = "SELL"
                trend = "🔴 Негативний"
                
            return {"signal": signal_action, "details": f"DIF {last_macd_value:.4f}, DEA {last_signal_value:.4f}", "trend": trend, "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": f"Помилка: {str(e)}", "trend": "❌ Не визначено", "histogram": [], "klines": []}

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("MACD signal command triggered")
    await update.message.reply_text("Обчислення MACD сигналу на 1хв таймфреймі...")
    result = get_macd_signal()
    
    if not result or not result.get("histogram"):
        await update.message.reply_text(f"Помилка: {result.get('details', 'Невдалося отримати MACD-сигнал')}")
        return

    try:
        current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'
        hist_color_emoji = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        response = [
            f"<b>{TRADE_SYMBOL} @ {current_price:.2f} (1m)</b>",
            f"<b>MACD (12,26,9): {hist_color_emoji} {result['histogram'][-1]:.4f}</b>",
            f"Тренд: {result['trend']}",
            f"Сигнал: {result['signal']}" if result['signal'] else "Сигналів для дії не виявлено"
        ]
        await update.message.reply_text("\n".join(response), parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error in macd_signal_command: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting balance...")
    try:
        balance_info = client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        
        btc_free = float(btc_balance_info['free']) if btc_balance_info else 0.0
        usdc_free = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        await update.message.reply_text(f"💰 Баланс:\nBTC: {btc_free:.8f}\nUSDC: {usdc_free:.2f}")
    except Exception as e:
        logging.error(f"Error getting balance: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting price...")
    try:
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        price = float(price_info['price'])
        await update.message.reply_text(f"📈 Поточна ціна {TRADE_SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error getting price: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Showing statistics...")
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня.")
        return
    
    messages = ["<b>📊 Історія торгів:</b>"]
    for trade in reversed(trade_history[-10:]):
        trade_type = trade['type']
        amount = trade['amount']
        price = trade['price']
        date = trade['date']
        
        trade_value = amount * price
        messages.append(f"{date} - {trade_type} {amount:.8f} BTC за {price:.2f} USDC (Сума: {trade_value:.2f} USDC)")
    
    await update.message.reply_text("\n".join(messages), parse_mode='HTML')

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    
    if auto_trading_enabled:
        await update.message.reply_text("✅ Автотрейдинг увімкнено!")
    else:
        await update.message.reply_text("⛔ Автотрейдинг вимкнено")

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    
    try:
        # Спрощена купівля
        balance_info = client.get_account()
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        usdc_balance = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        if usdc_balance < 10:  # Мінімум 10 USDC
            await update.message.reply_text(f"⚠️ Недостатньо USDC. Баланс: {usdc_balance:.2f} USDC")
            return
            
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(price_info['price'])
        quantity = usdc_balance / current_price
        
        # Проста купівля
        order = client.create_order(
            symbol=TRADE_SYMBOL,
            side="BUY",
            type="MARKET",
            quantity=f"{quantity:.8f}"
        )
        
        filled_qty = sum(float(f['qty']) for f in order['fills'])
        filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
        
        trade_data = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "type": "BUY",
            "amount": filled_qty,
            "price": filled_price
        }
        save_trade(trade_data)
        
        await update.message.reply_text(f"🟢 Купівля: {filled_qty:.8f} BTC за {filled_price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error buying BTC: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    
    try:
        balance_info = client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        btc_balance = float(btc_balance_info['free']) if btc_balance_info else 0.0
        
        if btc_balance < 0.0001:  # Мінімум 0.0001 BTC
            await update.message.reply_text(f"⚠️ Недостатньо BTC. Баланс: {btc_balance:.8f} BTC")
            return
            
        order = client.create_order(
            symbol=TRADE_SYMBOL,
            side="SELL",
            type="MARKET",
            quantity=f"{btc_balance:.8f}"
        )
        
        filled_qty = sum(float(f['qty']) for f in order['fills'])
        filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
        
        trade_data = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "type": "SELL",
            "amount": filled_qty,
            "price": filled_price
        }
        save_trade(trade_data)
        
        await update.message.reply_text(f"🔴 Продаж: {filled_qty:.8f} BTC за {filled_price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error selling BTC: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting bot...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "🔷 Bitcoin Scalping Bot\n\n"
        "⚡ Таймфрейм: 1 хвилина\n"
        "📊 MACD: 12, 26, 9\n"
        "🤖 Автотрейдинг - автоматичні угоди\n"
        "📊 MACD сигнал - перевірка стану\n"
        "🟢 Купити BTC - купівля на весь баланс USDC\n"
        "🔴 Продати BTC - продаж усього BTC\n"
        "📊 Статистика - історія торгів\n\n"
        "Оберіть дію:",
        reply_markup=reply_markup
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Refreshing keyboard...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "✅ Клавіатуру оновлено!\n\nОберіть дію:",
        reply_markup=reply_markup
    )

def main():
    logging.info("Starting main function...")
    load_trade_history()
    
    # Створення Telegram Application
    application = Application.builder().token(TELEGRAM_API_KEY).build()
    
    # Додавання обробників команд - ПРОСТІ РЕГУЛЯРНІ ВИРАЗИ!
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(MessageHandler(filters.Regex("^💰 Перевірити баланс$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^📈 Ціна BTC$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^📊 MACD сигнал$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^🤖 Автотрейдинг$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^🟢 Купити BTC$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^🔴 Продати BTC$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Статистика торгів$"), show_statistics))
    
    logging.info("Application started for BTC scalping on 1m timeframe")
    
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()    logging.info(f"Saving trade: {trade_data}")
    trade_history.append(trade_data)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving trade history: {e}")

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
    max_retries = 3
    logging.info("Calculating MACD signal for 1m timeframe...")
    
    for attempt in range(max_retries):
        try:
            # 1-хвилинний таймфрейм для скальпінгу
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
            close_prices = [float(k[4]) for k in klines]
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                return {"signal": None, "details": "Недостатньо даних", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            if not fast_ema or not slow_ema:
                return {"signal": None, "details": "Помилка розрахунку EMA", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            if not macd or len(macd) < MACD_SIGNAL:
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            if not signal:
                return {"signal": None, "details": "Помилка розрахунку Signal line", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            if not histogram_values:
                return {"signal": None, "details": "Помилка розрахунку Histogram", "trend": "❌ Не визначено", "histogram": [], "klines": klines}

            current_hist = histogram_values[-1]
            last_macd_value = macd[-1]
            last_signal_value = signal[-1]
            
            # Визначаємо сигнали
            if current_hist >= 0.0:
                signal_action = "BUY"
                trend = "🟢 Позитивний"
            else:
                signal_action = "SELL"
                trend = "🔴 Негативний"
                
            return {"signal": signal_action, "details": f"DIF {last_macd_value:.4f}, DEA {last_signal_value:.4f}", "trend": trend, "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": f"Помилка: {str(e)}", "trend": "❌ Не визначено", "histogram": [], "klines": []}

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("MACD signal command triggered")
    await update.message.reply_text("Обчислення MACD сигналу на 1хв таймфреймі...")
    result = get_macd_signal()
    
    if not result or not result.get("histogram"):
        await update.message.reply_text(f"Помилка: {result.get('details', 'Невдалося отримати MACD-сигнал')}")
        return

    try:
        current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'
        hist_color_emoji = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        response = [
            f"<b>{TRADE_SYMBOL} @ {current_price:.2f} (1m)</b>",
            f"<b>MACD (12,26,9): {hist_color_emoji} {result['histogram'][-1]:.4f}</b>",
            f"Тренд: {result['trend']}",
            f"Сигнал: {result['signal']}" if result['signal'] else "Сигналів для дії не виявлено"
        ]
        await update.message.reply_text("\n".join(response), parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error in macd_signal_command: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting balance...")
    try:
        balance_info = client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        
        btc_free = float(btc_balance_info['free']) if btc_balance_info else 0.0
        usdc_free = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        await update.message.reply_text(f"💰 Баланс:\nBTC: {btc_free:.8f}\nUSDC: {usdc_free:.2f}")
    except Exception as e:
        logging.error(f"Error getting balance: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting price...")
    try:
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        price = float(price_info['price'])
        await update.message.reply_text(f"📈 Поточна ціна {TRADE_SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error getting price: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Showing statistics...")
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня.")
        return
    
    messages = ["<b>📊 Історія торгів:</b>"]
    for trade in reversed(trade_history[-10:]):
        trade_type = trade['type']
        amount = trade['amount']
        price = trade['price']
        date = trade['date']
        
        trade_value = amount * price
        messages.append(f"{date} - {trade_type} {amount:.8f} BTC за {price:.2f} USDC (Сума: {trade_value:.2f} USDC)")
    
    await update.message.reply_text("\n".join(messages), parse_mode='HTML')

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    
    if auto_trading_enabled:
        await update.message.reply_text("✅ Автотрейдинг увімкнено!")
    else:
        await update.message.reply_text("⛔ Автотрейдинг вимкнено")

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    
    try:
        # Спрощена купівля
        balance_info = client.get_account()
        usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
        usdc_balance = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
        
        if usdc_balance < 10:  # Мінімум 10 USDC
            await update.message.reply_text(f"⚠️ Недостатньо USDC. Баланс: {usdc_balance:.2f} USDC")
            return
            
        price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(price_info['price'])
        quantity = usdc_balance / current_price
        
        # Проста купівля
        order = client.create_order(
            symbol=TRADE_SYMBOL,
            side="BUY",
            type="MARKET",
            quantity=f"{quantity:.8f}"
        )
        
        filled_qty = sum(float(f['qty']) for f in order['fills'])
        filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
        
        trade_data = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "type": "BUY",
            "amount": filled_qty,
            "price": filled_price
        }
        save_trade(trade_data)
        
        await update.message.reply_text(f"🟢 Купівля: {filled_qty:.8f} BTC за {filled_price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error buying BTC: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    
    try:
        balance_info = client.get_account()
        btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
        btc_balance = float(btc_balance_info['free']) if btc_balance_info else 0.0
        
        if btc_balance < 0.0001:  # Мінімум 0.0001 BTC
            await update.message.reply_text(f"⚠️ Недостатньо BTC. Баланс: {btc_balance:.8f} BTC")
            return
            
        order = client.create_order(
            symbol=TRADE_SYMBOL,
            side="SELL",
            type="MARKET",
            quantity=f"{btc_balance:.8f}"
        )
        
        filled_qty = sum(float(f['qty']) for f in order['fills'])
        filled_price = sum(float(f['price']) * float(f['qty']) for f in order['fills']) / filled_qty if filled_qty > 0 else 0
        
        trade_data = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "type": "SELL",
            "amount": filled_qty,
            "price": filled_price
        }
        save_trade(trade_data)
        
        await update.message.reply_text(f"🔴 Продаж: {filled_qty:.8f} BTC за {filled_price:.2f} USDC")
    except Exception as e:
        logging.error(f"Error selling BTC: {str(e)}")
        await update.message.reply_text(f"Помилка: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting bot...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал (1m)", "🤖 Автотрейдинг (1m)"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "🔷 Bitcoin Scalping Bot\n\n"
        "⚡ Таймфрейм: 1 хвилина\n"
        "📊 MACD: 12, 26, 9\n"
        "🤖 Автотрейдинг - автоматичні угоди\n"
        "📊 MACD сигнал - перевірка стану\n"
        "🟢 Купити BTC - купівля на весь баланс USDC\n"
        "🔴 Продати BTC - продаж усього BTC\n"
        "📊 Статистика - історія торгів\n\n"
        "Оберіть дію:",
        reply_markup=reply_markup
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Refreshing keyboard...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал (1m)", "🤖 Автотрейдинг (1m)"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика торгів"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "✅ Клавіатуру оновлено!\n\nОберіть дію:",
        reply_markup=reply_markup
    )

def main():
    logging.info("Starting main function...")
    load_trade_history()
    
    # Створення Telegram Application
    application = Application.builder().token(TELEGRAM_API_KEY).build()
    
    # Додавання обробників команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(MessageHandler(filters.Regex("^(💰 Перевірити баланс)$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^(📈 Ціна BTC)$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^(📊 MACD сигнал \(1m\))$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^(🤖 Автотрейдинг \(1m\))$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^(🟢 Купити BTC)$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^(🔴 Продати BTC)$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^(📊 Статистика торгів)$"), show_statistics))
    
    logging.info("Application started for BTC scalping on 1m timeframe")
    
    # Запуск бота
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
