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
    filename="/var/www/phyton/bot/botttss/logs_debug.txt",
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

# Проверка тейк-профита и исполненных докупок с учетом leverage
def check_positions(users):
    while True:
        for user in users:
            user_id = user["user_id"]
            session = user["session"]
            
            logging.info(f"DEBUG: {user_id}: Starting position check...")
            
            balance = check_balance(session, user_id)
            if balance is None:
                logging.warning(f"{user_id}: Skipping position check due to balance error")
                continue

            # Проверка тейк-профита на основе реального PNL от API Bybit с учетом leverage
            try:
                logging.info(f"DEBUG: {user_id}: Getting positions from API...")
                
                # Получаем реальные позиции от API
                response = session.get_positions(category="linear", settleCoin="USDT")
                if response["retCode"] != 0:
                    logging.error(f"{user_id}: Failed to get positions from API: {response['retMsg']} (ErrCode: {response['retCode']})")
                    continue
                
                logging.info(f"DEBUG: {user_id}: Got {len(response['result']['list'])} positions from API")
                
                api_positions = response["result"]["list"]
                
                for api_pos in api_positions:
                    symbol = api_pos["symbol"]
                    size = float(api_pos["size"])
                    
                    logging.info(f"DEBUG: {user_id}: Processing {symbol}, size={size}")
                    
                    # Пропускаем позиции с нулевым размером
                    if size == 0:
                        continue
                    
                    # Получаем реальный PNL от API
                    unrealized_pnl = float(api_pos["unrealisedPnl"]) if api_pos["unrealisedPnl"] else 0.0
                    position_value = float(api_pos["positionValue"]) if api_pos["positionValue"] else 0.0
                    leverage = float(api_pos["leverage"]) if api_pos["leverage"] else 1.0  # По умолчанию 1x если пусто
                    
                    logging.info(f"DEBUG: {user_id}: {symbol} - unrealized_pnl={unrealized_pnl}, position_value={position_value}, leverage={leverage}")
                    
                    if position_value == 0:
                        logging.warning(f"DEBUG: {user_id}: {symbol} position_value is 0, skipping")
                        continue
                    
                    # Рассчитываем PNL в процентах от стоимости позиции с учетом плеча
                    pnl_percent_raw = unrealized_pnl / position_value
                    pnl_percent = pnl_percent_raw * leverage  # Умножаем на leverage
                    
                    avg_entry_price = float(api_pos["avgPrice"]) if api_pos["avgPrice"] else 0.0
                    current_price = float(api_pos["markPrice"]) if api_pos["markPrice"] else 0.0
                    
                    logging.info(f"DEBUG: {user_id}: {symbol} - pnl_percent_raw={pnl_percent_raw:.6f}, pnl_percent={pnl_percent:.6f}")
                    
                    # ПРАВИЛЬНАЯ СТРОКА ЛОГИРОВАНИЯ
                    logging.info(f"{user_id}: Checking {symbol}, avg_entry_price={avg_entry_price}, current_price={current_price}, unrealized_pnl={unrealized_pnl:.4f} USDT, leverage={leverage}x, pnl_percent={pnl_percent:.4f} ({pnl_percent*100:.2f}%)")
                    
                    # Если PNL достиг триггера (15%), начинаем логику тейк-профита
                    if pnl_percent >= TAKE_PROFIT_TRIGGER:
                        logging.info(f"DEBUG: {user_id}: {symbol} PNL {pnl_percent:.4f} >= trigger {TAKE_PROFIT_TRIGGER}, setting take-profit")
                        
                        # Рассчитываем текущий уровень тейк-профита
                        steps_above_trigger = int((pnl_percent - TAKE_PROFIT_TRIGGER) / TAKE_PROFIT_STEP)
                        current_take_profit_level = TAKE_PROFIT_INITIAL + steps_above_trigger * TAKE_PROFIT_STEP
                        
                        logging.info(f"DEBUG: {user_id}: {symbol} take-profit level: {current_take_profit_level:.4f} ({current_take_profit_level*100:.2f}%)")
                        
                        # Получаем параметры для ордера
                        try:
                            response_ticker = session.get_instruments_info(category="linear", symbol=symbol)
                            if response_ticker["retCode"] == 0 and response_ticker["result"]["list"]:
                                instrument = response_ticker["result"]["list"][0]
                                min_order_qty = float(instrument.get("lotSizeFilter", {}).get("minOrderQty", 0.001))
                                max_order_qty = float(instrument.get("lotSizeFilter", {}).get("maxOrderQty", 1000000))
                                lot_step_size = float(instrument.get("lotSizeFilter", {}).get("lotStepSize", 1.0))
                                
                                # Устанавливаем новый тейк-профит
                                set_take_profit_result = set_take_profit_order_by_pnl(session, user_id, symbol, size, avg_entry_price, current_take_profit_level, min_order_qty, max_order_qty, lot_step_size)
                                
                                if set_take_profit_result:
                                    logging.info(f"{user_id}: {symbol} PNL={pnl_percent:.4f} ({pnl_percent*100:.2f}%), updated take-profit to {current_take_profit_level*100:.1f}%")
                                else:
                                    logging.error(f"{user_id}: {symbol} Failed to set take-profit order")
                            else:
                                logging.error(f"{user_id}: Failed to get ticker info for {symbol}")
                        except Exception as ticker_error:
                            logging.error(f"{user_id}: Error getting ticker info for {symbol}: {ticker_error}")
                    
                    elif pnl_percent >= TAKE_PROFIT_INITIAL:
                        logging.info(f"{user_id}: {symbol} PNL={pnl_percent:.4f} ({pnl_percent*100:.2f}%), approaching take-profit trigger ({TAKE_PROFIT_TRIGGER*100:.1f}%)")
                    
                    else:
                        logging.info(f"DEBUG: {user_id}: {symbol} PNL {pnl_percent:.4f} < trigger {TAKE_PROFIT_TRIGGER}, no action")
            
            except Exception as e:
                logging.error(f"{user_id}: CRITICAL ERROR in position check: {e}")
                import traceback
                logging.error(f"{user_id}: Traceback: {traceback.format_exc()}")
        
        time.sleep(15)

