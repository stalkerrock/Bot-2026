import asyncio
import logging
import os
import json
import time
from datetime import datetime, timedelta

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from binance.client import Client

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
SYMBOL = "BTCUSDC"

# Налаштування
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
AUTO_INTERVAL = 60  # 60 секунд = 1 хвилина

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
            logging.error(f"Cannot load history: {e}")
            trade_history = []


def save_trade(data):
    trade_history.append(data)
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=2)
    except Exception as e:
        logging.error(f"Cannot save history: {e}")


def calculate_ema(prices, period):
    """Розрахунок експоненційного ковзного середнього"""
    if len(prices) < period:
        return []
    
    alpha = 2 / (period + 1)
    ema = [prices[0]]
    
    for price in prices[1:]:
        ema_value = price * alpha + ema[-1] * (1 - alpha)
        ema.append(ema_value)
    
    return ema


def get_macd_signal():
    """Отримання MACD сигналу"""
    try:
        # Отримання даних за останні 100 хвилин (1хв інтервал)
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=Client.KLINE_INTERVAL_1MINUTE,
            limit=100
        )
        
        if not klines:
            logging.error("No klines data received")
            return None
            
        closes = [float(k[4]) for k in klines]
        
        # Перевірка достатності даних
        if len(closes) < MACD_SLOW:
            logging.warning(f"Not enough data for MACD: {len(closes)} < {MACD_SLOW}")
            return None
        
        # Розрахунок EMA
        fast_ema = calculate_ema(closes, MACD_FAST)
        slow_ema = calculate_ema(closes, MACD_SLOW)
        
        # Визначення довжини для MACD
        min_len = min(len(fast_ema), len(slow_ema))
        
        # Розрахунок MACD лінії
        macd_line = [fast_ema[i] - slow_ema[i] for i in range(min_len)]
        
        # Розрахунок сигнальної лінії
        if len(macd_line) < MACD_SIGNAL:
            logging.warning(f"MACD line too short: {len(macd_line)} < {MACD_SIGNAL}")
            return None
            
        signal_line = calculate_ema(macd_line, MACD_SIGNAL)
        
        # Останні значення
        current_macd = macd_line[-1]
        current_signal = signal_line[-1]
        histogram = current_macd - current_signal
        
        logging.info(f"MACD: {current_macd:.4f}, Signal: {current_signal:.4f}, Histogram: {histogram:.4f}")
        
        # Визначення сигналу
        return "BUY" if histogram >= 0 else "SELL"
        
    except Exception as e:
        logging.error(f"MACD calculation error: {str(e)}")
        return None


