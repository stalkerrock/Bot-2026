import asyncio
import socket
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from datetime import datetime, timedelta
import json
import os
import logging
from decimal import Decimal, ROUND_DOWN

log_file = 'trading_bot.log'
try:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='a'),
            logging.StreamHandler()
        ]
    )
    logging.info("Logging initialized successfully")
except Exception as e:
    print(f"Failed to initialize logging: {e}")
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

API_KEY = "3v2KzK8lhtYblymiQiRd9aFxuRZXOuv3wdgZnVgPGTWSIw7WQUYxxrPlf9cYQ8ul"
SECRET_KEY = "aFXnMhVhhet45dBQyxVbJzVgJS5pSUsC8P7SvDvGS1Tn0WDkWMKQMD3PdZUOOitR"
client = Client(API_KEY, SECRET_KEY)
TRADE_SYMBOL = "BTCUSDC"

MACD_FAST = 5
MACD_SLOW = 10
MACD_SIGNAL = 3
AUTO_TRADE_INTERVAL = 60  # кожну хвилину

auto_trading_enabled = False
trade_history = []
TRADE_HISTORY_FILE = "trade_history.json"
last_buy_price = None
prev_histogram_value = None
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

def generate_candlestick_graph(klines, max_bars=5):
    if not klines:
        return "Свічки: (немає даних)"
    
    candles = klines[-max_bars:]
    if not candles:
        return "Свічки: (немає даних для відображення)"

    prices_flat = []
    for k in candles:
        prices_flat.extend([float(k[1]), float(k[2]), float(k[3]), float(k[4])])

    max_price = max(prices_flat) if prices_flat else 1
    min_price = min(prices_flat) if prices_flat else 0

    price_range = max_price - min_price
    if price_range == 0: 
        price_range = max_price if max_price != 0 else 1 

    graph = ["<b>Свічки (1m):</b>"]
    for i, candle in enumerate(candles):
        open_price = float(candle[1])
        close_price = float(candle[4])
        high_price = float(candle[2])
        low_price = float(candle[3])
        is_bullish = close_price >= open_price
        color_emoji = "🟢" if is_bullish else "🔴"

        candle_time = datetime.fromtimestamp(int(candle[0]) / 1000).strftime('%Y-%m-%d %H:%M')

        BAR_SCALE = 10

        normalized_open = (open_price - min_price) / price_range * BAR_SCALE
        normalized_close = (close_price - min_price) / price_range * BAR_SCALE
        normalized_high = (high_price - min_price) / price_range * BAR_SCALE
        normalized_low = (low_price - min_price) / price_range * BAR_SCALE

        body_start = min(normalized_open, normalized_close)
        body_end = max(normalized_open, normalized_close)
        
        body_len = int(body_end - body_start)
        lower_wick_len = int(body_start - normalized_low)
        upper_wick_len = int(normalized_high - body_end)

        if body_len < 1:
            body_len = 1
        
        line_chars = [" "] * (BAR_SCALE + 2)

        for j in range(int(normalized_low), int(normalized_high) + 1):
            if 0 <= j < len(line_chars):
                line_chars[j] = "│"
        
        for j in range(int(body_start), int(body_end) + 1):
            if 0 <= j < len(line_chars):
                line_chars[j] = "█"
        
        if open_price == close_price:
            mid = int(normalized_open)
            if 0 <= mid < len(line_chars):
                line_chars[mid] = "─"

        graph.append(f"{i+1}. {color_emoji} {close_price:.2f} ({candle_time}) {''.join(line_chars).strip()}")

    return "\n".join(graph)

