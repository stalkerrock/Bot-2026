import logging, json, os, pytz
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from binance.client import Client
import config

# Налаштування логування для відстеження роботи
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

client = Client(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY)

TRADE_SYMBOL = "SOLUSDC"              
TEST_INTERVAL = Client.KLINE_INTERVAL_1MINUTE  
SUPERTREND_PERIOD = 5                 
SUPERTREND_MULTIPLIER = 1.5           
AUTO_TRADE_INTERVAL = 10              
PARIS_TZ = pytz.timezone("Europe/Paris")

auto_trading_enabled = False

def calculate_supertrend_manual(klines, period=5, multiplier=1.5):
    highs, lows, closes = [float(k[2]) for k in klines], [float(k[3]) for k in klines], [float(k[4]) for k in klines]
    tr = []
    for i in range(len(closes)):
        if i == 0: tr.append(highs[i] - lows[i])
        else: tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    atr = [0.0] * len(closes)
    if len(tr) >= period:
        atr[period-1] = sum(tr[:period]) / period
        for i in range(period, len(tr)): atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    basic_ub, basic_lb, final_ub, final_lb, st_line, direction = [0.0]*len(closes), [0.0]*len(closes), [0.0]*len(closes), [0.0]*len(closes), [0.0]*len(closes), [1]*len(closes)
    for i in range(period, len(closes)):
        hl2 = (highs[i] + lows[i]) / 2
        basic_ub[i] = hl2 + (multiplier * atr[i])
        basic_lb[i] = hl2 - (multiplier * atr[i])
        final_ub[i] = basic_ub[i] if (basic_ub[i] < final_ub[i-1] or closes[i-1] > final_ub[i-1]) else final_ub[i-1]
        final_lb[i] = basic_lb[i] if (basic_lb[i] > final_lb[i-1] or closes[i-1] < final_lb[i-1]) else final_lb[i-1]
        if direction[i-1] == 1: direction[i] = 1 if closes[i] >= final_lb[i] else -1
        else: direction[i] = -1 if closes[i] <= final_ub[i] else 1
        st_line[i] = final_lb[i] if direction[i] == 1 else final_ub[i]
    return direction, st_line, closes

def get_supertrend_signal():
    try:
        klines = client.get_klines(symbol=TRADE_SYMBOL, interval=TEST_INTERVAL, limit=100)
        direction, st_line, closes = calculate_supertrend_manual(klines, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
        current_dir, prev_dir = direction[-2], direction[-3]
        action = "BUY" if prev_dir == -1 and current_dir == 1 else ("SELL" if prev_dir == 1 and current_dir == -1 else None)
        return {"action": action}
    except Exception as e:
        logging.error(f"Помилка в SuperTrend: {e}")
        return None

def execute_spot_trade(side: str):
    try:
        acc = client.get_account()
        curr_price = Decimal(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price'])
        if side == "BUY":
            usdc = Decimal(next((a['free'] for a in acc['balances'] if a['asset'] == "USDC"), "0"))
            qty = (usdc / curr_price).quantize(Decimal('0.001'), rounding=ROUND_DOWN)
            if qty > 0: client.create_order(symbol=TRADE_SYMBOL, side="BUY", type="MARKET", quantity=str(qty))
        else:
            sol = Decimal(next((a['free'] for a in acc['balances'] if a['asset'] == "SOL"), "0"))
            if sol > 0: client.create_order(symbol=TRADE_SYMBOL, side="SELL", type="MARKET", quantity=str(sol.quantize(Decimal('0.001'), rounding=ROUND_DOWN)))
        return f"✅ Успішно виконано {side}"
    except Exception as e: return f"❌ Помилка торгівлі: {e}"

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    data = get_supertrend_signal()
    logging.info(f"Перевірка ринку... Сигнал: {data.get('action') if data else 'Помилка'}")
    if data and data.get('action'):
        msg = execute_spot_trade(data['action'])
        await context.bot.send_message(chat_id=context.job.data['chat_id'], text=f"🚀 Сигнал SuperTrend: {data['action']}\n{msg}")

async def enable_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    if not auto_trading_enabled:
        auto_trading_enabled = True
        context.job_queue.run_repeating(auto_job, interval=AUTO_TRADE_INTERVAL, first=1, name="st_auto_job", data={"chat_id": update.effective_chat.id})
        await update.message.reply_text("🚀 Автотрейдинг УВІМКНЕНО!")

async def disable_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = False
    for job in context.job_queue.get_jobs_by_name("st_auto_job"): job.schedule_removal()
    await update.message.reply_text("⛔ Автотрейдинг ВИМКНЕНО.")

def main():
    # Використовуємо ApplicationBuilder для автоматичної ініціалізації черги
    application = ApplicationBuilder().token(config.TELEGRAM_API_KEY).build()
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Бот готовий!", reply_markup=ReplyKeyboardMarkup([["🟢 Увімкнути автотрейдинг"], ["🔴 Вимкнути автотрейдинг"]], resize_keyboard=True))))
    application.add_handler(MessageHandler(filters.Regex(".*Увімкнути.*"), enable_trading))
    application.add_handler(MessageHandler(filters.Regex(".*Вимкнути.*"), disable_trading))
    application.run_polling()

if __name__ == '__main__': main()
