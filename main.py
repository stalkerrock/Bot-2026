import asyncio
import logging
import os
import json
from datetime import datetime, timedelta
from decimal import Decimal

from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from binance.client import Client

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
    alpha = Decimal(2) / Decimal(period + 1)
    ema = [Decimal(prices[0])]
    for price in prices[1:]:
        ema.append(Decimal(price) * alpha + ema[-1] * (Decimal(1) - alpha))
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
    result = await asyncio.to_thread(get_macd_signal)

    if not result:
        await update.message.reply_text("Не вдалося отримати MACD сигнал")
        return

    price_info = await asyncio.to_thread(lambda: client.get_symbol_ticker(symbol=SYMBOL))
    price = float(price_info['price'])

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
        account = await asyncio.to_thread(client.get_account)
        btc = next((a for a in account["balances"] if a["asset"] == "BTC"), {"free": "0"})
        usdc = next((a for a in account["balances"] if a["asset"] == "USDC"), {"free": "0"})
        text = f"💰 Баланс:\nBTC: {float(btc['free']):.8f}\nUSDC: {float(usdc['free']):.2f}"
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Помилка отримання балансу: {str(e)}")


async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price_info = await asyncio.to_thread(lambda: client.get_symbol_ticker(symbol=SYMBOL))
        price = float(price_info['price'])
        await update.message.reply_text(f"📈 {SYMBOL}: {price:.2f} USDC")
    except Exception as e:
        await update.message.reply_text(f"Помилка: {str(e)}")


async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("Історія торгів порожня.")
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


