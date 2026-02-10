import asyncio
import socket
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
import config
from datetime import datetime, timedelta
import json
import os
import logging
from decimal import Decimal, ROUND_DOWN
import time

# --- НАЛАШТУВАННЯ ---
log_file = 'trading_bot.log' [cite: 12]
TRADE_SYMBOL = "BTCUSDC"  # Змінено з ETH на BTC 
MACD_FAST = 5 [cite: 31]
MACD_SLOW = 10 [cite: 32]
MACD_SIGNAL = 3 [cite: 33]
AUTO_TRADE_INTERVAL = 60  # Перевірка кожну хвилину [cite: 34]
TRADE_HISTORY_FILE = "trade_history.json" [cite: 37]

# Глобальні змінні
auto_trading_enabled = False [cite: 35]
trade_history = [] [cite: 36]
last_buy_price = None [cite: 38, 350]
prev_histogram_value = None [cite: 39]
symbol_filters = {} [cite: 40]

# --- ІНІЦІАЛІЗАЦІЯ ЛОГУВАННЯ --- [cite: 14-26]
try:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file, mode='a'), logging.StreamHandler()]
    )
    logging.info("Logging initialized successfully")
except Exception as e:
    print(f"Failed to initialize logging: {e}")

# Binance Client [cite: 27-29]
client = Client(config.API_KEY, config.SECRET_KEY)

# --- ДОПОМІЖНІ ФУНКЦІЇ ---

def load_trade_history():
    global trade_history, last_buy_price
    if os.path.exists(TRADE_HISTORY_FILE):
        try:
            with open(TRADE_HISTORY_FILE, "r") as f:
                trade_history = json.load(f) [cite: 44]
                # Відновлюємо ціну останньої покупки, якщо останньою дією була покупка
                if trade_history and trade_history[-1]['type'] == 'BUY':
                    last_buy_price = trade_history[-1]['price']
        except Exception as e:
            logging.error(f"Error loading trade history: {e}")
            trade_history = []

def save_trade(trade_data):
    global trade_history
    trade_history.append(trade_data) [cite: 52]
    try:
        with open(TRADE_HISTORY_FILE, "w") as f:
            json.dump(trade_history, f, indent=4) [cite: 55]
    except IOError as e:
        logging.error(f"Error saving trade history: {e}") [cite: 57]

def calculate_ema(prices, period):
    if len(prices) < period: return [] [cite: 152]
    alpha = 2 / (period + 1) [cite: 154]
    ema = [prices[0]]
    for price in prices[1:]:
        ema.append((price * alpha) + (ema[-1] * (1 - alpha))) [cite: 157]
    return ema

# --- ТЕХНІЧНИЙ АНАЛІЗ (1m) ---

def get_macd_signal():
    global prev_histogram_value
    try:
        # Таймфрейм 1 хвилина 
        klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100)
        close_prices = [float(k[4]) for k in klines] [cite: 167]

        fast_ema = calculate_ema(close_prices, MACD_FAST) [cite: 171]
        slow_ema = calculate_ema(close_prices, MACD_SLOW) [cite: 171]
        
        length = min(len(fast_ema), len(slow_ema))
        macd = [fast_ema[i] - slow_ema[i] for i in range(length)] [cite: 182]
        signal = calculate_ema(macd, MACD_SIGNAL) [cite: 188]
        
        hist = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))] [cite: 192]
        current_hist = hist[-1]
        
        signal_action = "BUY" if current_hist >= 0 else "SELL" [cite: 209]
        trend = "Позитивний" if current_hist >= 0 else "Негативний" [cite: 210]
        
        return {"signal": signal_action, "trend": trend, "histogram": hist, "klines": klines, "details": f"H: {current_hist:.4f}"}
    except Exception as e:
        logging.error(f"MACD Error: {e}")
        return None

# --- ТОРГІВЛЯ (ALL-IN) ---

