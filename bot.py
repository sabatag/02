import os
import re
import time
import json
import logging
import websocket
import threading
from datetime import datetime
import pytz
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

# Настройка логирования с часовым поясом CEST
tz = pytz.timezone('Europe/Paris')
logging.basicConfig(
    filename="/var/www/phyton/bot/botttss/logs.txt",
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
logging.Formatter.converter = lambda *args: tz.localize(datetime.utcnow()).timetuple()

# Загрузка API-ключей
load_dotenv()

# Параметры стратегии
MAX_POSITIONS = 5
BLACKLIST_DURATION = 8 * 3600
POSITION_SIZE_PERCENT = 5.0  # Размер входа 5%
DOCOUP_SIZE_PERCENT = 5.0   # Размер докупки 5%
FIRST_DOCOUP_DROP = 0.01    # Докупка на -1%
TAKE_PROFIT_TRIGGER = 0.10  # Триггер тейк-профита на +10%
TAKE_PROFIT_INITIAL = 0.05  # Начальный тейк-профит +5%
TAKE_PROFIT_STEP = 0.05     # Шаг для расчёта тейк-профита
TAKE_PROFIT_PULL_STEP = 0.04  # Шаг подтягивания тейк-профита
TAKE_PROFIT_PERCENT = 0.10  # Тейк-профит 10%

# Глобальные переменные
positions_prices = {}
ws_instance = None

# Чтение пользователей
def load_users():
    users = []
    try:
        with open("/var/www/phyton/bot/botttss/users.txt", "r") as f:
            for line in f:
                parts = line.strip().split(",")
                user_id, api_key, api_secret = parts[:3]
                session = HTTP(testnet=False, api_key=api_key, api_secret=api_secret)
                logging.info(f"Initialized API session for {user_id} with key: {api_key[:4]}****")
                users.append({"user_id": user_id, "session": session})
        logging.info(f"Loaded {len(users)} users")
        return users
    except Exception as e:
        logging.error(f"Error loading users: {e}")
        return []

# Проверка баланса
def check_balance(session, user_id):
    try:
        response = session.get_wallet_balance(accountType="UNIFIED")
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Balance check failed: {response['retMsg']} (ErrCode: {response['retCode']})")
            return None
        balance = float(response["result"]["list"][0]["totalEquity"])
        logging.info(f"{user_id}: Balance: {balance} USD")
        return balance
    except Exception as e:
        logging.error(f"{user_id}: Error checking balance: {e}")
        return None

# Проверка тикера и размеров ордера
def check_ticker(session, symbol, user_id):
    try:
        response = session.get_instruments_info(category="linear", symbol=symbol)
        if response["retCode"] != 0 or not response["result"]["list"]:
            logging.error(f"{user_id}: Ticker {symbol} check failed: {response['retMsg']} (ErrCode: {response['retCode']})")
            return False, None, None, None
        instrument = response["result"]["list"][0]
        available = bool(instrument)
        min_order_qty = float(instrument.get("lotSizeFilter", {}).get("minOrderQty", 0.001))
        max_order_qty = float(instrument.get("lotSizeFilter", {}).get("maxOrderQty", 1000000))
        lot_step_size = float(instrument.get("lotSizeFilter", {}).get("lotStepSize", 1.0))
        logging.info(f"{user_id}: Ticker {symbol} available: {available}, minOrderQty: {min_order_qty}, maxOrderQty: {max_order_qty}, lotStepSize: {lot_step_size}")
        return available, min_order_qty, max_order_qty, lot_step_size
    except Exception as e:
        logging.error(f"{user_id}: Error checking ticker {symbol}: {e}")
        return False, None, None, None

# Выставление лимитного ордера на докупку
def place_docoup_order(session, user_id, symbol, docoup_price, docoup_size, min_order_qty, max_order_qty, lot_step_size):
    try:
        qty = docoup_size / docoup_price
        if qty < min_order_qty:
            qty = min_order_qty
            docoup_size = qty * docoup_price
        elif qty > max_order_qty:
            qty = max_order_qty
            docoup_size = qty * docoup_price
        qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)
        docoup_size = qty * docoup_price
        
        balance = check_balance(session, user_id)
        if balance is None or balance < docoup_size:
            logging.warning(f"{user_id}: Insufficient balance for docoup order: {balance} USD, required: {docoup_size}")
            return None
        
        response = session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy",
            orderType="Limit",
            price=str(docoup_price),
            qty=str(qty),
            reduceOnly=False
        )
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Failed to place docoup order for {symbol} at {docoup_price}: {response['retMsg']} (ErrCode: {response['retCode']})")
            return None
        order_id = response["result"]["orderId"]
        logging.info(f"{user_id}: Placed docoup order for {symbol} at {docoup_price}, size: {docoup_size} USD, qty: {qty}, orderId: {order_id}")
        with open(f"/var/www/phyton/bot/botttss/pending_docoups_{user_id}.txt", "a") as f:
            f.write(f"{symbol},{docoup_price},{docoup_size},{qty},{order_id}\n")
        return order_id
    except Exception as e:
        logging.error(f"{user_id}: Error placing docoup order for {symbol}: {e}")
        return None

