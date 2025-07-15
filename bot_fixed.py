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
TAKE_PROFIT_TRIGGER = 0.15  # Триггер тейк-профита на +15% PNL
TAKE_PROFIT_INITIAL = 0.10  # Начальный тейк-профит +10% PNL
TAKE_PROFIT_STEP = 0.02     # Шаг тащения тейк-профита +2%
TAKE_PROFIT_PULL_STEP = 0.02  # Шаг подтягивания тейк-профита
WEBSOCKET_RESUBSCRIBE_INTERVAL = 30  # Переподписка WebSocket каждые 30 секунд

# Глобальные переменные
positions_prices = {}
ws_instance = None
current_subscriptions = set()
last_resubscribe_time = 0

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

# Отмена всех лимитных ордеров для символа
def cancel_all_orders_for_symbol(session, user_id, symbol):
    try:
        # Отменяем все открытые ордера для символа
        response = session.get_open_orders(category="linear", symbol=symbol)
        if response["retCode"] == 0 and response["result"]["list"]:
            for order in response["result"]["list"]:
                order_id = order["orderId"]
                cancel_response = session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
                if cancel_response["retCode"] == 0:
                    logging.info(f"{user_id}: Canceled order {order_id} for {symbol}")
                else:
                    logging.error(f"{user_id}: Failed to cancel order {order_id} for {symbol}: {cancel_response['retMsg']}")
    except Exception as e:
        logging.error(f"{user_id}: Error canceling orders for {symbol}: {e}")

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

# Установка тейк-профита на основе PNL
def set_take_profit_order_by_pnl(session, user_id, symbol, qty, entry_price, take_profit_pnl_percent, min_order_qty, max_order_qty, lot_step_size):
    try:
        # Рассчитываем цену тейк-профита на основе процента PNL от entry_price
        take_profit_price = entry_price * (1 + take_profit_pnl_percent)
        
        # Округляем количество в соответствии с требованиями API
        qty = max(qty, min_order_qty)
        qty = min(qty, max_order_qty)
        qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)
        
        # Проверим, есть ли уже активные take-profit ордера
        existing_tp_orders = get_existing_take_profit_orders(session, user_id, symbol)
        
        # Если уже есть take-profit ордер с такой же ценой, не создаем дублирующий
        for existing_order in existing_tp_orders:
            existing_price = float(existing_order["price"])
            if abs(existing_price - take_profit_price) < 0.01:  # Допуск 0.01
                logging.info(f"{user_id}: Take-profit order for {symbol} already exists at {existing_price} (target: {take_profit_price})")
                return True
        
        # Сначала отменяем существующие тейк-профит ордера (для обновления уровня)
        cancel_take_profit_orders(session, user_id, symbol)
        
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
            logging.error(f"{user_id}: Failed to set take-profit order for {symbol} at {take_profit_price} (PNL: {take_profit_pnl_percent*100:.1f}%): {response['retMsg']} (ErrCode: {response['retCode']})")
            return False
        order_id = response["result"]["orderId"]
        logging.info(f"{user_id}: Set take-profit order for {symbol} at {take_profit_price} (PNL: {take_profit_pnl_percent*100:.1f}%), qty: {qty}, orderId: {order_id}")
        
        # Сохраняем информацию о тейк-профите
        with open(f"/var/www/phyton/bot/botttss/take_profit_orders_{user_id}.txt", "a") as f:
            f.write(f"{symbol},{take_profit_price},{qty},{order_id},{take_profit_pnl_percent}\n")
        
        return True
    except Exception as e:
        logging.error(f"{user_id}: Error setting take-profit order for {symbol}: {e}")
        return False

# Получение существующих take-profit ордеров
def get_existing_take_profit_orders(session, user_id, symbol):
    try:
        response = session.get_open_orders(category="linear", symbol=symbol)
        if response["retCode"] != 0:
            logging.error(f"{user_id}: Failed to get open orders for {symbol}: {response['retMsg']} (ErrCode: {response['retCode']})")
            return []
        
        # Фильтруем только reduceOnly ордера (take-profit)
        take_profit_orders = []
        for order in response["result"]["list"]:
            if order.get("reduceOnly") == True and order.get("side") == "Sell":
                take_profit_orders.append(order)
        
        return take_profit_orders
    except Exception as e:
        logging.error(f"{user_id}: Error getting existing take-profit orders for {symbol}: {e}")
        return []

