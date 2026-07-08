import logging, json, os, pytz
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from binance.client import Client
import config

# Налаштування логування для відстеження роботи у Railway
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

client = Client(config.BINANCE_API_KEY, config.BINANCE_SECRET_KEY)

TRADE_SYMBOL = "SOLUSDC"              
TEST_INTERVAL = Client.KLINE_INTERVAL_1HOUR   # Змінено на 1-годинні свічки
SUPERTREND_PERIOD = 20                 # Період 20
SUPERTREND_MULTIPLIER = 5.0           # Множник 5.0
AUTO_TRADE_INTERVAL = 60              # Перевірка ринку кожні 60 секунд (1 хвилина)
PARIS_TZ = pytz.timezone("Europe/Paris")
TRADE_HISTORY_FILE = "trade_history.json"

auto_trading_enabled = False
last_processed_candle_time = None
trade_history = []

def load_trade_history():
    global trade_history
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f: trade_history = json.load(f)
        except: trade_history = []

def save_trade(side, price, qty):
    try:
        acc = client.get_account()
        usdc_bal = float(next((a['free'] for a in acc['balances'] if a['asset'] == "USDC"), 0))
        sol_bal = float(next((a['free'] for a in acc['balances'] if a['asset'] == "SOL"), 0))
        total_usdc_value = usdc_bal + (sol_bal * float(price))
    except:
        total_usdc_value = 0.0

    trade = {
        "time": datetime.now(PARIS_TZ).strftime('%Y-%m-%d %H:%M:%S'),
        "side": side,
        "price": float(price),
        "qty": float(qty),
        "total_usdc_value": float(total_usdc_value)
    }
    trade_history.append(trade)
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4)
    except Exception as e:
        logging.error(f"Не вдалося зберегти історію: {e}")

def calculate_supertrend_manual(klines, period=20, multiplier=5.0):
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
    return direction, st_line, closes, [k[0] for k in klines]

def get_supertrend_signal():
    global last_processed_candle_time
    try:
        klines = client.get_klines(symbol=TRADE_SYMBOL, interval=TEST_INTERVAL, limit=100)
        direction, st_line, closes, times = calculate_supertrend_manual(klines, SUPERTREND_PERIOD, SUPERTREND_MULTIPLIER)
        
        closed_candle_time = times[-2]
        if last_processed_candle_time == closed_candle_time:
            return None
            
        current_dir = direction[-2]  
        prev_dir = direction[-3]     
        
        action = None
        if prev_dir == -1 and current_dir == 1: action = "BUY"
        elif prev_dir == 1 and current_dir == -1: action = "SELL"
            
        if action:
            last_processed_candle_time = closed_candle_time
            return {"action": action, "price": closes[-2]}
        return None
    except Exception as e:
        logging.error(f"Помилка розрахунку індикатора: {e}")
        return None