# Отмена лимитных ордеров на докупку
def cancel_docoup_orders(session, user_id, symbol):
    try:
        with open(f"/var/www/phyton/bot/botttss/pending_docoups_{user_id}.txt", "r") as f:
            docoups = [line.strip().split(",") for line in f if line.strip()]
        docoups = [d for d in docoups if d[0] == symbol]
        for docoup in docoups:
            symbol_d, _, _, _, order_id = docoup
            response = session.cancel_order(category="linear", symbol=symbol_d, orderId=order_id)
            if response["retCode"] != 0:
                logging.error(f"{user_id}: Failed to cancel docoup order {order_id} for {symbol}: {response['retMsg']} (ErrCode: {response['retCode']})")
            else:
                logging.info(f"{user_id}: Canceled docoup order {order_id} for {symbol}")
        with open(f"/var/www/phyton/bot/botttss/pending_docoups_{user_id}.txt", "w") as f:
            for d in docoups:
                if d[0] != symbol:
                    f.write(",".join(d) + "\n")
    except FileNotFoundError:
        logging.info(f"{user_id}: No pending docoup orders file found")
    except Exception as e:
        logging.error(f"{user_id}: Error canceling docoup orders for {symbol}: {e}")

# Установка тейк-профита сразу после открытия позиции
def set_take_profit_order(session, user_id, symbol, qty, take_profit_price, min_order_qty, max_order_qty, lot_step_size):
    try:
        # Округляем количество в соответствии с требованиями API
        qty = max(qty, min_order_qty)
        qty = min(qty, max_order_qty)
        qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)
        
        response = session.place_order(
            category="linear",
            symbol=symbol,
            side="Sell",
            orderType="Limit",
            price=str(take_profit_price),
            qty=str(qty),
            reduceOnly=True
        )
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Failed to set take-profit order for {symbol} at {take_profit_price}: {response['retMsg']} (ErrCode: {response['retCode']})")
            return False
        order_id = response["result"]["orderId"]
        logging.info(f"{user_id}: Set take-profit order for {symbol} at {take_profit_price}, qty: {qty}, orderId: {order_id}")
        return True
    except Exception as e:
        logging.error(f"{user_id}: Error setting take-profit order for {symbol}: {e}")
        return False