async def execute_trade(side: str):
    """Виконання торгової операції"""
    try:
        # Отримання балансу
        account = client.get_account()
        
        if side == "BUY":
            # Купівля BTC за USDC
            usdc_balance = 0.0
            for balance in account['balances']:
                if balance['asset'] == 'USDC':
                    usdc_balance = float(balance['free'])
                    break
            
            logging.info(f"BUY attempt - USDC balance: {usdc_balance:.2f}")
            
            # Перевірка мінімального балансу
            if usdc_balance < 10:
                return f"⚠️ Недостатньо USDC. Баланс: {usdc_balance:.2f} USDC (мінімум 10 USDC)"
            
            # Отримання поточної ціни
            price_info = client.get_symbol_ticker(symbol=SYMBOL)
            current_price = float(price_info['price'])
            
            # Розрахунок кількості
            quantity = usdc_balance / current_price
            
            # Виконання ордеру
            order = client.create_order(
                symbol=SYMBOL,
                side=Client.SIDE_BUY,
                type=Client.ORDER_TYPE_MARKET,
                quantity=f"{quantity:.8f}"
            )
            
            # Обробка результатів
            filled_qty = sum(float(fill['qty']) for fill in order['fills'])
            filled_value = sum(float(fill['price']) * float(fill['qty']) for fill in order['fills'])
            avg_price = filled_value / filled_qty if filled_qty > 0 else current_price
            
            # Збереження торгівлі
            trade_data = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "BUY",
                "amount": filled_qty,
                "price": avg_price
            }
            save_trade(trade_data)
            
            return f"🟢 Купівля: {filled_qty:.8f} BTC за {avg_price:.2f} USDC"
            
        elif side == "SELL":
            # Продаж BTC
            btc_balance = 0.0
            for balance in account['balances']:
                if balance['asset'] == 'BTC':
                    btc_balance = float(balance['free'])
                    break
            
            logging.info(f"SELL attempt - BTC balance: {btc_balance:.8f}")
            
            # Перевірка мінімального балансу
            if btc_balance < 0.0001:
                return f"⚠️ Недостатньо BTC. Баланс: {btc_balance:.8f} BTC (мінімум 0.0001 BTC)"
            
            # Отримання поточної ціни
            price_info = client.get_symbol_ticker(symbol=SYMBOL)
            current_price = float(price_info['price'])
            
            # Перевірка мінімальної суми
            min_notional = btc_balance * current_price
            if min_notional < 10:  # Binance мінімум
                return f"⚠️ Сума замала: {min_notional:.2f} USDC (мінімум 10 USDC)"
            
            # Виконання ордеру
            order = client.create_order(
                symbol=SYMBOL,
                side=Client.SIDE_SELL,
                type=Client.ORDER_TYPE_MARKET,
                quantity=f"{btc_balance:.8f}"
            )
            
            # Обробка результатів
            filled_qty = sum(float(fill['qty']) for fill in order['fills'])
            filled_value = sum(float(fill['price']) * float(fill['qty']) for fill in order['fills'])
            avg_price = filled_value / filled_qty if filled_qty > 0 else current_price
            
            # Збереження торгівлі
            trade_data = {
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": "SELL",
                "amount": filled_qty,
                "price": avg_price
            }
            save_trade(trade_data)
            
            return f"🔴 Продаж: {filled_qty:.8f} BTC за {avg_price:.2f} USDC"
            
    except Exception as e:
        logging.error(f"Trade execution error ({side}): {str(e)}")
        return f"❌ Помилка: {str(e)}"


async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник кнопки 'Купити'"""
    logging.info("Buy BTC button pressed")
    await update.message.reply_text("🔄 Спроба купівлі BTC...")
    
    # Запускаємо торгівлю в окремому потоці
    result = await asyncio.to_thread(execute_trade, "BUY")
    await update.message.reply_text(result)


async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник кнопки 'Продати'"""
    logging.info("Sell BTC button pressed")
    await update.message.reply_text("🔄 Спроба продажу BTC...")
    
    # Запускаємо торгівлю в окремому потоці
    result = await asyncio.to_thread(execute_trade, "SELL")
    await update.message.reply_text(result)


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Увімкнення/вимкнення автотрейдингу"""
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    
    logging.info(f"Auto-trading toggled: {auto_trading_enabled}")
    
    # Отримуємо чергу завдань
    job_queue = context.application.job_queue
    
    # Видаляємо всі попередні завдання автотрейдингу
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
    
    if auto_trading_enabled:
        # Додаємо нове завдання з перевіркою кожні 60 секунд
        job_queue.run_repeating(
            check_and_trade,
            interval=AUTO_INTERVAL,
            first=10,  # Почати через 10 секунд
            name="auto_trading",
            chat_id=update.effective_chat.id
        )
        await update.message.reply_text(
            f"✅ <b>Автотрейдинг увімкнено!</b>\n\n"
            f"⚡ Перевірка кожні {AUTO_INTERVAL} секунд\n"
            f"📊 MACD параметри: {MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}\n"
            f"📈 Сигнал ПОКУПКИ: гістограма ≥ 0\n"
            f"📉 Сигнал ПРОДАЖУ: гістограма < 0\n\n"
            f"Перша перевірка через 10 секунд...",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text("⛔ <b>Автотрейдинг вимкнено</b>", parse_mode='HTML')


async def check_and_trade(context: ContextTypes.DEFAULT_TYPE):
    """Функція для автоматичної перевірки та торгівлі"""
    if not auto_trading_enabled:
        return
    
    logging.info("🔄 Автоматична перевірка MACD...")
    
    try:
        # Отримуємо MACD сигнал
        signal = await asyncio.to_thread(get_macd_signal)
        
        if not signal:
            logging.warning("Не вдалося отримати MACD сигнал")
            return
        
        # Отримуємо поточну ціну
        price_info = client.get_symbol_ticker(symbol=SYMBOL)
        current_price = float(price_info['price'])
        
        logging.info(f"Автосигнал: {signal}, Ціна: {current_price:.2f}")
        
        # Виконуємо угоду
        result = await asyncio.to_thread(execute_trade, signal)
        
        # Відправляємо звіт в чат
        emoji = "🟢" if signal == "BUY" else "🔴"
        report = (
            f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')})</b>\n"
            f"📊 {SYMBOL} @ {current_price:.2f}\n"
            f"📈 Сигнал: {emoji} {signal}\n"
            f"💼 Результат: {result}"
        )
        
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=report,
            parse_mode='HTML'
        )
        
    except Exception as e:
        logging.error(f"Помилка в автотрейдингу: {str(e)}")


async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ поточного MACD сигналу"""
    logging.info("MACD signal button pressed")
    await update.message.reply_text("📊 Отримання MACD сигналу...")
    
    # Отримуємо сигнал
    signal = await asyncio.to_thread(get_macd_signal)
    
    if signal is None:
        await update.message.reply_text("❌ Не вдалося отримати MACD сигнал")
        return
    
    # Отримуємо поточну ціну
    try:
        price_info = client.get_symbol_ticker(symbol=SYMBOL)
        current_price = float(price_info['price'])
    except Exception as e:
        logging.error(f"Price error: {str(e)}")
        current_price = 0
    
    emoji = "🟢" if signal == "BUY" else "🔴"
    message = (
        f"<b>📊 MACD Сигнал (1хв)</b>\n\n"
        f"🔹 Пара: {SYMBOL}\n"
        f"🔹 Ціна: {current_price:.2f} USDC\n"
        f"🔹 Сигнал: {emoji} <b>{signal}</b>\n"
        f"🔹 Параметри: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n\n"
        f"<i>Правила:</i>\n"
        f"• 🟢 {signal} якщо гістограма ≥ 0\n"
        f"• 🔴 {signal} якщо гістограма < 0"
    )
    
    await update.message.reply_text(message, parse_mode='HTML')