def generate_histogram_graph(hist_values, klines, max_bars=10):
    if not hist_values or not klines:
        logging.warning("generate_histogram_graph: Empty histogram or klines data received")
        return "Гістограма: (немає даних)"
    
    hist_values_slice = hist_values[-max_bars:] 
    klines_slice = klines[-max_bars:]

    all_values = [v for v in hist_values_slice if v is not None]
    if not all_values:
        return "Гістограма: (немає даних для відображення)"

    max_abs_value = max(abs(v) for v in all_values) if all_values else 1 
    if max_abs_value == 0:
        max_abs_value = 1 

    graph = ["<b>Гістограма MACD:</b>"]
    BAR_WIDTH = 15

    zero_line_pos = int(BAR_WIDTH / 2)
    
    for i in range(len(hist_values_slice)): 
        hist_val = hist_values_slice[i]

        hist_norm = hist_val / max_abs_value
        
        bar_length = int(abs(hist_norm) * zero_line_pos)

        hist_bar_chars = [" "] * BAR_WIDTH
        
        if hist_val >= 0:
            for j in range(zero_line_pos, zero_line_pos + bar_length):
                if j < BAR_WIDTH:
                    hist_bar_chars[j] = "█"
            hist_color_emoji = "🟢"
        else:
            for j in range(zero_line_pos - bar_length, zero_line_pos):
                if j >= 0:
                    hist_bar_chars[j] = "█"
            hist_color_emoji = "🔴"
        
        if 0 <= zero_line_pos < BAR_WIDTH:
            hist_bar_chars[zero_line_pos] = "|"

        candle_time = datetime.fromtimestamp(int(klines_slice[i][0]) / 1000).strftime('%Y-%m-%d %H:%M') if i < len(klines_slice) else "Немає даних"

        graph.append(
            f"{i+1}. {hist_color_emoji} {hist_val:.2f} ({candle_time}) | {''.join(hist_bar_chars)}"
        )
    
    return "\n".join(graph)

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
    logging.info("Calculating MACD signal...")
    for attempt in range(max_retries):
        try:
            start_time = int((datetime.now() - timedelta(minutes=100)).timestamp() * 1000)
            klines = client.get_klines(symbol=TRADE_SYMBOL, interval=Client.KLINE_INTERVAL_1MINUTE, limit=100, startTime=start_time)
            close_prices = [float(k[4]) for k in klines]
            
            logging.info(f"Close prices (last 10): {close_prices[-10:]}")
            
            if len(close_prices) < max(MACD_SLOW, MACD_FAST, MACD_SIGNAL):
                logging.warning(f"Not enough klines to calculate MACD. Need at least {max(MACD_SLOW, MACD_FAST, MACD_SIGNAL)}, got {len(close_prices)}.")
                return {"signal": None, "details": "Недостатньо даних для MACD", "trend": "❌ Не визначено", "macd": [], "signal_line": [], "histogram": [], "klines": klines}

            fast_ema = calculate_ema(close_prices, MACD_FAST)
            slow_ema = calculate_ema(close_prices, MACD_SLOW)
            
            logging.info(f"Fast EMA (last 5): {fast_ema[-5:]}")
            logging.info(f"Slow EMA (last 5): {slow_ema[-5:]}")
            
            if not fast_ema or not slow_ema:
                logging.warning("EMA calculation resulted in empty lists.")
                return {"signal": None, "details": "Помилка розрахунку EMA", "trend": "❌ Не визначено", "macd": [], "signal_line": [], "histogram": [], "klines": klines}

            if len(fast_ema) < MACD_SLOW or len(slow_ema) < MACD_SLOW:
                logging.warning("Not enough EMA values for MACD calculation.")
                return {"signal": None, "details": "Недостатньо EMA значень для MACD", "trend": "❌ Не визначено", "macd": [], "signal_line": [], "histogram": [], "klines": klines}

            length = min(len(fast_ema), len(slow_ema))
            macd = [fast_ema[i] - slow_ema[i] for i in range(length)]
            
            logging.info(f"MACD (last 5): {macd[-5:]}")
            
            if not macd or len(macd) < MACD_SIGNAL:
                logging.warning("MACD line is too short to calculate Signal line.")
                return {"signal": None, "details": "MACD лінія занадто коротка", "trend": "❌ Не визначено", "macd": macd, "signal_line": [], "histogram": [], "klines": klines}

            signal = calculate_ema(macd, MACD_SIGNAL)
            
            logging.info(f"Signal (last 5): {signal[-5:]}")
            
            if not signal:
                logging.warning("Signal line calculation resulted in an empty list.")
                return {"signal": None, "details": "Помилка розрахунку Signal line", "trend": "❌ Не визначено", "macd": macd, "signal_line": signal, "histogram": [], "klines": klines}

            histogram_values = [macd[i] - signal[i] for i in range(min(len(macd), len(signal)))]
            
            logging.info(f"Histogram (last 5): {histogram_values[-5:]}")
            
            if not histogram_values:
                logging.warning("Histogram calculation resulted in an empty list.")
                return {"signal": None, "details": "Помилка розрахунку Histogram", "trend": "❌ Не визначено", "macd": macd, "signal_line": signal, "histogram": [], "klines": klines}

            current_hist = histogram_values[-1]
            last_macd_value = macd[-1]
            last_signal_value = signal[-1]
            logging.info(f"MACD calculated: MACD={last_macd_value:.2f}, Signal={last_signal_value:.2f}, Histogram={current_hist:.2f}")

            if prev_histogram_value is None:
                prev_histogram_value = current_hist
                return {"signal": None, "details": f"DIF {last_macd_value:.2f}, DEA {last_signal_value:.2f}", "trend": "🟡 Чекаємо перший стовпець", "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}
            
            prev_histogram_value = current_hist
            signal_action = "BUY" if current_hist >= 0 else "SELL" if current_hist < 0 else None
            trend = "🟢 Позитивний" if current_hist >= 0 else "🔴 Негативний"
            return {"signal": signal_action, "details": f"DIF {last_macd_value:.2f}, DEA {last_signal_value:.2f}", "trend": trend, "macd": macd, "signal_line": signal, "histogram": histogram_values, "klines": klines}

        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for MACD signal: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return {"signal": None, "details": f"Помилка аналізу: {str(e)}", "trend": "❌ Не визначено", "macd": [], "signal_line": [], "histogram": [], "klines": []}

async def macd_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("MACD signal command triggered")
    await update.message.reply_text("Обчислення MACD сигналу, будь ласка зачекайте...")
    result = get_macd_signal()
    
    if not result or not result.get("histogram"):
        logging.error("get_macd_signal returned invalid data or histogram is empty")
        await update.message.reply_text(f"Помилка: {result.get('details', 'Невдалося отримати MACD-сигнал або дані неповні.')}")
        return

    try:
        candlestick_graph = generate_candlestick_graph(result["klines"])
        hist_graph = generate_histogram_graph(result["histogram"], result["klines"])
        hist_color_emoji = "🟢" if result["histogram"][-1] >= 0 else "🔴"
        
        current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'

        response = [
            f"<b>{TRADE_SYMBOL} @ {current_price:.2f} (1m)</b>",
            f"<b>MACD: {hist_color_emoji} {result['histogram'][-1]:.2f}</b>",
            f"\n{candlestick_graph}",
            f"\n{hist_graph}",
            f"\nТренд: {result['trend']}",
            f"Останній сигнал: {result['signal']}" if result['signal'] else "Сигналів для дії не виявлено"
        ]
        logging.info("Sending MACD signal response to Telegram")
        await update.message.reply_text("\n".join(response), parse_mode='HTML')
    except Exception as e:
        logging.error(f"Error in macd_signal_command: {str(e)}")
        await update.message.reply_text(f"Помилка відображення MACD: {str(e)}")

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
            return current_filters
        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed to get symbol filters for {TRADE_SYMBOL}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"Failed to get symbol filters after {max_retries} attempts: '{str(e)}'")

