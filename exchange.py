import asyncio
import random
import aiohttp
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
        
        try:
            async with self.limiter:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            for balance in data["balances"]:
                                if balance["asset"] == asset:
                                    return float(balance["free"])
                            return 0
                        else:
                            error_text = await response.text()
                            logger.error(f"Ошибка получения баланса {asset} (статус {response.status}): {error_text}")
                            return 0
        except aiohttp.ClientError as e_client:
            logger.error(f"Ошибка клиента aiohttp при получении баланса {asset}: {e_client}")
            return 0
        except Exception as e_general:
            logger.error(f"Непредвиденное исключение при получении баланса {asset}: {e_general}")
            return 0

    async def get_market_price(self):
        self.api_counter.record_request()
        endpoint = "/api/v3/ticker/price"
        params = {"symbol": "SOLUSDT"}
        url = f"https://api.mexc.com{endpoint}?{urlencode(params)}"
        
        try:
            async with self.limiter:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            price = float(data["price"])
                            logger.info(f"Текущая рыночная цена SOL/USDT: {price}")
                            return price
                        else:
                            error_text = await response.text()
                            logger.error(f"Ошибка получения рыночной цены (статус {response.status}): {error_text}")
                            return None
        except aiohttp.ClientError as e_client:
            logger.error(f"Ошибка клиента aiohttp при получении рыночной цены: {e_client}")
            return None
        except Exception as e_general:
            logger.error(f"Непредвиденное исключение при получении рыночной цены: {e_general}")
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
        
        base_delay = 0.5
        jitter = 0.25
        for attempt in range(retries):
            try:
                async with self.limiter:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(url, headers=headers) as response:
                            if response.status == 200:
                                data = await response.json()
                                logger.info(f"Ордер {side} ({order_type}) успешен: {data}, фактическая цена: {data.get('price', 'рыночная')}")
                                return data["orderId"], data.get("clientOrderId")  # Изменено: Возвращаем кортеж (orderId, clientOrderId)
                            elif response.status >= 500 or response.status == 429:
                                error_text = await response.text()
                                logger.error(f"Ошибка ордера {side} ({order_type}) (попытка {attempt + 1}/{retries}), статус {response.status}: {error_text}")
                                if attempt < retries - 1:
                                    delay = (2 ** attempt) * base_delay + random.uniform(0, jitter)
                                    await asyncio.sleep(delay)
                                    continue
                                else:
                                    break # Last attempt failed
                            elif response.status >= 400 and response.status < 500 and response.status != 429:
                                error_text = await response.text()
                                logger.error(f"Ошибка клиента при ордере {side} ({order_type}), статус {response.status}: {error_text}. Повторные попытки не выполняются.")
                                break  # No retry for these client errors
                            else: # Catch-all for other non-200 statuses if any
                                error_text = await response.text()
                                logger.error(f"Непредвиденная ошибка ордера {side} ({order_type}) (попытка {attempt + 1}/{retries}), статус {response.status}: {error_text}")
                                if attempt < retries - 1:
                                    delay = (2 ** attempt) * base_delay + random.uniform(0, jitter)
                                    await asyncio.sleep(delay)
                                    continue
                                else:
                                    break # Last attempt failed
            except aiohttp.ClientError as e:
                logger.error(f"Ошибка клиента aiohttp при размещении ордера {side} ({order_type}) (попытка {attempt + 1}/{retries}): {str(e)}")
                if attempt < retries - 1:
                    delay = (2 ** attempt) * base_delay + random.uniform(0, jitter)
                    await asyncio.sleep(delay)
                    continue
                else:
                    break # Last attempt failed
            except Exception as e:
                logger.error(f"Непредвиденное исключение при размещении ордера {side} ({order_type}) (попытка {attempt + 1}/{retries}): {str(e)}")
                break # Break on unexpected exception
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
        
        try:
            async with self.limiter:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.debug(f"Статус ордера {order_id}: {data}")
                            return data["status"], float(data["price"])
                        else:
                            error_text = await response.text()
                            logger.error(f"Ошибка проверки статуса ордера {order_id} (статус {response.status}): {error_text}")
                            return None, None
        except aiohttp.ClientError as e_client:
            logger.error(f"Ошибка клиента aiohttp при проверке статуса ордера {order_id}: {e_client}")
            return None, None
        except Exception as e_general:
            logger.error(f"Непредвиденное исключение при проверке статуса ордера {order_id}: {e_general}")
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
        
        try:
            async with self.limiter:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            logger.debug(f"Открытые ордера: {data}")
                            return [(order["orderId"], order["status"], float(order["price"]), order.get("clientOrderId")) for order in data if order.get("clientOrderId", "").startswith("BOT_")]  # Изменено: Фильтруем по clientOrderId и возвращаем clientOrderId
                        else:
                            error_text = await response.text()
                            logger.error(f"Ошибка получения открытых ордеров (статус {response.status}): {error_text}")
                            return []
        except aiohttp.ClientError as e_client:
            logger.error(f"Ошибка клиента aiohttp при получении открытых ордеров: {e_client}")
            return []
        except Exception as e_general:
            logger.error(f"Непредвиденное исключение при получении открытых ордеров: {e_general}")
            return []