async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ балансу"""
    logging.info("Balance button pressed")
    
    try:
        account = client.get_account()
        
        btc_balance = 0.0
        usdc_balance = 0.0
        
        for balance in account['balances']:
            if balance['asset'] == 'BTC':
                btc_balance = float(balance['free'])
            elif balance['asset'] == 'USDC':
                usdc_balance = float(balance['free'])
        
        # Отримуємо поточну ціну для розрахунку загального балансу
        price_info = client.get_symbol_ticker(symbol=SYMBOL)
        current_price = float(price_info['price'])
        
        btc_value = btc_balance * current_price
        total_value = btc_value + usdc_balance
        
        message = (
            f"<b>💰 Баланс рахунку</b>\n\n"
            f"🔹 BTC: {btc_balance:.8f} (≈ {btc_value:.2f} USDC)\n"
            f"🔹 USDC: {usdc_balance:.2f}\n"
            f"🔹 Загалом: {total_value:.2f} USDC\n\n"
            f"<i>Ціна BTC: {current_price:.2f} USDC</i>"
        )
        
        await update.message.reply_text(message, parse_mode='HTML')
        
    except Exception as e:
        logging.error(f"Balance error: {str(e)}")
        await update.message.reply_text(f"❌ Помилка: {str(e)}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ поточної ціни"""
    logging.info("Price button pressed")
    
    try:
        price_info = client.get_symbol_ticker(symbol=SYMBOL)
        current_price = float(price_info['price'])
        
        # Отримуємо зміну ціни за останню годину
        klines = client.get_klines(
            symbol=SYMBOL,
            interval=Client.KLINE_INTERVAL_1HOUR,
            limit=2
        )
        
        if len(klines) >= 2:
            prev_price = float(klines[0][4])
            change = ((current_price - prev_price) / prev_price) * 100
            change_emoji = "📈" if change >= 0 else "📉"
            change_text = f"{change_emoji} {change:+.2f}% за годину"
        else:
            change_text = ""
        
        message = (
            f"<b>📊 Поточна ціна</b>\n\n"
            f"🔹 {SYMBOL}\n"
            f"🔹 Ціна: <b>{current_price:.2f} USDC</b>\n"
            f"🔹 {change_text}"
        )
        
        await update.message.reply_text(message, parse_mode='HTML')
        
    except Exception as e:
        logging.error(f"Price error: {str(e)}")
        await update.message.reply_text(f"❌ Помилка: {str(e)}")


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показ статистики торгів"""
    logging.info("Statistics button pressed")
    
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня")
        return
    
    # Обмежуємо до останніх 10 угод
    recent_trades = trade_history[-10:]
    
    lines = ["<b>📊 Останні угоди:</b>\n"]
    
    for trade in reversed(recent_trades):
        trade_type = trade['type']
        amount = trade['amount']
        price = trade['price']
        date = trade['date']
        value = amount * price
        
        emoji = "🟢" if trade_type == "BUY" else "🔴"
        lines.append(f"{emoji} {date} - {trade_type} {amount:.8f} BTC @ {price:.2f} (≈{value:.2f} USDC)")
    
    # Додаємо загальну статистику
    if trade_history:
        total_trades = len(trade_history)
        buy_count = len([t for t in trade_history if t['type'] == 'BUY'])
        sell_count = len([t for t in trade_history if t['type'] == 'SELL'])
        
        lines.append(f"\n<b>📈 Статистика:</b>")
        lines.append(f"Усього угод: {total_trades}")
        lines.append(f"Купівель: {buy_count}")
        lines.append(f"Продажів: {sell_count}")
    
    await update.message.reply_text("\n".join(lines), parse_mode='HTML')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда старту бота"""
    logging.info("Start command received")
    
    # Створюємо клавіатуру
    keyboard = [
        ["💰 Баланс", "📈 Ціна"],
        ["📊 MACD", "🤖 Авто"],
        ["🟢 Купити", "🔴 Продати"],
        ["📊 Історія"]
    ]
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # Статус автотрейдингу
    auto_status = "🟢 УВІМКНЕНО" if auto_trading_enabled else "🔴 ВИМКНЕНО"
    
    # Повідомлення привітання
    welcome_message = (
        f"<b>🤖 Bitcoin Scalping Bot</b>\n\n"
        f"⚡ Таймфрейм: 1 хвилина\n"
        f"📊 MACD: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}\n"
        f"🤖 Автотрейдинг: {auto_status}\n"
        f"⏱️ Перевірка: кожні {AUTO_INTERVAL} сек\n\n"
        f"<b>Доступні команди:</b>\n"
        f"• 💰 Баланс - перевірка балансу\n"
        f"• 📈 Ціна - поточна ціна BTC\n"
        f"• 📊 MACD - поточний сигнал\n"
        f"• 🤖 Авто - увімк/вимк автотрейдинг\n"
        f"• 🟢 Купити - купити BTC\n"
        f"• 🔴 Продати - продати BTC\n"
        f"• 📊 Історія - статистика торгів\n\n"
        f"<i>Оберіть дію з клавіатури ↓</i>"
    )
    
    await update.message.reply_text(
        welcome_message,
        reply_markup=reply_markup,
        parse_mode='HTML'
    )