async def get_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting balance...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            balance_info = client.get_account()
            btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
            usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
            
            btc_free = float(btc_balance_info['free']) if btc_balance_info else 0.0
            usdc_free = float(usdc_balance_info['free']) if usdc_balance_info else 0.0
            
            logging.info(f"Balance: BTC={btc_free:.8f}, USDC={usdc_free:.2f}")
            await update.message.reply_text(f"💰 Баланс:\nBTC: {btc_free:.8f}\nUSDC: {usdc_free:.2f}")
            return
        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for balance: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                await update.message.reply_text(f"Помилка отримання балансу: {str(e)}")

async def get_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Getting price...")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
            price = float(price_info['price'])
            logging.info(f"Price: {price:.2f} USDC")
            await update.message.reply_text(f"📈 Поточна ціна {TRADE_SYMBOL}: {price:.2f} USDC")
            return
        except Exception as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed for price: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                await update.message.reply_text(f"Помилка отримання ціни: {str(e)}")

async def show_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Showing statistics...")
    if not trade_history:
        await update.message.reply_text("📊 Історія торгів порожня.")
        return
    messages = ["📊 Історія торгів (останні 10):"]
    for trade in reversed(trade_history[-10:]): 
        trade_amount_usdc = trade['amount'] * trade['price']
        profit_loss = 0
        if last_buy_price is not None:
            if trade['type'] == 'SELL':
                profit_loss = trade_amount_usdc - (trade['amount'] * last_buy_price)
            elif trade['type'] == 'BUY':
                profit_loss = -(trade_amount_usdc - (trade['amount'] * last_buy_price))
        profit_loss_color = f"<span style='color:green'>+{profit_loss:.2f} USDC</span>" if profit_loss >= 0 else f"<span style='color:red'>{profit_loss:.2f} USDC</span>"
        messages.append(f"{trade['date']} - {trade['type']} {trade['amount']:.8f} BTC за {trade['price']:.2f} USDC (Зміна балансу: {profit_loss_color})") 
    await update.message.reply_text("\n".join(messages), parse_mode='HTML')