# Открытие позиции и выставление лимитного ордера на докупку
def open_position(session, user_id, symbol, position_size, price, min_order_qty, max_order_qty, lot_step_size, is_docoup=False):
    try:
        balance = check_balance(session, user_id)
        if balance is None or balance < position_size:
            logging.warning(f"{user_id}: Insufficient balance for position: {balance} USD, required: {position_size}")
            return False
        
        qty = position_size / price
        qty = max(min_order_qty, min(max_order_qty, qty))
        qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)
        
        # Пересчитываем размер позиции после округления qty
        position_size = qty * price
        
        if balance < position_size:
            position_size = balance * (POSITION_SIZE_PERCENT / 100)  # Пересчитываем, если превышает баланс
            qty = position_size / price
            qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)
            position_size = qty * price
        
        response = session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy",
            orderType="Market",
            qty=str(qty),
            reduceOnly=False
        )
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Failed to open position for {symbol}: {response['retMsg']} (ErrCode: {response['retCode']})")
            return False
        
        logging.info(f"{user_id}: {'Docoup' if is_docoup else 'Opened position'}: {symbol}, size: {position_size} USD, price: {price}, qty: {qty}")
        
        with open(f"/var/www/phyton/bot/botttss/positions_{user_id}.txt", "a") as f:
            f.write(f"{symbol},{price},{position_size},{qty},{0 if is_docoup else 1}\n")
        
        # Устанавливаем тейк-профит только для основной позиции, не для докупа
        if not is_docoup:
            take_profit_price = price * (1 + TAKE_PROFIT_PERCENT)
            set_take_profit_order(session, user_id, symbol, qty, take_profit_price, min_order_qty, max_order_qty, lot_step_size)
            
            # Выставляем лимитный ордер на докупку
            docoup_size = balance * (DOCOUP_SIZE_PERCENT / 100)
            docoup_price = price * (1 - FIRST_DOCOUP_DROP)
            place_docoup_order(session, user_id, symbol, docoup_price, docoup_size, min_order_qty, max_order_qty, lot_step_size)
        
        return True
    except Exception as e:
        logging.error(f"{user_id}: Exception opening position for {symbol}: {e}")
        return False

# Закрытие позиции
def close_position(session, user_id, symbol, qty, min_order_qty, max_order_qty, lot_step_size, reason):
    try:
        qty = max(float(qty), min_order_qty)
        qty = min(qty, max_order_qty)
        qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)
        
        response = session.place_order(
            category="linear",
            symbol=symbol,
            side="Sell",
            orderType="Market",
            qty=str(qty),
            reduceOnly=True
        )
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Failed to close position for {symbol}: {response['retMsg']} (ErrCode: {response['retCode']})")
            return False
        
        logging.info(f"{user_id}: Closed position: {symbol}, qty: {qty}, reason: {reason}")
        cancel_docoup_orders(session, user_id, symbol)
        
        positions = load_positions(user_id)
        positions = [p for p in positions if p["symbol"] != symbol or abs(float(p["qty"]) - float(qty)) > 1e-6]
        save_positions(user_id, positions)
        
        with open(f"/var/www/phyton/bot/botttss/blacklist_{user_id}.txt", "a") as f:
            f.write(f"{symbol},{time.time()}\n")
        
        return True
    except Exception as e:
        logging.error(f"{user_id}: Error closing position for {symbol}: {e}")
        return False

# Установка тейк-профита
def set_take_profit(session, user_id, symbol, take_profit_price):
    try:
        response = session.set_trading_stop(
            category="linear",
            symbol=symbol,
            takeProfit=str(take_profit_price)
        )
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Failed to set take-profit for {symbol}: {response['retMsg']} (ErrCode: {response['retCode']})")
            return False
        logging.info(f"{user_id}: Set take-profit for {symbol} at {take_profit_price}")
        return True
    except Exception as e:
        logging.error(f"{user_id}: Error setting take-profit for {symbol}: {e}")
        return False