async def execute_trade(side: str):
    try:
        # Отримати фільтри для пари
        symbol_info = await asyncio.to_thread(lambda: client.get_symbol_info(SYMBOL))
        filters = symbol_info['filters']
        lot_size = next(f for f in filters if f['filterType'] == 'LOT_SIZE')
        min_qty = Decimal(lot_size['minQty'])
        max_qty = Decimal(lot_size['maxQty'])
        step_size = Decimal(lot_size['stepSize'])
        min_notional = Decimal(next(f for f in filters if f['filterType'] == 'NOTIONAL')['minNotional'])

        account = await asyncio.to_thread(client.get_account)
        price_info = await asyncio.to_thread(lambda: client.get_symbol_ticker(symbol=SYMBOL))
        current_price = Decimal(price_info['price'])

        if side == "BUY":
            usdc_free = Decimal(next((a['free'] for a in account['balances'] if a['asset'] == 'USDC'), '0'))
            if usdc_free < min_notional:
                return f"⚠️ Недостатньо USDC. Мінімум: {min_notional}"

            qty = usdc_free / current_price
            qty = (qty // step_size) * step_size
            if qty < min_qty:
                return f"⚠️ Кількість менше мінімуму ({min_qty})"

            qty_str = str(qty.quantize(Decimal('1.' + '0' * 8)))
            order = await asyncio.to_thread(lambda: client.create_order(
                symbol=SYMBOL,
                side="BUY",
                type="MARKET",
                quantity=qty_str
            ))

        elif side == "SELL":
            btc_free = Decimal(next((a['free'] for a in account['balances'] if a['asset'] == 'BTC'), '0'))
            if btc_free < min_qty:
                return f"⚠️ Недостатньо BTC. Мінімум: {min_qty}"

            qty = btc_free - (btc_free % step_size)
            qty_str = str(qty.quantize(Decimal('1.' + '0' * 8)))
            order = await asyncio.to_thread(lambda: client.create_order(
                symbol=SYMBOL,
                side="SELL",
                type="MARKET",
                quantity=qty_str
            ))

        filled_qty = sum(Decimal(f['qty']) for f in order['fills'])
        avg_price = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills']) / filled_qty

        trade_data = {
            "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "type": side,
            "amount": float(filled_qty),
            "price": float(avg_price)
        }
        await asyncio.to_thread(save_trade, trade_data)

        emoji = "🟢" if side == "BUY" else "🔴"
        return f"{emoji} {side}: {filled_qty:.8f} BTC за {avg_price:.2f} USDC"

    except Exception as e:
        logging.error(f"Trade error ({side}): {str(e)}")
        return f"Помилка торгівлі: {str(e)}"


async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Спроба купівлі BTC...")
    result = await execute_trade("BUY")
    await update.message.reply_text(result)


async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Спроба продажу BTC...")
    result = await execute_trade("SELL")
    await update.message.reply_text(result)


async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return

    result = await asyncio.to_thread(get_macd_signal)
    
    if not result:
        return

    signal_action = result["signal"]
    
    if signal_action in ["BUY", "SELL"]:
        trade_message = await execute_trade(signal_action)
        
        if "Помилка" not in trade_message:
            price_info = await asyncio.to_thread(lambda: client.get_symbol_ticker(symbol=SYMBOL))
            current_price = float(price_info['price'])
            
            hist_color_emoji = "🟢" if result["current_hist"] >= 0 else "🔴"
            
            response = [
                f"<b>🤖 АВТОТРЕЙДИНГ ({datetime.now().strftime('%H:%M:%S')}):</b>",
                f"<b>{SYMBOL} @ {current_price:.2f}</b>",
                f"<b>MACD: {hist_color_emoji} {result['current_hist']:.4f}</b>",
                f"Тренд: {result['trend']}",
                f"Дія: {signal_action}",
                f"Результат: {trade_message}"
            ]
            await context.bot.send_message(chat_id=context.job.chat_id, text="\n".join(response), parse_mode='HTML')


async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = not auto_trading_enabled
    
    job_queue = context.application.job_queue
    
    # Видалити старі завдання
    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()
    
    if auto_trading_enabled:
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_INTERVAL,
            first=10,
            name="auto_trading",
            chat_id=update.effective_chat.id
        )
        await update.message.reply_text(
            "✅ <b>АВТОТРЕЙДИНГ УВІМКНЕНО!</b>\n\n"
            f"⚡ Перевірка кожні {AUTO_INTERVAL} секунд\n"
            f"📊 MACD параметри: {MACD_FAST}, {MACD_SLOW}, {MACD_SIGNAL}\n"
            "📈 Сигнал ПОКУПКИ: гістограма ≥ 0\n"
            "📉 Сигнал ПРОДАЖУ: гістограма < 0\n\n"
            "Перша перевірка через 10 секунд...",
            parse_mode='HTML'
        )
    else:
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
        "🔷 <b>Bitcoin Scalping Bot</b>\n\n"
        "⚡ Таймфрейм: 1 хвилина\n"
        "📊 MACD: {},{},{}\n"
        "🤖 Автотрейдинг: {}\n"
        "⏱️ Перевірка: кожні {} сек\n\n"
        "<b>Правила торгівлі:</b>\n"
        "• 🟢 Купівля: MACD гістограма ≥ 0\n"
        "• 🔴 Продаж: MACD гістограма < 0\n\n"
        "<b>Оберіть дію:</b>".format(MACD_FAST, MACD_SLOW, MACD_SIGNAL, status, AUTO_INTERVAL),
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
        "✅ <b>Клавіатуру оновлено!</b>\n"
        "🤖 Автотрейдинг: {}\n\n"
        "Оберіть дію:".format(status),
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

def main():
    load_history()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("refresh", refresh))
    app.add_handler(MessageHandler(filters.Regex("^💰 Перевірити баланс$"), get_balance))
    app.add_handler(MessageHandler(filters.Regex("^📈 Ціна BTC$"), get_price))
    app.add_handler(MessageHandler(filters.Regex("^📊 MACD сигнал$"), macd_signal_command))
    app.add_handler(MessageHandler(filters.Regex("^🤖 Автотрейдинг$"), toggle_auto_trading))
    app.add_handler(MessageHandler(filters.Regex("^🟢 Купити BTC$"), buy_btc_command))
    app.add_handler(MessageHandler(filters.Regex("^🔴 Продати BTC$"), sell_btc_command))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика торгів$"), show_statistics))

    logging.info("Application started for BTC scalping on 1m timeframe")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