async def toggle_auto_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global auto_trading_enabled
    logging.info("Toggling auto trading...")
    job_queue = context.application.job_queue

    auto_trading_enabled = not auto_trading_enabled

    for job in job_queue.get_jobs_by_name("auto_trading"):
        job.schedule_removal()

    if auto_trading_enabled:
        logging.info(f"Scheduling auto trading to start after 10 seconds with interval {AUTO_TRADE_INTERVAL} seconds")
        job_queue.run_repeating(
            check_macd_and_trade,
            interval=AUTO_TRADE_INTERVAL,
            first=10,
            name="auto_trading",
            data={"chat_id": update.effective_chat.id}
        )
        await update.message.reply_text(f"✅ Автотрейдинг увімкнено!\nПеревірка кожну хвилину.")
    else:
        logging.info("Auto trading disabled")
        await update.message.reply_text("⛔ Автотрейдинг вимкнено")

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
        logging.error(f"Failed to get symbol filters in execute_market_trade: {e}")
        return f"Помилка торгівлі: Не вдалося отримати фільтри символу: {str(e)}"

    for attempt in range(max_retries):
        try:
            if side == "BUY":
                balance_info = client.get_account()
                usdc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "USDC"), None)
                usdc_balance = Decimal(usdc_balance_info['free']) if usdc_balance_info else Decimal('0')

                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = Decimal(current_price_info['price'])

                if usdc_balance < min_notional:
                    return f"Баланс {usdc_balance:.2f} USDC нижче мінімуму {min_notional:.2f} USDC"

                amount_to_spend = usdc_balance

                if current_price <= 0:
                    return "Помилка: Поточна ціна нульова або від'ємна"

                quantity_raw = amount_to_spend / current_price

                rounding_precision = Decimal('1E-%d' % qty_precision)
                quantity_decimal = (quantity_raw / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
                quantity_decimal = quantity_decimal.quantize(rounding_precision, rounding=ROUND_DOWN)

                if quantity_decimal < min_qty:
                    return f"Кількість {quantity_decimal:.8f} нижче мінімуму {min_qty}"

                if quantity_decimal > max_qty:
                    return f"Кількість {quantity_decimal:.8f} перевищує максимум {max_qty}"

                calculated_notional = quantity_decimal * current_price
                if calculated_notional < min_notional:
                    return f"Сума ордера {calculated_notional:.2f} нижче мінімуму {min_notional:.2f}"

                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="BUY",
                    type="MARKET",
                    quantity=f"{quantity_decimal:.{qty_precision}f}"
                )

                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills']) / Decimal(str(filled_qty)) if filled_qty > 0 else Decimal('0')

                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M'),
                    "type": "BUY",
                    "amount": float(filled_qty),
                    "price": float(filled_price)
                }
                last_buy_price = trade_data["price"]
                save_trade(trade_data)
                return f"🟢 Купівля: {trade_data['amount']:.8f} BTC за {trade_data['price']:.2f} USDC"

            elif side == "SELL":
                balance_info = client.get_account()
                btc_balance_info = next((asset for asset in balance_info['balances'] if asset['asset'] == "BTC"), None)
                btc_balance = Decimal(btc_balance_info['free']) if btc_balance_info else Decimal('0')

                if btc_balance < min_qty:
                    return f"Недостатньо BTC: {btc_balance:.8f} (мін. {min_qty})"

                rounding_precision = Decimal('1E-%d' % qty_precision)
                quantity_decimal = (btc_balance / step_size).quantize(Decimal('1'), rounding=ROUND_DOWN) * step_size
                quantity_decimal = quantity_decimal.quantize(rounding_precision, rounding=ROUND_DOWN)

                if quantity_decimal > max_qty:
                    return f"Кількість перевищує максимум {max_qty}"

                current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
                current_price = Decimal(current_price_info['price'])

                calculated_notional = quantity_decimal * current_price
                if calculated_notional < min_notional:
                    return f"Сума ордера {calculated_notional:.2f} нижче мінімуму {min_notional:.2f}"

                order = client.create_order(
                    symbol=TRADE_SYMBOL,
                    side="SELL",
                    type="MARKET",
                    quantity=f"{quantity_decimal:.{qty_precision}f}"
                )

                filled_qty = sum(float(f['qty']) for f in order['fills'])
                filled_price = sum(Decimal(f['price']) * Decimal(f['qty']) for f in order['fills']) / Decimal(str(filled_qty)) if filled_qty > 0 else Decimal('0')

                trade_data = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M'),
                    "type": "SELL",
                    "amount": float(filled_qty),
                    "price": float(filled_price)
                }
                save_trade(trade_data)
                last_buy_price = None
                return f"🔴 Продаж: {trade_data['amount']:.8f} BTC за {trade_data['price']:.2f} USDC"

        except Exception as e:
            logging.error(f"Attempt {attempt + 1} failed: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return f"Помилка торгівлі: {str(e)}"

async def buy_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Buy BTC command triggered")
    await update.message.reply_text("Спроба купівлі BTC...")
    result = execute_market_trade("BUY")
    await update.message.reply_text(result)

async def sell_btc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Sell BTC command triggered")
    await update.message.reply_text("Спроба продажу BTC...")
    result = execute_market_trade("SELL")
    await update.message.reply_text(result)

async def check_macd_and_trade(context: ContextTypes.DEFAULT_TYPE):
    if not auto_trading_enabled:
        return
    logging.info("Checking MACD and trading...")
    chat_id = context.job.data["chat_id"]
    result = get_macd_signal()
    
    if not result or not result.get("histogram"):
        logging.error("check_macd_and_trade: get_macd_signal returned invalid data or histogram is empty")
        await context.bot.send_message(chat_id=chat_id, text="Помилка: Невдалося отримати MACD-сигнал або дані неповні.")
        return

    signal_action = result["signal"]
    histogram_value = result["histogram"][-1] if result["histogram"] else 0
    
    trade_message = None

    if signal_action == "BUY" and histogram_value >= 0:
        logging.info("MACD BUY signal detected")
        trade_message = execute_market_trade("BUY")
    elif signal_action == "SELL" and histogram_value < 0:
        logging.info("MACD SELL signal detected")
        trade_message = execute_market_trade("SELL")
    else:
        return

    if trade_message:
        hist_color_emoji = "🟢" if histogram_value >= 0 else "🔴"
        current_price_info = client.get_symbol_ticker(symbol=TRADE_SYMBOL)
        current_price = float(current_price_info['price']) if current_price_info else 'N/A'

        response_parts = [
            f"<b>Автотрейдинг ({datetime.now().strftime('%H:%M:%S')}):</b>",
            f"<b>{TRADE_SYMBOL} @ {current_price:.2f} USDC</b>",
            f"<b>MACD гістограма (1м): {hist_color_emoji} {histogram_value:.4f}</b>",
            f"Тренд: {result['trend']}",
            f"Сигнал: {signal_action}",
            f"Результат торгівлі: {trade_message}"
        ]
        await context.bot.send_message(chat_id=chat_id, text="\n".join(response_parts), parse_mode='HTML')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Starting bot...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "🔷 Торгівельний бот Binance\n\n"
        "🤖 Автотрейдинг - автоматичні угоди за сигналами MACD (1м)\n"
        "📊 MACD сигнал - перевірка поточного стану\n"
        "🟢 Купити BTC - купівля BTC за всю суму USDC за ринковою ціною\n"
        "🔴 Продати BTC - продаж усього BTC за ринковою ціною\n"
        "📊 Статистика - показує історію торгів\n\n"
        "Оберіть дію:",
        reply_markup=reply_markup
    )