# Отмена тейк-профит ордеров
def cancel_take_profit_orders(session, user_id, symbol):
    try:
        # Читаем файл с тейк-профит ордерами
        if os.path.exists(f"/var/www/phyton/bot/botttss/take_profit_orders_{user_id}.txt"):
            with open(f"/var/www/phyton/bot/botttss/take_profit_orders_{user_id}.txt", "r") as f:
                tp_orders = [line.strip().split(",") for line in f if line.strip()]
            
            # Отменяем ордера для данного символа
            for tp_order in tp_orders:
                if tp_order[0] == symbol:
                    order_id = tp_order[3]
                    response = session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
                    if response["retCode"] == 0:
                        logging.info(f"{user_id}: Canceled take-profit order {order_id} for {symbol}")
                    else:
                        logging.error(f"{user_id}: Failed to cancel take-profit order {order_id} for {symbol}: {response['retMsg']}")
            
            # Обновляем файл, убирая отмененные ордера
            with open(f"/var/www/phyton/bot/botttss/take_profit_orders_{user_id}.txt", "w") as f:
                for tp_order in tp_orders:
                    if tp_order[0] != symbol:
                        f.write(",".join(tp_order) + "\n")
    except Exception as e:
        logging.error(f"{user_id}: Error canceling take-profit orders for {symbol}: {e}")

# Открытие позиции (без автоматического тейк-профита)
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
            position_size = balance * (POSITION_SIZE_PERCENT / 100)
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
        
        # Для основной позиции выставляем докупку (тейк-профит будет установлен позже на основе PNL)
        if not is_docoup:
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
        
        # Отменяем все ордера для символа
        cancel_docoup_orders(session, user_id, symbol)
        cancel_take_profit_orders(session, user_id, symbol)
        
        positions = load_positions(user_id)
        positions = [p for p in positions if p["symbol"] != symbol or abs(float(p["qty"]) - float(qty)) > 1e-6]
        save_positions(user_id, positions)
        
        with open(f"/var/www/phyton/bot/botttss/blacklist_{user_id}.txt", "a") as f:
            f.write(f"{symbol},{time.time()}\n")
        
        return True
    except Exception as e:
        logging.error(f"{user_id}: Error closing position for {symbol}: {e}")
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

# Получение актуальных тикеров для подписки
def get_active_tickers():
    active_tickers = set()
    users = load_users()
    for user in users:
        positions = load_positions(user["user_id"])
        for pos in positions:
            active_tickers.add(pos["symbol"])
    return active_tickers

# Переподписка WebSocket на актуальные тикеры
def resubscribe_websocket():
    global ws_instance, current_subscriptions, last_resubscribe_time
    
    current_time = time.time()
    if current_time - last_resubscribe_time < WEBSOCKET_RESUBSCRIBE_INTERVAL:
        return
    
    last_resubscribe_time = current_time
    
    if not ws_instance:
        return
    
    try:
        new_tickers = get_active_tickers()
        
        # Если набор тикеров изменился, переподписываемся
        if new_tickers != current_subscriptions:
            logging.info(f"WebSocket: Resubscribing from {current_subscriptions} to {new_tickers}")
            
            # Отписываемся от старых тикеров
            if current_subscriptions:
                unsubscribe_args = [f"publicTrade.{t}" for t in current_subscriptions]
                ws_instance.send(json.dumps({"op": "unsubscribe", "args": unsubscribe_args}))
            
            # Подписываемся на новые тикеры
            if new_tickers:
                subscribe_args = [f"publicTrade.{t}" for t in new_tickers]
                ws_instance.send(json.dumps({"op": "subscribe", "args": subscribe_args}))
                logging.info(f"WebSocket: Subscribed to {new_tickers}")
            
            current_subscriptions = new_tickers.copy()
    except Exception as e:
        logging.error(f"WebSocket: Error resubscribing: {e}")

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

