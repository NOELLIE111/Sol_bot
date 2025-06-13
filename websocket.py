from datetime import datetime
import hashlib
import hmac
import os
import aiohttp
import asyncio
import json
import time
from loguru import logger
from config import settings, send_notification
from exchange import MEXCExchange
import PrivateOrdersV3Api_pb2 as orders_pb2
import PrivateDealsV3Api_pb2 as deals_pb2
from urllib.parse import urlencode

class MEXCWebSocket:
    def __init__(self, on_price_update, trading_bot):
        self.price_url = "wss://wbs.mexc.com/ws"
        self.on_price_update = on_price_update
        self.trading_bot = trading_bot
        self.session = None
        self.ws = None
        self.reconnect_delay = 5
        self.last_price = None
        self.last_logged_price = None
        self.last_message_time = time.time()  # Для отслеживания активности
        self.exchange = MEXCExchange(os.getenv("MEXC_API_KEY"), os.getenv("MEXC_SECRET_KEY"))
        self.notification_queue = asyncio.Queue()
        self.api_key = os.getenv("MEXC_API_KEY")
        self.api_secret = os.getenv("MEXC_SECRET_KEY")
        self.listen_key = None

    async def create_listen_key(self):
        """Создаёт listenKey через REST API."""
        url = "https://api.mexc.com/api/v3/userDataStream"
        timestamp = int(time.time() * 1000)
        params = {"timestamp": timestamp, "recvWindow": 10000}
        query_string = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(), query_string.encode(), hashlib.sha256
        ).hexdigest()
        headers = {"X-MEXC-APIKEY": self.api_key, "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, params={**params, "signature": signature}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.listen_key = data["listenKey"]
                    logger.info(f"Создан listenKey: {self.listen_key[:10]}...")
                    return self.listen_key
                else:
                    error_text = await resp.text()
                    logger.error(f"Ошибка создания listenKey: HTTP {resp.status} — {error_text}")
                    return None

    async def extend_listen_key(self):
        """Продлевает listenKey."""
        url = "https://api.mexc.com/api/v3/userDataStream"
        timestamp = int(time.time() * 1000)
        params = {"listenKey": self.listen_key, "timestamp": timestamp, "recvWindow": 10000}
        signature = hmac.new(
            self.api_secret.encode(), urlencode(params).encode(), hashlib.sha256
        ).hexdigest()
        headers = {"X-MEXC-APIKEY": self.api_key, "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, params={**params, "signature": signature}) as resp:
                if resp.status == 200:
                    logger.info(f"ListenKey продлён: {self.listen_key[:10]}...")
                    return True
                else:
                    error_text = await resp.text()
                    logger.error(f"Ошибка продления listenKey: HTTP {resp.status} —Nicolas_text")
                    return False

    async def keepalive_task(self):
        """Продлевает listenKey каждые 30 минут."""
        while True:
            await asyncio.sleep(1800)  # 30 минут
            if self.listen_key:
                success = await self.extend_listen_key()
                if not success:
                    logger.warning("Не удалось продлить listenKey, создаём новый")
                    await self.create_listen_key()

    async def ping_task(self):
        """Отправляет пинг-сообщения каждые 30 секунд для поддержания активности WebSocket."""
        while True:
            try:
                if self.ws and not self.ws.closed:
                    await self.ws.send_json({"method": "PING", "id": int(time.time() * 1000)})
                    logger.debug("Отправлено пинг-сообщение WebSocket")
                await asyncio.sleep(30)  # Пинг каждые 30 секунд
            except Exception as e:
                logger.error(f"Ошибка отправки пинг-сообщения: {str(e)}")
                break

    async def parse_binary_message(self, message: bytes, channel: str):
        """Парсит бинарное Protobuf-сообщение."""
        logger.debug(f"Сырое сообщение: {message.hex()}")
        try:
            channel_length = message[1]
            pair_length_offset = 2 + channel_length
            pair_length = message[pair_length_offset + 1]
            # Извлекаем symbol (пару) из заголовка
            pair_start = pair_length_offset + 2
            symbol = message[pair_start:pair_start + pair_length].decode('utf-8')
            # Фильтруем только SOLUSDT
            if symbol != "SOLUSDT":
                logger.debug(f"Игнорируем сообщение для пары {symbol}, ожидаем SOLUSDT")
                return None
            # Пропускаем канал, пару и 10 байт метаданных
            protobuf_start = pair_length_offset + 2 + pair_length + 10
            data_bytes = message[protobuf_start:]
            logger.debug(f"Protobuf-данные: {data_bytes.hex()}")

            if channel == "spot@private.orders.v3.api.pb":
                order_data = orders_pb2.PrivateOrdersV3Api()
                order_data.ParseFromString(data_bytes)
                logger.debug(f"OrderPush: {order_data}")
                # Безопасно проверяем наличие clientOrderId
                client_order_id = getattr(order_data, 'clientOrderId', '')
                if not client_order_id or not client_order_id.startswith("BOT_"):
                    logger.debug(f"Игнорируем ордер {order_data.id}, clientOrderId={client_order_id} не начинается с BOT_ или отсутствует")
                    return None
                order_type = "LIMIT" if order_data.orderType == 1 else "MARKET"
                trade_type = "BUY" if order_data.tradeType == 1 else "SELL"
                status = {1: "NEW", 2: "FILLED", 3: "PARTIALLY_FILLED", 4: "CANCELED", 5: "REJECTED"}.get(order_data.status, "UNKNOWN")
                # Используем cumulativeQuantity, если quantity == "0"
                quantity = order_data.quantity if order_data.quantity != "0" else order_data.cumulativeQuantity
                if quantity == "0":
                    logger.warning(f"Quantity is 0 for orderId={order_data.id}, using cumulativeQuantity={order_data.cumulativeQuantity}")
                return {
                    "type": "OrderPush",
                    "orderId": order_data.id,
                    "symbol": order_data.market if order_data.HasField("market") else symbol,
                    "orderType": order_type,
                    "side": trade_type,
                    "price": order_data.price,
                    "quantity": quantity,
                    "status": status,
                    "createdTime": order_data.createTime / 1000,
                    "avgPrice": order_data.avgPrice,
                    "cumQty": order_data.cumulativeQuantity,
                    "cumAmt": order_data.cumulativeAmount,
                    "clientOrderId": client_order_id
                }

            elif channel == "spot@private.deals.v3.api.pb":
                deal_data = deals_pb2.PrivateDealsV3Api()
                deal_data.ParseFromString(data_bytes)
                logger.debug(f"DealPush: {deal_data}")
                # Безопасно проверяем наличие clientOrderId
                client_order_id = getattr(deal_data, 'clientOrderId', '')
                if not client_order_id or not client_order_id.startswith("BOT_"):
                    logger.debug(f"Игнорируем сделку для orderId={deal_data.orderId}, clientOrderId={client_order_id} не начинается с BOT_ или отсутствует")
                    return None
                trade_type = "BUY" if deal_data.tradeType == 1 else "SELL"
                return {
                    "type": "DealPush",
                    "orderId": deal_data.orderId,
                    "clientOrderId": client_order_id,
                    "tradeId": deal_data.tradeId,
                    "side": trade_type,
                    "price": deal_data.price,
                    "quantity": deal_data.quantity,
                    "amount": deal_data.amount,
                    "feeAmount": deal_data.feeAmount,
                    "feeCurrency": deal_data.feeCurrency,
                    "tradeTime": deal_data.time / 1000,
                    "symbol": symbol
                }
            else:
                logger.warning(f"Неизвестный канал: {channel}")
                return None

        except Exception as e:
            logger.error(f"Ошибка парсинга для {channel}: {e}, сообщение: {message.hex()}")
            return None

    async def connect(self):
        """Подключается к WebSocket и подписывается на каналы."""
        self.notification_task = asyncio.create_task(self.process_notifications())
        self.listen_key = await self.create_listen_key()
        if not self.listen_key:
            logger.error("Не удалось создать listenKey, завершаем")
            return
        asyncio.create_task(self.keepalive_task())

        while True:
            try:
                self.session = aiohttp.ClientSession()
                self.ws = await self.session.ws_connect(f"{self.price_url}?listenKey={self.listen_key}")
                logger.info("WebSocket для цен подключен")
                # Подписка на канал цен
                await self.ws.send_json({
                    "method": "SUBSCRIPTION",
                    "params": ["spot@public.bookTicker.v3.api@SOLUSDT"],
                    "id": 1
                })
                logger.debug("Подписка на канал цен отправлена: spot@public.bookTicker.v3.api@SOLUSDT")
                # Подписка на каналы ордеров
                await self.ws.send_json({
                    "method": "SUBSCRIPTION",
                    "params": ["spot@private.orders.v3.api.pb"],
                    "id": 2
                })
                await self.ws.send_json({
                    "method": "SUBSCRIPTION",
                    "params": ["spot@private.deals.v3.api.pb"],
                    "id": 3
                })
                logger.info("WebSocket для ордеров подключен")
                logger.debug("Подписка на каналы ордеров отправлена: spot@private.orders.v3.api.pb, spot@private.deals.v3.api.pb")
                await self.handle_messages()
            except Exception as e:
                logger.error(f"Ошибка WebSocket: {str(e)}. Переподключение через {self.reconnect_delay} сек")
                await asyncio.sleep(self.reconnect_delay)
            except asyncio.CancelledError:
                logger.info("WebSocket остановлен")
                break
            finally:
                await self.close()

    async def handle_messages(self):
        """Обрабатывает входящие сообщения WebSocket."""
        try:
            async for msg in self.ws:
                self.last_message_time = time.time()  # Обновляем время последнего сообщения
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if "c" in data and data["c"] == "spot@public.bookTicker.v3.api@SOLUSDT":
                            price = float(data["d"]["b"])
                            if self.last_price != price:
                                self.last_price = price
                                drop_trigger = await self.on_price_update(price)
                                if self.last_logged_price is None or abs(price - self.last_logged_price) >= 0.1:
                                    self.last_logged_price = price
                                    # Добавить задержку для синхронизации с записью в order.json
                                    await asyncio.sleep(1)
                                    # Найти ближайший активный ордер на продажу
                                    nearest_order = None
                                    try:
                                        with open("order.json", "r", encoding="utf-8") as f:
                                            content = f.read().strip()
                                            orders = json.loads(content) if content else []
                                        logger.debug(f"Загружено ордеров из order.json: {len(orders)}, IDs: {[o.get('order_id', 'unknown') for o in orders]}")
                                        active_sell_orders = []
                                        for o in orders:
                                            # Проверяем корректность order_id и обязательных полей
                                            if not isinstance(o.get("order_id"), str):
                                                logger.error(f"Некорректный order_id в ордере: {o.get('order_id')}, пропускаем")
                                                continue
                                            if not all(key in o for key in ["status", "side", "type", "quantity", "price", "client_order_id"]):
                                                logger.error(f"Отсутствуют обязательные поля в ордере {o.get('order_id')}: {o}, пропускаем")
                                                continue
                                            # Проверяем возможность преобразования quantity и price в float
                                            try:
                                                quantity = float(o["quantity"])
                                                price_val = float(o["price"])
                                            except (ValueError, TypeError) as e:
                                                logger.error(f"Некорректные quantity или price в ордере {o.get('order_id')}: quantity={o['quantity']}, price={o['price']}, ошибка: {str(e)}")
                                                continue
                                            # Фильтруем активные ордера на продажу
                                            if (o["status"] == "active"
                                                and o["side"] == "SELL"
                                                and o["type"] == "LIMIT"
                                                and o.get("client_order_id", "").startswith("BOT_")
                                                and quantity > 0):
                                                active_sell_orders.append(o)
                                        logger.debug(f"Найдено активных ордеров на продажу: {len(active_sell_orders)}, ордера: {[o['order_id'] for o in active_sell_orders]}")
                                        if active_sell_orders:
                                            nearest_order = min(
                                                active_sell_orders,
                                                key=lambda o: abs(float(o["price"]) - price)
                                            )
                                            logger.debug(f"Ближайший ордер на продажу: order_id={nearest_order['order_id']}, price={nearest_order['price']}, quantity={nearest_order['quantity']}")
                                        else:
                                            logger.debug("Активные ордера на продажу не найдены")
                                    except json.JSONDecodeError as e:
                                        logger.error(f"Ошибка декодирования order.json: {str(e)}")
                                    except Exception as e:
                                        logger.error(f"Ошибка чтения order.json для поиска ближайшего ордера: {str(e)}")

                                    nearest_order_info = (
                                        f", Ближайший ордер на продажу: {nearest_order['quantity']} SOL по {float(nearest_order['price']):.2f} USDT"
                                        if nearest_order else ", Нет активных ордеров на продажу"
                                    )
                                    logger.info(
                                        f"Новая цена SOL/USDT: {price:.2f} USDT, цель покупки: {drop_trigger:.2f} USDT{nearest_order_info}"
                                        if drop_trigger is not None
                                        else f"Новая цена SOL/USDT: {price:.2f} USDT, цель покупки: не установлена{nearest_order_info}"
                                    )
                        else:
                            logger.debug(f"Получен текстовый ответ от MEXC: {data}")
                            if "msg" in data:
                                logger.info(f"Подписка подтверждена: {data['msg']}")
                    except json.JSONDecodeError as e:
                        logger.error(f"Ошибка декодирования текстового сообщения: {str(e)}")
                    except Exception as e:
                        logger.error(f"Ошибка обработки текстового сообщения: {str(e)}")
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    try:
                        channel_length = msg.data[1]
                        channel = msg.data[2:2 + channel_length].decode('utf-8')
                        logger.debug(f"Канал: {channel}, Длина канала: {channel_length}")
                        data = await self.parse_binary_message(msg.data, channel)
                        if not data:
                            logger.debug(f"Сообщение отфильтровано или не распарсено: channel={channel}")
                            continue
                        # Дополнительная проверка clientOrderId
                        if not data.get("clientOrderId", "").startswith("BOT_"):
                            logger.debug(f"Игнорируем событие {data['type']} для orderId={data['orderId']}, clientOrderId={data.get('clientOrderId')} не начинается с BOT_")
                            continue
                        # Логируем все события OrderPush для отладки
                        if data["type"] == "OrderPush":
                            logger.debug(f"Получено OrderPush: orderId={data['orderId']}, status={data['status']}, side={data['side']}, orderType={data['orderType']}")
                            if data["orderType"] == "MARKET" and data["side"] == "BUY" and data["status"] == "FILLED":
                                logger.info(f"Маркетная покупка: {data}")
                                await self.trading_bot.on_order_update(data)
                            elif data["orderType"] == "LIMIT" and data["side"] == "SELL" and data["status"] in ["NEW", "PARTIALLY_FILLED"]:
                                logger.info(f"Лимитный ордер на продажу выставлен: {data}")
                                await self.trading_bot.on_order_update(data)
                            elif data["orderType"] == "LIMIT" and data["side"] == "SELL" and data["status"] == "FILLED":
                                logger.info(f"Лимитная продажа исполнена: {data}")
                                await self.trading_bot.on_order_update(data)
                        elif data["type"] == "DealPush":
                            logger.info(f"Сделка: {data}")
                            if hasattr(self.trading_bot, 'on_deal_update'):
                                await self.trading_bot.on_deal_update(data)
                            else:
                                logger.warning(f"Метод on_deal_update отсутствует в TradingBot, пропуск DealPush")
                    except UnicodeDecodeError as e:
                        logger.error(f"Не удалось декодировать канал: {msg.data.hex()}, ошибка: {str(e)}")
                    except Exception as e:
                        logger.error(f"Ошибка обработки бинарного сообщения: {str(e)}")
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    logger.error("WebSocket закрыт")
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket ошибка")
                    break
        except asyncio.CancelledError:
            logger.info("Обработка сообщений WebSocket остановлена")
            raise
        except Exception as e:
            logger.error(f"Критическая ошибка в handle_messages: {str(e)}")
            raise

    async def process_notifications(self):
        """Обрабатывает очередь уведомлений."""
        while True:
            try:
                message = await self.notification_queue.get()
                await send_notification(self.trading_bot.telegram_app, message)
                self.notification_queue.task_done()
            except asyncio.CancelledError:
                logger.info("Обработка уведомлений остановлена")
                break
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {str(e)}")

    async def close(self):
        """Закрывает WebSocket-соединение и связанные ресурсы."""
        if self.notification_task:
            self.notification_task.cancel()
            try:
                await self.notification_task
            except asyncio.CancelledError:
                logger.info("Задача обработки уведомлений остановлена")
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        logger.info("WebSocket закрыт")