async def refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("Refreshing keyboard...")
    trade_keyboard = [
        ["💰 Перевірити баланс", "📈 Ціна BTC"],
        ["📊 MACD сигнал", "🤖 Автотрейдинг"],
        ["🟢 Купити BTC", "🔴 Продати BTC"],
        ["📊 Статистика"]
    ]
    reply_markup = ReplyKeyboardMarkup(trade_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "✅ Клавіатуру оновлено!\n\nОберіть дію:",
        reply_markup=reply_markup
    )

def main():
    logging.info("Starting main function...")
    load_trade_history()
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("refresh", refresh))
    application.add_handler(MessageHandler(filters.Regex("^(💰 Перевірити баланс)$"), get_balance))
    application.add_handler(MessageHandler(filters.Regex("^(📈 Ціна BTC)$"), get_price))
    application.add_handler(MessageHandler(filters.Regex("^(📊 MACD сигнал)$"), macd_signal_command))
    application.add_handler(MessageHandler(filters.Regex("^(🤖 Автотрейдинг)$"), toggle_auto_trading))
    application.add_handler(MessageHandler(filters.Regex("^(🟢 Купити BTC)$"), buy_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^(🔴 Продати BTC)$"), sell_btc_command))
    application.add_handler(MessageHandler(filters.Regex("^(📊 Статистика)$"), show_statistics))

    logging.info("Application started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