# Чтение и сохранение позиций
def load_positions(user_id):
    positions = []
    try:
        with open(f"/var/www/phyton/bot/botttss/positions_{user_id}.txt", "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) == 5:
                    symbol, price, size, qty, is_main = parts
                else:
                    logging.warning(f"{user_id}: Ignored invalid position line: {line}")
                    continue
                try:
                    positions.append({
                        "symbol": symbol,
                        "entry_price": float(price),
                        "size": float(size),
                        "qty": float(qty),
                        "is_main": int(is_main)
                    })
                except ValueError as e:
                    logging.error(f"{user_id}: Error parsing position line '{line}': {e}")
        
        # Проверка активных позиций через API с добавлением settleCoin=USDT
        session = next((u["session"] for u in load_users() if u["user_id"] == user_id), None)
        if session:
            try:
                response = session.get_positions(category="linear", settleCoin="USDT")
                if response["retCode"] == 0:
                    active_symbols = {pos["symbol"] for pos in response["result"]["list"] if float(pos["size"]) > 0}
                    # Оставляем только активные позиции или докупы
                    positions = [p for p in positions if p["symbol"] in active_symbols or p["is_main"] == 0]
                    logging.info(f"{user_id}: Synced positions with API. Active symbols: {active_symbols}")
                else:
                    logging.error(f"{user_id}: Failed to get positions from API: {response['retMsg']} (ErrCode: {response['retCode']})")
            except Exception as e:
                logging.error(f"{user_id}: Error getting positions from API: {e}")
        
        save_positions(user_id, positions)
        return positions
    except FileNotFoundError:
        logging.info(f"{user_id}: Positions file not found, starting with empty positions")
        return []
    except Exception as e:
        logging.error(f"{user_id}: Error reading positions file: {e}")
        return []

def save_positions(user_id, positions):
    try:
        with open(f"/var/www/phyton/bot/botttss/positions_{user_id}.txt", "w") as f:
            for p in positions:
                f.write(f"{p['symbol']},{p['entry_price']},{p['size']},{p['qty']},{p['is_main']}\n")
    except Exception as e:
        logging.error(f"{user_id}: Error saving positions: {e}")

# Черный список
def load_blacklist(user_id):
    blacklist = {}
    try:
        with open(f"/var/www/phyton/bot/botttss/blacklist_{user_id}.txt", "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.count(",") != 1:
                    logging.warning(f"{user_id}: Ignored invalid blacklist line: {line}")
                    continue
                try:
                    ticker, timestamp = line.split(",")
                    blacklist[ticker] = float(timestamp)
                except ValueError as e:
                    logging.error(f"{user_id}: Error parsing blacklist line '{line}': {e}")
    except FileNotFoundError:
        logging.info(f"{user_id}: Blacklist file not found, starting with empty blacklist")
    except Exception as e:
        logging.error(f"{user_id}: Error reading blacklist file: {e}")
    return blacklist

def save_blacklist(user_id, blacklist):
    try:
        with open(f"/var/www/phyton/bot/botttss/blacklist_{user_id}.txt", "w") as f:
            for ticker, timestamp in blacklist.items():
                f.write(f"{ticker},{timestamp}\n")
    except Exception as e:
        logging.error(f"{user_id}: Error saving blacklist: {e}")

def clean_blacklist(user_id):
    blacklist = load_blacklist(user_id)
    current_time = time.time()
    blacklist = {k: v for k, v in blacklist.items() if current_time - v < BLACKLIST_DURATION}
    save_blacklist(user_id, blacklist)
    return blacklist

# Парсинг сигнала (простой, как в старом скрипте)
def parse_signal(line):
    pattern_hashtag = r"#(\w+)(?:USDT)?(?:\s|$)?"
    match_hashtag = re.search(pattern_hashtag, line.strip())
    if match_hashtag:
        ticker = match_hashtag.group(1).upper() + "USDT"
        logging.info(f"Parsed hashtag signal: {ticker}")
        return ticker, None, None, True
    logging.info(f"Ignored invalid signal: {line.strip()}")
    return None, None, None, False

# Обработка сигнала
def process_signal(ticker, drop, fr, is_oversold, users):
    for user in users:
        user_id = user["user_id"]
        session = user["session"]
        
        logging.info(f"{user_id}: Processing {ticker}, oversold={is_oversold}, drop={drop}, fr={fr}")
        
        blacklist = clean_blacklist(user_id)
        if ticker in blacklist:
            logging.info(f"{user_id}: {ticker} in blacklist, ignoring")
            continue
        
        available, min_order_qty, max_order_qty, lot_step_size = check_ticker(session, ticker, user_id)
        if not available:
            logging.info(f"{user_id}: {ticker} not available on Bybit")
            continue
        
        positions = load_positions(user_id)
        
        try:
            # Получаем актуальные позиции через API с settleCoin=USDT
            response = session.get_positions(category="linear", settleCoin="USDT")
            if response["retCode"] == 0:
                active_positions = [p for p in positions if p["is_main"] and p["symbol"] in {pos["symbol"] for pos in response["result"]["list"] if float(pos["size"]) > 0}]
            else:
                logging.error(f"{user_id}: Failed to get positions from API: {response['retMsg']} (ErrCode: {response['retCode']})")
                active_positions = [p for p in positions if p["is_main"]]
        except Exception as e:
            logging.error(f"{user_id}: Error getting positions from API: {e}")
            active_positions = [p for p in positions if p["is_main"]]
        
        logging.info(f"{user_id}: Active positions count: {len(active_positions)}")
        
        if len(active_positions) >= MAX_POSITIONS:
            logging.info(f"{user_id}: Max positions ({MAX_POSITIONS}) reached, ignoring")
            continue
        
        if any(p["symbol"] == ticker for p in active_positions):
            logging.info(f"{user_id}: Position for {ticker} already exists, ignoring")
            continue
        
        balance = check_balance(session, user_id)
        if balance is None:
            logging.info(f"{user_id}: Failed to check balance, ignoring {ticker}")
            continue
        
        position_size = balance * (POSITION_SIZE_PERCENT / 100)
        
        try:
            response = session.get_tickers(category="linear", symbol=ticker)
            if response["retCode"] != 0 or not response["result"]["list"]:
                logging.error(f"{user_id}: Ticker price check failed for {ticker}: {response['retMsg']}")
                continue
            price = float(response["result"]["list"][0]["lastPrice"])
            logging.info(f"{user_id}: Current price for {ticker}: {price}")
        except Exception as e:
            logging.error(f"{user_id}: Error getting price for {ticker}: {e}")
            continue
        
        logging.info(f"{user_id}: Opening position for {ticker}")
        open_position(session, user_id, ticker, position_size, price, min_order_qty, max_order_qty, lot_step_size)

# WebSocket для мониторинга цен
def on_message(ws, message):
    global positions_prices
    try:
        data = json.loads(message)
        if "topic" not in data or "data" not in data or not isinstance(data["data"], list):
            return
        for item in data["data"]:
            if "s" not in item or "p" not in item:
                continue
            symbol = item["s"]
            price = float(item["p"])
            timestamp = time.time() / 1000
            positions_prices[symbol] = (price, timestamp)
            logging.info(f"WebSocket: Updated price for {symbol}: {price}")
    except Exception as e:
        logging.error(f"WebSocket error: {e}")

def on_error(ws, error):
    logging.error(f"WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    global ws_instance
    logging.info(f"WebSocket closed: {close_msg} (status: {close_status_code}), reconnecting...")
    if ws_instance:
        try:
            ws_instance.close()
            ws_instance = None
        except Exception as e:
            logging.error(f"Error closing WebSocket: {e}")
    time.sleep(5)
    start_websocket()

def on_open(ws):
    global ws_instance
    logging.info("WebSocket opened")
    ws_instance = ws
    users = load_users()
    tickers = []
    for user in users:
        positions = load_positions(user["user_id"])
        logging.info(f"Loaded positions for {user['user_id']}: {positions}")
        for p in positions:
            symbol = p["symbol"]
            if check_ticker(user["session"], symbol, user["user_id"])[0]:
                tickers.append(symbol)
    if tickers:
        ws.send(json.dumps({"op": "subscribe", "args": [f"publicTrade.{t}" for t in set(tickers)]}))
        logging.info(f"WebSocket: Subscribed to {tickers}")
    else:
        logging.warning("WebSocket: No tickers to subscribe to")

def start_websocket():
    global ws_instance
    if ws_instance:
        try:
            ws_instance.close()
            ws_instance = None
        except Exception as e:
            logging.error(f"Error closing previous WebSocket: {e}")
    
    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            ws = websocket.WebSocketApp(
                "wss://stream.bybit.com/v5/public/linear",
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open
            )
            ws_instance = ws
            threading.Thread(target=ws.run_forever, daemon=True).start()
            logging.info(f"WebSocket started, attempt {attempt + 1}")
            return
        except Exception as e:
            logging.error(f"WebSocket connection failed, attempt {attempt + 1}: {e}")
            time.sleep(5)
    logging.error("Failed to start WebSocket after maximum attempts")

# Проверка тейк-профита и исполненных докупок
def check_positions(users):
    while True:
        for user in users:
            user_id = user["user_id"]
            session = user["session"]
            positions = load_positions(user_id)
            
            balance = check_balance(session, user_id)
            if balance is None:
                logging.warning(f"{user_id}: Skipping position check due to balance error")
                continue
            
            # Проверка исполненных докупок
            try:
                with open(f"/var/www/phyton/bot/botttss/pending_docoups_{user_id}.txt", "r") as f:
                    docoups = [line.strip().split(",") for line in f if line.strip()]
                for docoup in docoups:
                    symbol, docoup_price, docoup_size, qty, order_id = docoup
                    docoup_price = float(docoup_price)
                    docoup_size = float(docoup_size)
                    qty = float(qty)
                    
                    response = session.get_order_history(category="linear", symbol=symbol, orderId=order_id)
                    if response["retCode"] == 0 and response["result"]["list"]:
                        order_status = response["result"]["list"][0]["orderStatus"]
                        if order_status == "Filled":
                            logging.info(f"{user_id}: Docoup order {order_id} for {symbol} filled at {docoup_price}, size: {docoup_size}, qty: {qty}")
                            with open(f"/var/www/phyton/bot/botttss/positions_{user_id}.txt", "a") as f:
                                f.write(f"{symbol},{docoup_price},{docoup_size},{qty},0\n")
                            docoups = [d for d in docoups if d[4] != order_id]
                            with open(f"/var/www/phyton/bot/botttss/pending_docoups_{user_id}.txt", "w") as f:
                                for d in docoups:
                                    f.write(",".join(d) + "\n")
            except FileNotFoundError:
                logging.info(f"{user_id}: No pending docoup orders file found")
            except Exception as e:
                logging.error(f"{user_id}: Error checking pending docoup orders: {e}")
            
            # Проверка тейк-профита
            for pos in positions:
                symbol = pos["symbol"]
                entry_price = pos["entry_price"]
                size = pos["size"]
                qty = pos["qty"]
                is_main = pos["is_main"]
                
                if symbol not in positions_prices:
                    logging.warning(f"{user_id}: No price data for {symbol}, skipping")
                    continue
                
                current_price, _ = positions_prices[symbol]
                pnl = (current_price - entry_price) / entry_price
                logging.info(f"{user_id}: Checking {symbol}, entry_price={entry_price}, current_price={current_price}, pnl={pnl:.4f}")
                
                if is_main and pnl >= TAKE_PROFIT_TRIGGER:
                    step_count = int((pnl - TAKE_PROFIT_TRIGGER) / TAKE_PROFIT_STEP)
                    take_profit_price = entry_price * (1 + TAKE_PROFIT_INITIAL + step_count * TAKE_PROFIT_PULL_STEP)
                    logging.info(f"{user_id}: {symbol} pnl={pnl:.4f}, setting take-profit at {take_profit_price} (step_count={step_count})")
                    set_take_profit(session, user_id, symbol, take_profit_price)
                elif is_main:
                    logging.info(f"{user_id}: {symbol} pnl={pnl:.4f}, not yet at take-profit trigger ({TAKE_PROFIT_TRIGGER})")
        
        time.sleep(15)

# Мониторинг signals.txt через watchdog
class SignalHandler(FileSystemEventHandler):
    def __init__(self, users):
        self.users = users
        self.last_modified = 0
        self.debounce_interval = 1
        self.processed_signals = set()
        logging.info("SignalHandler initialized")

    def process_existing_signals(self):
        try:
            with open("/var/www/phyton/bot/signals.txt", "r") as f:
                lines = f.readlines()
                logging.info(f"SignalHandler: Read {len(lines)} existing lines from signals.txt")
                unique_signals = set()
                for line in lines:
                    line = line.strip()
                    if not line:
                        logging.info("SignalHandler: Skipped empty line")
                        continue
                    ticker, drop, fr, is_oversold = parse_signal(line)
                    if ticker and (ticker, is_oversold, drop, fr) not in self.processed_signals:
                        logging.info(f"SignalHandler: Adding existing signal for {ticker}")
                        unique_signals.add((ticker, drop, fr, is_oversold))
                        self.processed_signals.add((ticker, is_oversold, drop, fr))
                    else:
                        logging.info(f"SignalHandler: Ignored duplicate or invalid existing signal: {line}")
                for ticker, drop, fr, is_oversold in unique_signals:
                    logging.info(f"SignalHandler: Processing existing signal for {ticker}")
                    process_signal(ticker, drop, fr, is_oversold, self.users)
        except Exception as e:
            logging.error(f"SignalHandler: Error reading existing signals.txt: {e}")

    def on_modified(self, event):
        if event.src_path.endswith("signals.txt"):
            current_time = time.time()
            if current_time - self.last_modified < self.debounce_interval:
                logging.info("Watchdog: Debounce interval, ignoring signal file change")
                return
            self.last_modified = current_time
            logging.info(f"Watchdog: Detected change in signals.txt at {event.src_path}")
            try:
                with open("/var/www/phyton/bot/signals.txt", "r") as f:
                    lines = f.readlines()
                    logging.info(f"Watchdog: Read {len(lines)} lines from signals.txt")
                    unique_signals = set()
                    for line in lines:
                        line = line.strip()
                        if not line:
                            logging.info("Watchdog: Skipped empty line")
                            continue
                        ticker, drop, fr, is_oversold = parse_signal(line)
                        if ticker and (ticker, is_oversold, drop, fr) not in self.processed_signals:
                            logging.info(f"Watchdog: Adding new signal for {ticker}")
                            unique_signals.add((ticker, drop, fr, is_oversold))
                            self.processed_signals.add((ticker, is_oversold, drop, fr))
                        else:
                            logging.info(f"Watchdog: Ignored duplicate or invalid signal: {line}")
                    for ticker, drop, fr, is_oversold in unique_signals:
                        logging.info(f"Watchdog: Processing signal for {ticker}")
                        process_signal(ticker, drop, fr, is_oversold, self.users)
                    self.processed_signals = {(t, o, d, f) for t, o, d, f in self.processed_signals if current_time - self.last_modified < 3600}
            except Exception as e:
                logging.error(f"Watchdog: Error reading signals.txt: {e}")

# Запуск
def start_monitoring():
    logging.info("Starting bot monitoring...")
    users = load_users()
    if not users:
        logging.error("No users loaded, exiting")
        return
    
    start_websocket()
    threading.Thread(target=check_positions, args=(users,), daemon=True).start()
    
    observer = Observer()
    signal_handler = SignalHandler(users)
    observer.schedule(signal_handler, path="/var/www/phyton/bot", recursive=False)
    observer.start()
    logging.info("Watchdog: Monitoring signals.txt started")
    
    signal_handler.process_existing_signals()
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down bot...")
        observer.stop()
        if ws_instance:
            try:
                ws_instance.close()
                logging.info("WebSocket closed on shutdown")
            except Exception as e:
                logging.error(f"Error closing WebSocket on shutdown: {e}")
    observer.join()

if __name__ == "__main__":
    start_monitoring()