def main():
    """Головна функція запуску бота"""
    # Завантажуємо історію торгів
    load_history()
    
    logging.info("Starting Bitcoin Scalping Bot...")
    logging.info(f"Symbol: {SYMBOL}")
    logging.info(f"MACD parameters: {MACD_FAST}/{MACD_SLOW}/{MACD_SIGNAL}")
    logging.info(f"Auto-trading interval: {AUTO_INTERVAL} seconds")
    
    # Створюємо додаток
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Реєструємо обробники команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refresh", start))  # Додаємо refresh як команду
    
    # Реєструємо обробники кнопок
    app.add_handler(MessageHandler(filters.Regex(r"^💰 Баланс$"), get_balance))
    app.add_handler(MessageHandler(filters.Regex(r"^📈 Ціна$"), get_price))
    app.add_handler(MessageHandler(filters.Regex(r"^📊 MACD$"), macd_signal_command))
    app.add_handler(MessageHandler(filters.Regex(r"^🤖 Авто$"), toggle_auto_trading))
    app.add_handler(MessageHandler(filters.Regex(r"^🟢 Купити$"), buy_btc_command))
    app.add_handler(MessageHandler(filters.Regex(r"^🔴 Продати$"), sell_btc_command))
    app.add_handler(MessageHandler(filters.Regex(r"^📊 Історія$"), show_statistics))
    
    # Обробник для невідомих команд
    async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ Невідома команда. Натисніть /start для отримання меню.")
    
    app.add_handler(MessageHandler(filters.ALL, unknown))
    
    # Запускаємо бота
    logging.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
