import asyncio
import requests
import hmac
import hashlib
import time
from urllib.parse import urlencode
from aiolimiter import AsyncLimiter
from loguru import logger
from config import send_notification
from utils import APICounter

class MEXCExchange:
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key
        self.limiter = AsyncLimiter(120, 60)
        self.api_counter = APICounter()

    def sign_request(self, params):
        query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        signature = hmac.new(
            self.secret_key.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        logger.debug(f"Параметры запроса: {query}, Подпись: {signature}")
        return signature, query

    async def get_balance(self, asset):
        self.api_counter.record_request()
        endpoint = "/api/v3/account"
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp}
        signature, query = self.sign_request(params)
        headers = {"X-MEXC-APIKEY": self.api_key}
        url = f"https://api.mexc.com{endpoint}?{query}&signature={signature}"
        
        async with self.limiter:
            response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            for balance in data["balances"]:
                if balance["asset"] == asset:
                    return float(balance["free"])
            return 0
        logger.error(f"Ошибка получения баланса {asset}: {response.text}")
        return 0

    async def get_market_price(self):
        self.api_counter.record_request()
        endpoint = "/api/v3/ticker/price"
        params = {"symbol": "SOLUSDT"}
        url = f"https://api.mexc.com{endpoint}?{urlencode(params)}"
        
        async with self.limiter:
            response = requests.get(url)
        if response.status_code == 200:
            data = response.json()
            price = float(data["price"])
            logger.info(f"Текущая рыночная цена SOL/USDT: {price}")
            return price
        logger.error(f"Ошибка получения рыночной цены: {response.text}")
        return None

    async def place_order(self, side, quantity, price=None, order_type="LIMIT", retries=3, telegram_app=None, client_order_id=None):
        self.api_counter.record_request()
        endpoint = "/api/v3/order"
        timestamp = int(time.time() * 1000)
        params = {
            "symbol": "SOLUSDT",
            "side": side,
            "type": order_type,
            "quantity": round(quantity, 2),
            "timestamp": timestamp,
        }
        if order_type == "LIMIT" and price is not None:
            params["price"] = round(price, 2)
        if client_order_id:  # Изменено: Добавляем newClientOrderId, если указан
            params["newClientOrderId"] = client_order_id
        signature, query = self.sign_request(params)
        headers = {"X-MEXC-APIKEY": self.api_key}
        url = f"https://api.mexc.com{endpoint}?{query}&signature={signature}"
        
        for attempt in range(retries):
            try:
                async with self.limiter:
                    response = requests.post(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    logger.info(f"Ордер {side} ({order_type}) успешен: {data}, фактическая цена: {data.get('price', 'рыночная')}")
                    return data["orderId"], data.get("clientOrderId")  # Изменено: Возвращаем кортеж (orderId, clientOrderId)
                else:
                    logger.error(f"Ошибка ордера {side} ({order_type}): {response.text}")
                    if attempt < retries - 1:
                        await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Исключение при размещении ордера {side} ({order_type}): {str(e)}")
                if attempt < retries - 1:
                    await asyncio.sleep(1)
        if telegram_app:
            await send_notification(
                telegram_app,
                f"⚠️ Не удалось разместить ордер {side} ({order_type}): {quantity} SOL"
            )
        return None, None  # Изменено: Возвращаем None для обоих значений в случае ошибки

    async def check_order_status(self, order_id):
        self.api_counter.record_request()
        endpoint = "/api/v3/order"
        timestamp = int(time.time() * 1000)
        params = {
            "symbol": "SOLUSDT",
            "orderId": order_id,
            "timestamp": timestamp,
        }
        signature, query = self.sign_request(params)
        headers = {"X-MEXC-APIKEY": self.api_key}
        url = f"https://api.mexc.com{endpoint}?{query}&signature={signature}"
        
        async with self.limiter:
            response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            logger.debug(f"Статус ордера {order_id}: {data}")
            return data["status"], float(data["price"])
        logger.error(f"Ошибка проверки статуса ордера {order_id}: {response.text}")
        return None, None

    async def get_open_orders(self):
        self.api_counter.record_request()
        endpoint = "/api/v3/openOrders"
        timestamp = int(time.time() * 1000)
        params = {
            "symbol": "SOLUSDT",
            "timestamp": timestamp,
        }
        signature, query = self.sign_request(params)
        headers = {"X-MEXC-APIKEY": self.api_key}
        url = f"https://api.mexc.com{endpoint}?{query}&signature={signature}"
        
        async with self.limiter:
            response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            logger.debug(f"Открытые ордера: {data}")
            return [(order["orderId"], order["status"], float(order["price"]), order.get("clientOrderId")) for order in data if order.get("clientOrderId", "").startswith("BOT_")]  # Изменено: Фильтруем по clientOrderId и возвращаем clientOrderId
        logger.error(f"Ошибка получения открытых ордеров: {response.text}")
        return []