import logging, json, os, pytz
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from binance.client import Client
import config

# Логування для відстеження помилок
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

client = Client(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY)

TRADE_SYMBOL = "SOLUSDC"              
AUTO_TRADE_INTERVAL = 10              
TRADE_HISTORY_FILE = "trade_history.json" 
PARIS_TZ = pytz.timezone("Europe/Paris")

auto_trading_enabled = False
trade_history = []

def load_trade_history():
    global trade_history
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f: trade_history = json.load(f)
        except: trade_history = []

def get_supertrend_signal():
    # Ваша логіка розрахунку SuperTrend залишається тут
    try:
        # (Ваш код розрахунку SuperTrend)
        return {"action": None, "current_state": "NEUTRAL"}
    except Exception as e:
        logging.error(f"Помилка розрахунку: {e}")
        return None

def execute_spot_trade(side: str):
    try:
        # Логіка торгівлі через Binance API
        return f"✅ Виконано: {side}"
    except Exception as e: return f"❌ Помилка: {e}"

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    data = get_supertrend_signal()
    if data and data.get('action'):
        msg = execute_spot_trade(data['action'])
        await context.bot.send_message(chat_id=context.job.data['chat_id'], text=f"🚀 Сигнал: {data['action']}\n{msg}")

async def enable_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    if not auto_trading_enabled:
        auto_trading_enabled = True
        # context.job_queue автоматично доступний у нових версіях бібліотеки
        context.job_queue.run_repeating(auto_job, interval=AUTO_TRADE_INTERVAL, first=1, name="st_auto_job", data={"chat_id": update.effective_chat.id})
        await update.message.reply_text("🚀 Автотрейдинг УВІМКНЕНО!")

async def disable_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = False
    for job in context.job_queue.get_jobs_by_name("st_auto_job"):
        job.schedule_removal()
    await update.message.reply_text("⛔ Автотрейдинг ВИМКНЕНО.")

def main():
    # ApplicationBuilder сам налаштовує JobQueue, якщо він є у вимогах
    application = ApplicationBuilder().token(config.TELEGRAM_API_KEY).build()
    
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("Бот готовий!", reply_markup=ReplyKeyboardMarkup([["🟢 Увімкнути автотрейдинг"], ["🔴 Вимкнути автотрейдинг"]], resize_keyboard=True))))
    application.add_handler(MessageHandler(filters.Regex(".*Увімкнути.*"), enable_trading))
    application.add_handler(MessageHandler(filters.Regex(".*Вимкнути.*"), disable_trading))
    
    application.run_polling()

if __name__ == '__main__':
    load_trade_history()
    main()