def execute_spot_trade(side: str):
    try:
        acc = client.get_account()
        curr_price = Decimal(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price'])
        
        if side == "BUY":
            usdc = Decimal(next((a['free'] for a in acc['balances'] if a['asset'] == "USDC"), "0"))
            raw_qty = (usdc / curr_price) * Decimal('0.995')
            qty = raw_qty.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            if qty > 0: 
                client.create_order(symbol=TRADE_SYMBOL, side="BUY", type="MARKET", quantity=str(qty))
                save_trade(side, curr_price, qty)
                return f"✅ Куплено {qty} SOL по ~{curr_price} USDC"
            return "❌ Недостатньо USDC на балансі."
            
        else:
            sol = Decimal(next((a['free'] for a in acc['balances'] if a['asset'] == "SOL"), "0"))
            qty = sol.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            if qty > 0: 
                client.create_order(symbol=TRADE_SYMBOL, side="SELL", type="MARKET", quantity=str(qty))
                save_trade(side, curr_price, qty)
                return f"✅ Продано {qty} SOL по ~{curr_price} USDC"
            return "❌ Недостатньо SOL на балансі."
            
    except Exception as e: 
        return f"❌ Помилка виконання ордера на Binance: {e}"

async def auto_job(context: ContextTypes.DEFAULT_TYPE):
    data = get_supertrend_signal()
    if data and data.get('action'):
        msg = execute_spot_trade(data['action'])
        await context.bot.send_message(chat_id=context.job.data['chat_id'], text=f"🚀 <b>Підтверджений сигнал: {data['action']}</b>\n\n{msg}", parse_mode="HTML")

async def get_balance_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        acc = client.get_account()
        usdc = float(next((a['free'] for a in acc['balances'] if a['asset'] == "USDC"), 0))
        sol = float(next((a['free'] for a in acc['balances'] if a['asset'] == "SOL"), 0))
        await update.message.reply_text(f"💳 <b>Поточний баланс:</b>\n• USDC: <code>{usdc:.2f}</code>\n• SOL: <code>{sol:.4f}</code>", parse_mode="HTML")
    except Exception as e: 
        await update.message.reply_text(f"❌ Помилка запиту балансу: {e}")

async def show_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not trade_history:
        await update.message.reply_text("📭 Історія угод порожня.")
        return
    
    lines = ["📊 <b>Історія ордерів та балансу:</b>"]
    last_val = None
    
    for t in trade_history[-10:]:
        side = t['side']
        price = t['price']
        qty = t['qty']
        total_usdc = t.get('total_usdc_value', 0.0)
        icon = "🛒" if side == "BUY" else "💰"
        
        diff_str = ""
        if last_val and total_usdc > 0:
            diff_usdc = total_usdc - last_val
            color = "🟢" if diff_usdc >= 0 else "🔴"
            diff_str = f" ({color} {diff_usdc:+.2f} USDC)"
                
        lines.append(f"{icon} <b>{side}</b> {qty} SOL @ {price:.2f} | Депозит: {total_usdc:.2f} USDC{diff_str}")
        if total_usdc > 0:
            last_val = total_usdc
        
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def manual_buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Виконую маркет-купівлю на весь баланс USDC...")
    res = execute_spot_trade("BUY")
    await update.message.reply_text(res)

async def manual_sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Виконую маркет-продаж всього балансу SOL...")
    res = execute_spot_trade("SELL")
    await update.message.reply_text(res)

async def enable_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    if not auto_trading_enabled:
        auto_trading_enabled = True
        context.job_queue.run_repeating(auto_job, interval=AUTO_TRADE_INTERVAL, first=1, name="st_auto_job", data={"chat_id": update.effective_chat.id})
        await update.message.reply_text("🚀 <b>Автотрейдинг за SuperTrend (20, 5.0) УВІМКНЕНО!</b>\nБот сканує закриті 1-годинні свічки.", parse_mode="HTML")
    else:
        await update.message.reply_text("⚠️ Автотрейдинг уже запущений.")

async def disable_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    auto_trading_enabled = False
    for job in context.job_queue.get_jobs_by_name("st_auto_job"): 
        job.schedule_removal()
    await update.message.reply_text("⛔ <b>Автотрейдинг ПОВНІСТЮ ВИМКНЕНО.</b>")

def main():
    load_trade_history()
    application = ApplicationBuilder().token(config.TELEGRAM_API_KEY).build()
    
    kb = [
        ["💰 Баланс", "📜 Історія ордерів"],
        ["🟢 Ручна Купівля (Всі)", "🔴 Ручний Продаж (Всі)"],
        ["⚡ Увімкнути авто", "⏸ Вимкнути авто"]
    ]
    
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("🔷 Робочий спотовий профіль SOL/USDC активовано.", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))))
    
    application.add_handler(MessageHandler(filters.Regex(".*Баланс.*"), get_balance_cmd))
    application.add_handler(MessageHandler(filters.Regex(".*Історія.*"), show_history_cmd))
    application.add_handler(MessageHandler(filters.Regex(".*Ручна Купівля.*"), manual_buy_cmd))
    application.add_handler(MessageHandler(filters.Regex(".*Ручний Продаж.*"), manual_sell_cmd))
    application.add_handler(MessageHandler(filters.Regex(".*Увімкнути авто.*"), enable_trading))
    application.add_handler(MessageHandler(filters.Regex(".*Вимкнути авто.*"), disable_trading))
    
    application.run_polling()

if __name__ == '__main__': main()