# Парсинг сигнала
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
            timestamp = time.time()
            positions_prices[symbol] = (price, timestamp)
            
            # Логируем обновления цен только раз в 3 секунды для каждого символа
            if symbol not in getattr(on_message, 'last_log_time', {}):
                on_message.last_log_time = {}
            
            if timestamp - on_message.last_log_time.get(symbol, 0) > 3:
                logging.info(f"WebSocket: Updated price for {symbol}: {price}")
                on_message.last_log_time[symbol] = timestamp
                
        # Периодически переподписываемся на актуальные тикеры
        resubscribe_websocket()
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
    global ws_instance, current_subscriptions
    logging.info("WebSocket opened")
    ws_instance = ws
    
    # Подписываемся на актуальные тикеры
    active_tickers = get_active_tickers()
    if active_tickers:
        subscribe_args = [f"publicTrade.{t}" for t in active_tickers]
        ws.send(json.dumps({"op": "subscribe", "args": subscribe_args}))
        logging.info(f"WebSocket: Subscribed to {active_tickers}")
        current_subscriptions = active_tickers.copy()
    else:
        logging.warning("WebSocket: No active tickers to subscribe to")

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

# Получение средней цены входа для позиции
def get_position_average_price(user_id, symbol):
    positions = load_positions(user_id)
    symbol_positions = [p for p in positions if p["symbol"] == symbol]
    
    if not symbol_positions:
        return None
    
    total_size = sum(p["size"] for p in symbol_positions)
    if total_size == 0:
        return None
    
    weighted_avg = sum(p["entry_price"] * p["size"] for p in symbol_positions) / total_size
    return weighted_avg

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
            
            # Проверка тейк-профита на основе реального PNL от API Bybit
            try:
                # Получаем реальные позиции от API
                response = session.get_positions(category="linear", settleCoin="USDT")
                if response["retCode"] != 0:
                    logging.error(f"{user_id}: Failed to get positions from API: {response['retMsg']} (ErrCode: {response['retCode']})")
                    continue
                
                api_positions = response["result"]["list"]
                
                for api_pos in api_positions:
                    symbol = api_pos["symbol"]
                    size = float(api_pos["size"])
                    
                    # Пропускаем позиции с нулевым размером
                    if size == 0:
                        continue
                    
                    # Получаем реальный PNL от API
                    unrealized_pnl = float(api_pos["unrealisedPnl"])
                    position_value = float(api_pos["positionValue"])
                    
                    if position_value == 0:
                        continue
                    
                    # Рассчитываем PNL в процентах от стоимости позиции
                    pnl_percent = unrealized_pnl / position_value
                    avg_entry_price = float(api_pos["avgPrice"])
                    current_price = float(api_pos["markPrice"])
                    
                    logging.info(f"{user_id}: Checking {symbol}, avg_entry_price={avg_entry_price}, current_price={current_price}, unrealized_pnl={unrealized_pnl:.4f} USDT, pnl_percent={pnl_percent:.4f} ({pnl_percent*100:.2f}%)")
                    
                    # Если PNL достиг триггера (15%), начинаем логику тейк-профита
                    if pnl_percent >= TAKE_PROFIT_TRIGGER:
                        # Рассчитываем текущий уровень тейк-профита
                        steps_above_trigger = int((pnl_percent - TAKE_PROFIT_TRIGGER) / TAKE_PROFIT_STEP)
                        current_take_profit_level = TAKE_PROFIT_INITIAL + steps_above_trigger * TAKE_PROFIT_STEP
                        
                        # Получаем параметры для ордера
                        available, min_order_qty, max_order_qty, lot_step_size = check_ticker(session, symbol, user_id)
                        if available:
                            # Устанавливаем новый тейк-профит
                            set_take_profit_order_by_pnl(session, user_id, symbol, size, avg_entry_price, current_take_profit_level, min_order_qty, max_order_qty, lot_step_size)
                            logging.info(f"{user_id}: {symbol} PNL={pnl_percent:.4f} ({pnl_percent*100:.2f}%), updated take-profit to {current_take_profit_level*100:.1f}%")
                    
                    elif pnl_percent >= TAKE_PROFIT_INITIAL:
                        logging.info(f"{user_id}: {symbol} PNL={pnl_percent:.4f} ({pnl_percent*100:.2f}%), approaching take-profit trigger ({TAKE_PROFIT_TRIGGER*100:.1f}%)")
            
            except Exception as e:
                logging.error(f"{user_id}: Error checking positions for take-profit: {e}")
        
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