def get_filters():
    global symbol_filters
    if TRADE_SYMBOL in symbol_filters: return symbol_filters[TRADE_SYMBOL] [cite: 251]
    
    info = client.get_symbol_info(TRADE_SYMBOL)
    filters = {f['filterType']: f for f in info['filters']} [cite: 266]
    
    res = {
        'minNotional': Decimal(filters['NOTIONAL']['minNotional']), [cite: 283]
        'minQty': Decimal(filters['LOT_SIZE']['minQty']), [cite: 284]
        'stepSize': Decimal(filters['LOT_SIZE']['stepSize']), [cite: 286]
        'precision': info['quoteAssetPrecision']
    }
    # Визначаємо точність кількості з stepSize [cite: 289]
    res['qtyPrecision'] = len(str(res['stepSize']).rstrip('0').split('.')[1]) if '.' in str(res['stepSize']) else 0
    symbol_filters[TRADE_SYMBOL] = res
    return res

def execute_market_trade(side):
    global last_buy_price
    f = get_filters() [cite: 249]
    
    try:
        acc = client.get_account() [cite: 308]
        price = Decimal(client.get_symbol_ticker(symbol=TRADE_SYMBOL)['price']) [cite: 332]
        
        if side == "BUY":
            balance = Decimal(next(a['free'] for a in acc['balances'] if a['asset'] == "USDC")) [cite: 313]
            if balance < f['minNotional']: return "⚠️ Мало USDC"
            
            # Купівля на все (мінус 0.1% на комісію)
            quantity = ((balance * Decimal("0.999") / price) / f['stepSize']).quantize(Decimal('1'), rounding=ROUND_DOWN) * f['stepSize']
        else:
            balance = Decimal(next(a['free'] for a in acc['balances'] if a['asset'] == "BTC")) [cite: 313]
            if balance < f['minQty']: return "⚠️ Мало BTC"
            quantity = (balance / f['stepSize']).quantize(Decimal('1'), rounding=ROUND_DOWN) * f['stepSize']

        if (quantity * price) < f['minNotional']: return "⚠️ Сума нижче ліміту"

        order = client.create_order(
            symbol=TRADE_SYMBOL, side=side, type="MARKET",
            quantity=f"{quantity:.{f['qtyPrecision']}f}"
        ) [cite: 366]

        # Розрахунок результатів
        filled_qty = sum(float(i['qty']) for i in order['fills']) [cite: 366]
        avg_price = sum(float(i['price']) * float(i['qty']) for i in order['fills']) / filled_qty [cite: 367]
        
        net_profit_str = ""
        if side == "SELL" and last_buy_price:
            profit = (avg_price - last_buy_price) * filled_qty
            net_profit_str = f" | Прибуток: {profit:.2f} USDC"
            logging.info(f"NET PROFIT: {profit:.2f} USDC")
        
        trade_data = {"date": datetime.now().strftime('%Y-%m-%d %H:%M'), "type": side, "amount": filled_qty, "price": avg_price}
        save_trade(trade_data) [cite: 368]
        
        if side == "BUY": last_buy_price = avg_price
        else: last_buy_price = None

        return f"{'🟢' if side=='BUY' else '🔴'} {side} {filled_qty:.5f} BTC @ {avg_price:.2f}{net_profit_str}"

    except Exception as e:
        logging.error(f"Trade Error: {e}")
        return f"❌ Помилка: {e}"

# --- TELEGRAM ФУНКЦІЇ ---

async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    res = get_macd_signal()
    if not res: return

    signal = res['signal']
    chat_id = context.job.data['chat_id']
    
    # Логіка: Купуємо тільки якщо ще не в позиції, продаємо якщо в позиції
    if signal == "BUY" and last_buy_price is None:
        msg = execute_market_trade("BUY")
        await context.bot.send_message(chat_id=chat_id, text=f"🤖 {msg}")
    elif signal == "SELL" and last_buy_price is not None:
        msg = execute_market_trade("SELL")
        await context.bot.send_message(chat_id=chat_id, text=f"🤖 {msg}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await refresh(update, context) [cite: 373]

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kbd = [["💰 Баланс", "📈 Ціна BTC"], ["📊 MACD", "🤖 Автотрейдинг"], ["🟢 Купити BTC", "🔴 Продати BTC"], ["📊 Статистика"]]
    await update.message.reply_text("BTC Бот готовий:", reply_markup=ReplyKeyboardMarkup(kbd, resize_keyboard=True)) [cite: 374]

def main():
    load_trade_history() [cite: 41]
    app = Application.builder().token(config.TELEGRAM_API_KEY).build()
    
    # Додайте тут свої обробники (CommandHandler, MessageHandler) як у вихідному коді [cite: 374]
    app.run_polling()

if __name__ == "__main__":
    main()