# Установка тейк-профита на основе PNL с учетом leverage
def set_take_profit_order_by_pnl(session, user_id, symbol, qty, entry_price, take_profit_pnl_percent, min_order_qty, max_order_qty, lot_step_size):
    try:
        # Рассчитываем цену тейк-профита на основе процента PNL от entry_price
        take_profit_price = entry_price * (1 + take_profit_pnl_percent)
        
        logging.info(f"DEBUG: {user_id}: Setting take-profit for {symbol} at {take_profit_price} (PNL: {take_profit_pnl_percent*100:.2f}%)")

        # Округляем количество в соответствии с требованиями API
        qty = max(qty, min_order_qty)
        qty = min(qty, max_order_qty)
        qty = min_order_qty + (round((qty - min_order_qty) / lot_step_size) * lot_step_size)

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
        logging.info(f"{user_id}: ✅ Set take-profit order for {symbol} at {take_profit_price} (PNL: {take_profit_pnl_percent*100:.1f}%), qty: {qty}, orderId: {order_id}")

        # Сохраняем информацию о тейк-профите
        with open(f"/var/www/phyton/bot/botttss/take_profit_orders_{user_id}.txt", "a") as f:
            f.write(f"{symbol},{take_profit_price},{qty},{order_id},{take_profit_pnl_percent}\n")

        return True
    except Exception as e:
        logging.error(f"{user_id}: Error setting take-profit order for {symbol}: {e}")
        return False

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

# Запуск
def start_monitoring():
    logging.info("🐛 Starting DEBUG bot monitoring v2.0 with leverage support...")
    users = load_users()
    if not users:
        logging.error("No users loaded, exiting")
        return

    threading.Thread(target=check_positions, args=(users,), daemon=True).start()
    logging.info("✅ Position check thread started")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down debug bot...")

if __name__ == "__main__":
    start_monitoring()