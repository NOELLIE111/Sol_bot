import asyncio
import os
import json
import random
import time
from enum import Enum
from loguru import logger
from config import settings, send_notification, save_state
from exchange import MEXCExchange
from order_manager import OrderManager
from datetime import datetime, timedelta
import glob

# Файлы состояния
TRADE_STATE_FILE = "trade_state.json"

class TradingState(Enum):
    IDLE = "idle"
    PROCESSING = "processing"
    AWAITING_NOTIFICATION = "awaiting_notification"

class TradingBot:
    def __init__(self, telegram_app):
        self.exchange = MEXCExchange(os.getenv("MEXC_API_KEY"), os.getenv("MEXC_SECRET_KEY"))
        self.telegram_app = telegram_app
        self.order_manager = OrderManager()
        self.last_action_price = None
        self.last_action_type = None
        self.buy_price = None
        self.sell_prices = {}
        self.order_id = None
        self.quantity = None
        self.position_active = False
        self.current_market_price = None
        self.last_buy_time = 0
        self.state = TradingState.IDLE
        self.processed_deal_ids = {}
        self._usdt_balance_cache = None
        self._balance_cache_time = 0
        self._balance_cache_ttl = 10
        self._max_balance_cache_ttl = 300
        self.low_balance_notified = False
        self.last_notified_balance = None
        self.last_notified_order_size = None
        self.low_balance_notified_auto = False
        self.last_notified_order_size_auto = None
        self.low_balance_limit_notified = False
        self.session_id = str(random.randint(10000000, 99999999))
        self.load_state()
        logger.info(f"Запуск бота, сессия: {self.session_id}")
        asyncio.create_task(self.cleanup_processed_deal_ids())

    async def _execute_buy_and_place_sell(self, current_price, order_size, trade_type: str):
        # Returns True if both buy and sell orders were successfully initiated, False otherwise.
        
        original_state_vars = {
            "buy_price": self.buy_price,
            "position_active": self.position_active,
            "order_id": self.order_id,
            "quantity": self.quantity,
            "sell_prices": self.sell_prices.copy(),
            "last_action_price_before_helper": self.last_action_price # For more precise revert
        }

        try:
            self.quantity = round(order_size / current_price, 2)
            logger.info(f"Helper: Рассчитанное количество: {self.quantity} SOL по цене {current_price} для '{trade_type}'")

            buy_client_order_id = f"BOT_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
            buy_order_id, _ = await self.exchange.place_order(
                side="BUY",
                quantity=self.quantity,
                order_type="MARKET",
                telegram_app=self.telegram_app,
                client_order_id=buy_client_order_id
            )

            if not buy_order_id:
                logger.error(f"Helper: Не удалось выполнить маркетный ордер на покупку для '{trade_type}'")
                # Notification is handled by place_order or calling method
                return False

            # Successfully placed buy order
            self.last_buy_time = time.time()
            self.buy_price = current_price # Or use actual execution price if available and important
            self.position_active = True
            # self.order_id = buy_order_id # Keep track of buy order ID for parent_order_id
            buy_order_system_id = buy_order_id # Store the system-generated ID

            self.update_trade_state("BUY", self.buy_price)
            
            buy_amount = round(self.quantity * self.buy_price, 4)
            orders = self.order_manager.load_orders(self.order_manager.order_file)
            orders.append({
                "order_id": buy_order_system_id,
                "client_order_id": buy_client_order_id,
                "side": "BUY",
                "type": "MARKET",
                "status": "completed", # Market orders are assumed completed quickly
                "quantity": str(self.quantity),
                "price": str(self.buy_price),
                "amount": str(buy_amount),
                "timestamp": int(time.time() * 1000),
                "profit": "0",
                "notified": False, # Notification will be handled by on_deal_update or on_order_update
                "parent_order_id": "",
                "trade_type": trade_type
            })
            self.order_manager.save_orders(self.order_manager.order_file, orders)
            logger.info(f"Helper: Ордер на покупку {buy_order_system_id} ({buy_client_order_id}) сохранен для '{trade_type}'.")

            # Check SOL balance (simulating what was there)
            # In a real scenario, you might want to confirm the asset is received via WebSocket or another API call
            # For now, we assume the buy was effective if place_order returned an ID.
            # A more robust check would involve querying balance after a short delay.
            # sol_balance = await self.exchange.get_balance("SOL")
            # if sol_balance < self.quantity:
            #     logger.error(f"Helper: Недостаточно SOL после покупки: {sol_balance} < {self.quantity} для '{trade_type}'")
            #     await send_notification(self.telegram_app, f"⚠️ Недостаточно SOL: {sol_balance} < {self.quantity}")
            #     self.position_active = False # Revert state
            #     self.order_id = original_state_vars["order_id"]
            #     self.buy_price = original_state_vars["buy_price"]
            #     # Consider how to handle the already executed buy order if SOL not confirmed.
            #     return False


            sell_price = round(self.buy_price * (1 + settings["profit_percent"] / 100), 2)
            sell_client_order_id = f"BOT_{int(time.time() * 1000)}_{random.randint(1000, 9999)}"
            
            sell_order_id, _ = await self.exchange.place_order(
                side="SELL",
                quantity=self.quantity,
                price=sell_price,
                order_type="LIMIT",
                telegram_app=self.telegram_app,
                client_order_id=sell_client_order_id
            )

            if not sell_order_id:
                logger.error(f"Helper: Не удалось выставить ордер на продажу после покупки {buy_order_system_id} для '{trade_type}'.")
                await send_notification(self.telegram_app, f"⚠️ Не удалось выставить ордер на продажу для '{trade_type}'")
                # Critical: What to do with the bought SOL?
                # For now, we leave position_active=True, buy_price set. User needs to be aware.
                # Reverting all state might be complex if buy was partially filled etc.
                # This highlights need for robust handling of such failures.
                self.order_id = buy_order_system_id # Point to the buy order as the last action
                return False # Indicate that the full sequence didn't complete

            # Successfully placed sell order
            self.order_id = sell_order_id # Now track the active sell order
            status, actual_sell_price_from_check = await self.exchange.check_order_status(sell_order_id)
            if status and actual_sell_price_from_check: # status might be NEW, price is from order details
                final_sell_price = round(actual_sell_price_from_check, 2)
            else: # Fallback if check_order_status fails or doesn't return price
                final_sell_price = sell_price
            self.sell_prices[sell_order_id] = final_sell_price
            
            orders = self.order_manager.load_orders(self.order_manager.order_file)
            orders.append({
                "order_id": sell_order_id,
                "client_order_id": sell_client_order_id,
                "side": "SELL",
                "type": "LIMIT",
                "status": "active",
                "quantity": str(self.quantity),
                "price": str(final_sell_price),
                "amount": "0",
                "timestamp": int(time.time() * 1000),
                "profit": "0",
                "notified": False,
                "parent_order_id": buy_order_system_id, # Link to the buy order
                "trade_type": trade_type
            })
            self.order_manager.save_orders(self.order_manager.order_file, orders)
            self.update_trade_state("SELL_ORDER_PLACED", final_sell_price) # A more descriptive state
            
            logger.info(
                f"Helper: Покупка {self.quantity} SOL по {self.buy_price:.2f}, "
                f"Ордер на продажу {sell_order_id} ({sell_client_order_id}) выставлен по {final_sell_price:.2f} для '{trade_type}'."
            )
            return True

        except Exception as e:
            logger.error(f"Helper: Ошибка в _execute_buy_and_place_sell для '{trade_type}': {str(e)}")
            # Revert state to before the call
            self.buy_price = original_state_vars["buy_price"]
            self.position_active = original_state_vars["position_active"]
            self.order_id = original_state_vars["order_id"]
            self.quantity = original_state_vars["quantity"]
            self.sell_prices = original_state_vars["sell_prices"]
            # Save reverted state if update_trade_state was called
            if self.last_action_price != original_state_vars.get("last_action_price_before_helper"): # Heuristic
                 self.update_trade_state(original_state_vars.get("last_action_type"), original_state_vars.get("last_action_price_before_helper")) # Revert precisely
            await send_notification(self.telegram_app, f"⚠️ Критическая ошибка при выполнении покупки/продажи ({trade_type}): {str(e)}")
            return False

    async def cleanup_processed_deal_ids(self):
        """Очищает устаревшие ID сделок."""
        while True:
            try:
                current_time = time.time()
                self.processed_deal_ids = {
                    trade_id: timestamp
                    for trade_id, timestamp in self.processed_deal_ids.items()
                    if current_time - timestamp < 86400
                }
                logger.debug(f"Очищено processed_deal_ids, текущий размер: {len(self.processed_deal_ids)}")
                await asyncio.sleep(3600)
            except Exception as e:
                logger.error(f"Ошибка очистки processed_deal_ids: {str(e)}")
                await asyncio.sleep(60)

    def load_state(self):
        """Загружает состояние торговли из trade_state.json."""
        if not os.path.exists(TRADE_STATE_FILE):
            logger.info("Файл trade_state.json не найден, создаётся новый")
            self.save_trade_state()
        else:
            try:
                with open(TRADE_STATE_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if not content:
                        logger.warning("Файл trade_state.json пуст, создаётся новый")
                        self.save_trade_state()
                        return
                    trade_state = json.loads(content)
                self.last_action_price = trade_state.get("last_action_price")
                self.last_action_type = trade_state.get("last_action_type")
                self.low_balance_notified = trade_state.get("low_balance_notified", False)
                self.last_notified_balance = trade_state.get("last_notified_balance")
                self.last_notified_order_size = trade_state.get("last_notified_order_size")
                self.low_balance_notified_auto = trade_state.get("low_balance_notified_auto", False)
                self.last_notified_order_size_auto = trade_state.get("last_notified_order_size_auto")
                self.low_balance_limit_notified = trade_state.get("low_balance_limit_notified", False)
                logger.info(
                    f"Состояние торговли загружено (сессия {self.session_id}): "
                    f"last_action_price={self.last_action_price}, "
                    f"last_action_type={self.last_action_type}, "
                    f"low_balance_notified={self.low_balance_notified}, "
                    f"low_balance_notified_auto={self.low_balance_notified_auto}, "
                    f"low_balance_limit_notified={self.low_balance_limit_notified}"
                )
            except json.JSONDecodeError as e:
                logger.error(f"Ошибка загрузки trade_state.json: Некорректный JSON ({str(e)})")
                self.save_trade_state()
            except Exception as e:
                logger.error(f"Ошибка загрузки trade_state.json: {str(e)}")
                self.save_trade_state()

        orders = self.order_manager.load_orders(self.order_manager.order_file)
        active_orders = [order for order in orders if order["status"] == "active"]
        logger.info(f"Найдено активных ордеров: {len(active_orders)}")
        for order in active_orders:
            if order["side"] == "SELL" and order.get("client_order_id", "").startswith("BOT_"):
                self.order_id = order["order_id"]
                self.position_active = True
                self.quantity = order["quantity"]
                self.buy_price = None
                self.sell_prices[order["order_id"]] = float(order["price"])
                logger.debug(f"Установлен текущий активный ордер: {order}, sell_price={self.sell_prices[order['order_id']]}")
                break

    async def sync_orders(self):
        """Синхронизирует ордера с биржей, удаляя несуществующие."""
        try:
            orders = self.order_manager.load_orders(self.order_manager.order_file)
            logger.info(f"Синхронизация ордеров: проверка {len(orders)} записей в order.json")
            
            open_orders = await self.exchange.get_open_orders()
            valid_order_ids = {order[0] for order in open_orders}
            updated_orders = []
            for order in orders:
                if order["status"] == "active" and order["side"] == "SELL" and order["order_id"] not in valid_order_ids:
                    logger.warning(f"Ордер {order['order_id']} не найден на бирже, помечаем как completed")
                    order["status"] = "completed"
                if not any(o["order_id"] == order["order_id"] for o in updated_orders):
                    updated_orders.append(order)
            
            self.order_manager.save_orders(self.order_manager.order_file, updated_orders)
            logger.info(f"Синхронизация завершена: сохранено {len(updated_orders)} ордеров")
        except Exception as e:
            logger.error(f"Ошибка синхронизации ордеров: {str(e)}")

    def save_trade_state(self):
        """Сохраняет состояние торговли в trade_state.json."""
        trade_state = {
            "last_action_price": self.last_action_price,
            "last_action_time": int(time.time() * 1000) if self.last_action_price else None,
            "last_action_type": self.last_action_type,
            "low_balance_notified": self.low_balance_notified,
            "last_notified_balance": self.last_notified_balance,
            "last_notified_order_size": self.last_notified_order_size,
            "low_balance_notified_auto": self.low_balance_notified_auto,
            "last_notified_order_size_auto": self.last_notified_order_size_auto,
            "low_balance_limit_notified": self.low_balance_limit_notified
        }
        try:
            with open(TRADE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(trade_state, f, indent=4, ensure_ascii=False)
            logger.info(f"Состояние торговли сохранено (сессия {self.session_id}): {trade_state}")
        except Exception as e:
            logger.error(f"Ошибка сохранения trade_state.json: {str(e)}")

    def update_trade_state(self, action_type, price):
        """Обновляет состояние торговли."""
        self.last_action_type = action_type
        self.last_action_price = price
        self.save_trade_state()

    async def get_usdt_balance(self):
        """Получает текущий баланс USDT, используя кэш."""
        current_time = time.time()
        if (self._usdt_balance_cache is not None and
                current_time - self._balance_cache_time < self._balance_cache_ttl):
            logger.debug(f"Использован кэшированный баланс USDT: {self._usdt_balance_cache:.4f}, TTL: {self._balance_cache_ttl} сек")
            self._balance_cache_ttl = min(self._balance_cache_ttl * 2, self._max_balance_cache_ttl)
            return self._usdt_balance_cache

        try:
            usdt_balance = await self.exchange.get_balance("USDT")
            if usdt_balance >= settings["order_size"]:
                if self.low_balance_notified or self.low_balance_notified_auto:
                    self.low_balance_notified = False
                    self.low_balance_notified_auto = False
                    self.last_notified_balance = None
                    self.last_notified_order_size = None
                    self.last_notified_order_size_auto = None
                    logger.info("Баланс USDT стал достаточным, сброшены флаги low_balance_notified и low_balance_notified_auto")
            self._usdt_balance_cache = usdt_balance
            self._balance_cache_time = current_time
            self._balance_cache_ttl = 10
            logger.debug(f"Обновлен баланс USDT: {usdt_balance:.4f}, новый TTL: {self._balance_cache_ttl} сек")
            return usdt_balance
        except Exception as e:
            logger.error(f"Ошибка получения баланса USDT: {str(e)}")
            return 0.0

    async def get_used_balance(self):
        """Возвращает сумму USDT, задействованную в активных ордерах на продажу."""
        try:
            orders = self.order_manager.load_orders(self.order_manager.order_file)
            used_balance = 0.0
            for order in orders:
                if (order["status"] == "active" and
                    order["side"] == "SELL" and
                    order.get("client_order_id", "").startswith("BOT_")):
                    parent_id = order.get("parent_order_id", "")
                    for buy_order in orders:
                        if buy_order["order_id"] == parent_id and buy_order["side"] == "BUY":
                            used_balance += float(buy_order["amount"])
                            break
            logger.debug(f"Задействовано {used_balance:.4f} USDT в активных ордерах")
            return used_balance
        except Exception as e:
            logger.error(f"Ошибка подсчёта задействованного баланса: {str(e)}")
            return 0.0

    def reset_balance_cache(self):
        """Сбрасывает кэш баланса USDT."""
        self._usdt_balance_cache = None
        self._balance_cache_time = 0
        self._balance_cache_ttl = 10
        logger.debug("Кэш баланса USDT сброшен")

    async def get_price_info(self):
        """Возвращает текущую рыночную цену и следующую цену покупки."""
        current_price = self.current_market_price
        next_buy_price = None
        if self.last_action_price is not None:
            next_buy_price = round(self.last_action_price * (1 - settings["drop_percent"] / 100), 2)
        return current_price, next_buy_price

    async def on_order_update(self, order_data):
        """Обрабатывает обновления статуса ордеров."""
        if order_data["symbol"] != "SOLUSDT":
            logger.debug(f"Игнорируем ордер {order_data['orderId']}, symbol={order_data['symbol']} не SOLUSDT")
            return
        if not order_data.get("clientOrderId", "").startswith("BOT_"):
            logger.debug(f"Игнорируем ордер {order_data['orderId']}, clientOrderId={order_data.get('clientOrderId')} не начинается с BOT_")
            return

        order_id = order_data["orderId"]
        status = order_data["status"]
        side = order_data["side"]
        order_type = order_data["orderType"]
        price = float(order_data["price"]) if order_data["price"] else None
        quantity = float(order_data["quantity"]) if order_data["quantity"] else None
        avg_price = float(order_data["avgPrice"]) if order_data["avgPrice"] else None
        cum_qty = float(order_data["cumQty"]) if order_data["cumQty"] else None
        cum_amt = float(order_data["cumAmt"]) if order_data["cumAmt"] else None
        created_time = order_data["createdTime"]
        client_order_id = order_data.get("clientOrderId", "")

        execution_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "FILLED" else datetime.fromtimestamp(created_time).strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"Обработка OrderPush (сессия {self.session_id}): orderId={order_id}, status={status}, side={side}, orderType={order_type}")

        orders = self.order_manager.load_orders(self.order_manager.order_file)
        order_exists = any(o["order_id"] == order_id for o in orders)
        current_order = next((o for o in orders if o["order_id"] == order_id), None)

        trade_type = current_order.get("trade_type", "auto") if current_order else "auto"

        if not order_exists:
            amount = str(cum_amt) if cum_amt and status == "FILLED" else "0"
            order_price = str(round(avg_price or price, 2)) if avg_price or price else "0"
            orders.append({
                "order_id": order_id,
                "client_order_id": client_order_id,
                "side": side,
                "type": order_type,
                "status": "active" if status in ["NEW", "PARTIALLY_FILLED"] else "completed",
                "quantity": str(quantity),
                "price": order_price,
                "amount": amount,
                "timestamp": int(created_time * 1000),
                "profit": "0",
                "notified": False,
                "parent_order_id": "",
                "trade_type": trade_type
            })
            logger.info(f"Добавлен новый ордер: {order_id}, status={status}, amount={amount}, trade_type={trade_type}")

        self.state = TradingState.AWAITING_NOTIFICATION
        if order_type == "MARKET" and side == "BUY" and status == "FILLED":
            for order in orders:
                if order["order_id"] == order_id and not order.get("notified", False):
                    sell_price = self.sell_prices.get(self.order_id, round((avg_price or price) * (1 + settings["profit_percent"] / 100), 2))
                    await send_notification(
                        application=self.telegram_app,
                        message=(
                            f"🟢 Сделка подтверждена ({'Ручная покупка' if order['trade_type'] == 'manual' else 'Покупка (Autobuy)'})!\n"
                            f"🕒 Время: {execution_time}\n"
                            f"📈 Покупка: {quantity} SOL по {(avg_price or price):.2f} USDT\n"
                            f"💰 Продажа: {quantity} SOL по {sell_price:.2f} USDT\n"
                            f"💸 Сумма: {cum_amt:.4f} USDT"
                        )
                    )
                    logger.info(f"Отправлено уведомление о покупке для ордера {order_id}, trade_type={order['trade_type']}")
                    order["notified"] = True
                    order["price"] = str(round(avg_price or price, 2))
                    order["amount"] = str(cum_amt)
                    break
        elif order_type == "LIMIT" and side == "SELL" and status in ["NEW", "PARTIALLY_FILLED"]:
            logger.info(
                f"Лимитный ордер на продажу выставлен!\n"
                f"Время: {execution_time}\n"
                f"Количество: {quantity} SOL\n"
                f"Цена: {price:.2f} USDT\n"
                f"Статус: {'Частично исполнен' if status == 'PARTIALLY_FILLED' else 'Новый'}"
            )
        elif order_type == "LIMIT" and side == "SELL" and status == "FILLED":
            logger.info(
                f"Лимитная продажа исполнена!\n"
                f"Время: {execution_time}\n"
                f"Количество: {cum_qty} SOL\n"
                f"Цена: {avg_price or price:.2f} USDT\n"
                f"Сумма: {cum_amt:.4f} USDT"
            )
            buy_amount = None
            buy_price = None
            for order in orders:
                if order["order_id"] == order_id:
                    parent_order_id = order.get("parent_order_id", "")
                    for buy_order in orders:
                        if buy_order["order_id"] == parent_order_id and buy_order["side"] == "BUY" and buy_order["status"] == "completed":
                            buy_amount = float(buy_order["amount"])
                            buy_price = float(buy_order["price"])
                            break
                    trade_type = order.get("trade_type", "auto")
                    break
            if buy_amount and cum_amt and buy_price:
                taker_fee = buy_amount * (settings["taker_fee_percent"] / 100)
                maker_fee = cum_amt * (settings["maker_fee_percent"] / 100)
                profit = round(cum_amt - (buy_amount + taker_fee + maker_fee), 4)
                logger.debug(f"Расчет прибыли для ордера {order_id}: sell={cum_amt:.4f}, buy={buy_amount:.4f}, taker_fee={taker_fee:.4f}, maker_fee={maker_fee:.4f}, profit={profit:.4f}")
                for order in orders:
                    if order["order_id"] == order_id and not order.get("notified", False):
                        await send_notification(
                            application=self.telegram_app,
                            message=(
                                f"🔴 Сделка завершена ({'Продажа (Buy)' if trade_type == 'manual' else 'Продажа (Autobuy)'})!\n"
                                f"🕒 Время: {execution_time}\n"
                                f"📈 Покупка: {cum_qty} SOL по {buy_price:.2f} USDT\n"
                                f"💰 Продажа: {cum_qty} SOL по {(avg_price or price):.2f} USDT\n"
                                f"💳 Комиссия тейкера: {taker_fee:.4f} USDT\n"
                                f"💳 Комиссия мейкера: {maker_fee:.4f} USDT\n"
                                f"💸 Прибыль: {profit:.4f} USDT"
                            )
                        )
                        logger.info(f"Отправлено уведомление о продаже для ордера {order_id}: Прибыль {profit:.4f} USDT, trade_type={trade_type}")
                        order["notified"] = True
                        break
                for order in orders:
                    if order["order_id"] == order_id:
                        order["amount"] = str(cum_amt)
                        order["profit"] = str(profit)
                        order["price"] = str(round(avg_price or price, 2))
                        order["timestamp"] = int(time.time() * 1000)
                        break
                try:
                    with open("state.json", "r", encoding="utf-8") as f:
                        state = json.load(f)
                    state["settings"]["total_profit"] = str(float(state["settings"].get("total_profit", "0")) + profit)
                    with open("state.json", "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=4, ensure_ascii=False)
                    logger.info(f"Обновлена общая прибыль: {state['settings']['total_profit']} USDT")
                except Exception as e:
                    logger.error(f"Ошибка обновления total_profit: {str(e)}")
                if order_id in self.sell_prices:
                    del self.sell_prices[order_id]
            else:
                logger.warning(f"Не найдена покупка для ордера {order_id} (parent_order_id={parent_order_id}), прибыль не рассчитана")

        for order in orders:
            if order["order_id"] == order_id:
                if status in ["NEW", "PARTIALLY_FILLED"]:
                    order["status"] = "active"
                elif status == "FILLED":
                    order["status"] = "completed"
                    if side == "BUY":
                        self.last_action_price = avg_price or price
                        self.position_active = True
                        self.buy_price = avg_price or price
                        self.quantity = cum_qty
                        self.order_id = order_id
                        self.update_trade_state("BUY", avg_price or price)
                    elif side == "SELL":
                        self.last_action_price = avg_price or price
                        self.position_active = False
                        self.order_id = None
                        self.buy_price = None
                        self.quantity = None
                        self.low_balance_limit_notified = False
                        self.update_trade_state("SELL", avg_price or price)
                elif status in ["CANCELED", "REJECTED"]:
                    order["status"] = "completed"
                    if order_id == self.order_id:
                        self.position_active = False
                        self.order_id = None
                        if order_id in self.sell_prices:
                            del self.sell_prices[order_id]
                        message = (
                            f"⚠️ Ордер {order_id} {'отменён' if status == 'CANCELED' else 'отклонён'}!\n"
                            f"🕒 Время: {execution_time}"
                        )
                        await send_notification(self.telegram_app, message)
                break

        self.order_manager.save_orders(self.order_manager.order_file, orders)
        self.state = TradingState.IDLE
        logger.debug(f"Состояние изменено на {self.state}")

    async def start_trading(self):
        """Запускает автоматическую торговлю."""
        if not settings["autobuy_enabled"]:
            logger.warning("Торговля не запущена: autobuy_enabled=False")
            return

        if self.state != TradingState.IDLE:
            logger.warning(f"Торговля не запущена: текущее состояние {self.state}")
            return

        current_time = time.time()
        if current_time - self.last_buy_time < 2:
            logger.warning("Слишком частая команда /autobuy, пропуск")
            await send_notification(self.telegram_app, "⚠️ Слишком частая команда /autobuy, подождите 2 секунды")
            return

        self.state = TradingState.PROCESSING
        logger.debug(f"Состояние изменено на {self.state}")

        try:
            await self.sync_orders()

            usdt_balance = await self.get_usdt_balance()
            order_size = settings["order_size"]
            if usdt_balance < order_size:
                logger.error(f"Недостаточно USDT: {usdt_balance} < {order_size}")
                if not self.low_balance_notified:
                    await send_notification(self.telegram_app, f"⚠️ Недостаточно USDT: {usdt_balance:.4f} < {order_size}")
                    self.low_balance_notified = True
                    self.last_notified_balance = usdt_balance
                    self.save_trade_state()
                self.state = TradingState.IDLE
                return

            fixed_balance_limit = settings.get("fixed_balance_limit")
            if fixed_balance_limit is not None:
                used_balance = await self.get_used_balance()
                available_balance = fixed_balance_limit - used_balance
                if available_balance < order_size:
                    logger.warning(
                        f"Покупка не выполнена: доступный лимит {available_balance:.4f} USDT < размер ордера {order_size} USDT "
                        f"(задействовано {used_balance:.4f}/{fixed_balance_limit} USDT)"
                    )
                    if not self.low_balance_limit_notified:
                        await send_notification(
                            self.telegram_app,
                            f"⚠️ Покупка (Autobuy) не выполнена!\n"
                            f"💵 Лимит баланса: {fixed_balance_limit:.2f} USDT\n"
                            f"💸 Задействовано: {used_balance:.4f} USDT\n"
                            f"📊 Доступно: {available_balance:.4f} USDT\n"
                            f"❌ Причина: Недостаточно средств в лимите для ордера {order_size} USDT"
                        )
                        self.low_balance_limit_notified = True
                        self.save_trade_state()
                    self.state = TradingState.IDLE
                    return
                self.low_balance_limit_notified = False
                self.save_trade_state()

            market_price = await self.exchange.get_market_price()
            if not market_price:
                logger.error("Не удалось получить рыночную цену")
                self.state = TradingState.IDLE
                return

            drop_trigger = None
            if self.last_action_price is not None:
                drop_trigger = round(self.last_action_price * (1 - settings["drop_percent"] / 100), 4)
                logger.debug(f"Проверка цены: текущая={market_price}, цель={drop_trigger}")

            orders = self.order_manager.load_orders(self.order_manager.order_file)
            active_sell_orders = [order for order in orders if order["status"] == "active" and order["side"] == "SELL" and order.get("client_order_id", "").startswith("BOT_")]
            if active_sell_orders and (drop_trigger is None or market_price > drop_trigger):
                await self.telegram_app.bot.send_message(
                    chat_id=(await self.telegram_app.bot.get_updates())[0].message.chat.id,
                    text=f"Торговля возобновлена, но покупка не выполнена: цена {market_price:.2f} USDT выше цели {drop_trigger:.2f} USDT. Активных ордеров: {len(active_sell_orders)}."
                )
                logger.info(f"Торговля возобновлена, но покупка пропущена: цена {market_price} > drop_trigger {drop_trigger}, активных ордеров: {len(active_sell_orders)}")
                settings["autobuy_enabled"] = True
                save_state()
                self.state = TradingState.IDLE
                return

            # Refactored block using the helper method
            success = await self._execute_buy_and_place_sell(market_price, order_size, "auto")
            if success:
                logger.info("start_trading: _execute_buy_and_place_sell успешно завершен.")
                self.state = TradingState.AWAITING_NOTIFICATION
            else:
                logger.error("start_trading: _execute_buy_and_place_sell не удался.")
                # Notifications are handled by the helper or place_order
                self.state = TradingState.IDLE
                # Ensure state is reverted if helper failed mid-way by helper's own try/except
            # End of refactored block

        except Exception as e:
            logger.error(f"Ошибка в start_trading: {str(e)}")
            # Ensure critical errors in start_trading itself (outside helper) also send notification
            await send_notification(self.telegram_app, f"⚠️ Критическая ошибка автоторговли: {str(e)}")
        finally:
            if self.state == TradingState.PROCESSING:
                self.state = TradingState.IDLE
            logger.debug(f"Состояние изменено на {self.state}")

    async def manual_buy(self):
        """Совершает ручную покупку."""
        if self.state != TradingState.IDLE:
            logger.warning(f"Ручная покупка не выполнена: текущее состояние {self.state}")
            return False

        current_time = time.time()
        if current_time - self.last_buy_time < 2:
            logger.warning("Слишком частые команды /buy, пропуск")
            await send_notification(self.telegram_app, "⚠️ Слишком частые команды /buy, подождите 2 секунды")
            return False

        self.state = TradingState.PROCESSING
        logger.debug(f"Состояние изменено на {self.state}")

        try:
            usdt_balance = await self.get_usdt_balance()
            order_size = settings["order_size"]
            if usdt_balance < order_size:
                if not self.low_balance_notified or self.last_notified_order_size != order_size:
                    logger.error(f"Недостаточно USDT: {usdt_balance} < {order_size}")
                    self.low_balance_notified = True
                    self.last_notified_balance = usdt_balance
                    self.last_notified_order_size = order_size
                    self.save_trade_state()
                await send_notification(self.telegram_app, f"⚠️ Недостаточно USDT: {usdt_balance:.4f} < {order_size}")
                self.state = TradingState.IDLE
                return False

            fixed_balance_limit = settings.get("fixed_balance_limit")
            if fixed_balance_limit is not None:
                used_balance = await self.get_used_balance()
                available_balance = fixed_balance_limit - used_balance
                if available_balance < order_size:
                    logger.warning(
                        f"Ручная покупка не выполнена: доступный лимит {available_balance:.4f} USDT < размер ордера {order_size} USDT "
                        f"(задействовано {used_balance:.4f}/{fixed_balance_limit} USDT)"
                    )
                    await send_notification(
                        self.telegram_app,
                        f"⚠️ Ручная покупка не выполнена!\n"
                        f"💵 Лимит баланса: {fixed_balance_limit:.2f} USDT\n"
                        f"💸 Задействовано: {used_balance:.4f} USDT\n"
                        f"📊 Доступно: {available_balance:.4f} USDT\n"
                        f"❌ Причина: Недостаточно средств в лимите для ордера {order_size} USDT"
                    )
                    self.state = TradingState.IDLE
                    return False

            market_price = await self.exchange.get_market_price()
            if not market_price:
                logger.error("Не удалось получить рыночную цену")
                await send_notification(self.telegram_app, f"⚠️ Не удалось получить рыночную цену")
                self.state = TradingState.IDLE
                return False

            # Refactored block using the helper method
            success = await self._execute_buy_and_place_sell(market_price, order_size, "manual")
            if success:
                logger.info("manual_buy: _execute_buy_and_place_sell успешно завершен.")
                # The original manual_buy returned True on success, which was then used by the command handler
                # to send a "Ручная покупка выполнена!" message. We'll keep that logic in the command handler.
                self.state = TradingState.AWAITING_NOTIFICATION # Or IDLE if notifications are fully handled by websockets
                return True # Indicate success to the caller
            else:
                logger.error("manual_buy: _execute_buy_and_place_sell не удался.")
                # Notifications for failure are handled by the helper or place_order
                self.state = TradingState.IDLE
                return False # Indicate failure to the caller
            # End of refactored block

        except Exception as e:
            logger.error(f"Ошибка в manual_buy: {str(e)}")
            # Ensure critical errors in manual_buy itself (outside helper) also send notification
            await send_notification(self.telegram_app, f"⚠️ Критическая ошибка ручной покупки: {str(e)}")
            return False # Indicate failure
        finally:
            if self.state == TradingState.PROCESSING: # Ensure state is reset if it was PROCESSING
                self.state = TradingState.IDLE
            logger.debug(f"Состояние изменено на {self.state}")

    async def calculate_profit(self, period="day", date=None):
        """Рассчитывает количество сделок и прибыль за указанный период."""
        try:
            month_names = [
                "january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"
            ]

            orders = self.order_manager.load_orders(self.order_manager.order_file)
            all_orders = orders.copy()

            now = datetime.now()
            if period == "day":
                if date is None:
                    date = now
                start_time = date.replace(hour=0, minute=0, second=0, microsecond=0)
                end_time = date.replace(hour=23, minute=59, second=59, microsecond=999999)
                archive_file = f"order_archive_{month_names[date.month - 1]}_{date.year}.json"
                if os.path.exists(archive_file):
                    try:
                        with open(archive_file, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                            if content:
                                archive_orders = json.loads(content)
                                all_orders.extend(archive_orders)
                    except json.JSONDecodeError as e:
                        logger.error(f"Ошибка чтения {archive_file}: {str(e)}")
                    except Exception as e:
                        logger.error(f"Ошибка загрузки {archive_file}: {str(e)}")
            elif period == "month":
                if date is None:
                    date = now
                start_time = date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                end_time = (date.replace(day=1, month=date.month % 12 + 1) if date.month < 12 else date.replace(day=1, month=1, year=date.year + 1)) - timedelta(microseconds=1)
                archive_file = f"order_archive_{month_names[date.month - 1]}_{date.year}.json"
                if os.path.exists(archive_file):
                    try:
                        with open(archive_file, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                            if content:
                                archive_orders = json.loads(content)
                                all_orders.extend(archive_orders)
                    except json.JSONDecodeError as e:
                        logger.error(f"Ошибка чтения {archive_file}: {str(e)}")
                    except Exception as e:
                        logger.error(f"Ошибка загрузки {archive_file}: {str(e)}")
                if date.year == now.year and date.month == now.month:
                    all_orders.extend(orders)
            elif period == "all":
                start_time = datetime.fromtimestamp(0)
                end_time = datetime.max
                archive_files = glob.glob("order_archive_*.json")
                for archive_file in archive_files:
                    try:
                        with open(archive_file, "r", encoding="utf-8") as f:
                            content = f.read().strip()
                            if content:
                                archive_orders = json.loads(content)
                                all_orders.extend(archive_orders)
                    except json.JSONDecodeError as e:
                        logger.error(f"Ошибка чтения {archive_file}: {str(e)}")
                    except Exception as e:
                        logger.error(f"Ошибка загрузки {archive_file}: {str(e)}")
                all_orders.extend(orders)

            total_profit = 0.0
            total_trades = 0

            for order in all_orders:
                if (order["side"] == "SELL" and 
                    order["status"] == "completed" and 
                    "profit" in order and 
                    order.get("client_order_id", "").startswith("BOT_")):
                    order_time = datetime.fromtimestamp(order["timestamp"] / 1000)
                    if start_time <= order_time <= end_time:
                        try:
                            profit = float(order["profit"])
                            total_profit += profit
                            total_trades += 1
                        except (ValueError, TypeError) as e:
                            logger.warning(f"Некорректное значение profit в ордере {order['order_id']}: {str(e)}")

            logger.debug(f"Статистика за {period}: trades={total_trades}, profit={total_profit:.4f} USDT")
            return total_trades, total_profit
        except Exception as e:
            logger.error(f"Ошибка подсчёта прибыли: {str(e)}")
            return 0, 0.0

    async def on_price_update(self, price):
        """Обрабатывает обновления рыночной цены."""
        if not settings["autobuy_enabled"]:
            logger.debug("Автоторговля отключена, пропуск обновления цены")
            return None

        self.current_market_price = price
        drop_trigger = None
        if self.last_action_price is not None:
            drop_trigger = round(self.last_action_price * (1 - settings["drop_percent"] / 100), 4)
            logger.debug(f"Цена покупки: текущая={price}, цель={drop_trigger}, last_buy_time={self.last_buy_time}")

            if price > drop_trigger and self.low_balance_notified_auto:
                self.low_balance_notified_auto = False
                self.last_notified_order_size_auto = None
                self.save_trade_state()
                logger.debug("Цена выше триггера, сброшен флаг low_balance_notified_auto")

            if self.state != TradingState.IDLE:
                logger.debug(f"Покупка заблокирована: текущее состояние {self.state}")
                return drop_trigger

            current_time = time.time()
            if current_time - self.last_buy_time < 2:
                logger.debug(f"Слишком частые покупки, пропуск (время с последней покупки: {current_time - self.last_buy_time:.2f} сек)")
                return drop_trigger

            if price <= drop_trigger:
                self.state = TradingState.PROCESSING
                logger.debug(f"Состояние изменено на {self.state}")

                try:
                    usdt_balance = await self.get_usdt_balance()
                    order_size = settings["order_size"]
                    if usdt_balance < order_size:
                        if not self.low_balance_notified_auto or self.last_notified_order_size_auto != order_size:
                            logger.error(f"Недостаточно USDT: {usdt_balance} < {order_size}")
                            await send_notification(
                                self.telegram_app,
                                f"⚠️ Покупка (Autobuy) не выполнена!\n"
                                f"📈 Текущая цена: {price:.2f} USDT\n"
                                f"🎯 Триггерная цена: {drop_trigger:.2f} USDT\n"
                                f"💸 Размер ордера: {order_size:.2f} USDT\n"
                                f"💰 Баланс: {usdt_balance:.4f} USDT\n"
                                f"❌ Причина: Недостаточно USDT"
                            )
                            self.low_balance_notified_auto = True
                            self.last_notified_balance = usdt_balance
                            self.last_notified_order_size_auto = order_size
                            self.save_trade_state()
                        self.state = TradingState.IDLE
                        return drop_trigger

                    fixed_balance_limit = settings.get("fixed_balance_limit")
                    if fixed_balance_limit is not None:
                        used_balance = await self.get_used_balance()
                        available_balance = fixed_balance_limit - used_balance
                        if available_balance < order_size:
                            current_conditions = (used_balance, fixed_balance_limit, order_size)
                            if not hasattr(self, "last_notified_limit_conditions") or self.last_notified_limit_conditions != current_conditions:
                                logger.warning(
                                    f"Покупка не выполнена: доступный лимит {available_balance:.4f} USDT < размер ордера {order_size} USDT "
                                    f"(задействовано {used_balance:.4f}/{fixed_balance_limit} USDT)"
                                )
                                self.last_notified_limit_conditions = current_conditions
                            if not self.low_balance_limit_notified:
                                await send_notification(
                                    self.telegram_app,
                                    f"⚠️ Покупка (Autobuy) не выполнена!\n"
                                    f"📈 Текущая цена: {price:.2f} USDT\n"
                                    f"🎯 Триггерная цена: {drop_trigger:.2f} USDT\n"
                                    f"💵 Лимит баланса: {fixed_balance_limit:.2f} USDT\n"
                                    f"💸 Задействовано: {used_balance:.4f} USDT\n"
                                    f"📊 Доступно: {available_balance:.4f} USDT\n"
                                    f"❌ Причина: Недостаточно средств в лимите для ордера {order_size} USDT"
                                )
                                self.low_balance_limit_notified = True
                                self.save_trade_state()
                            self.state = TradingState.IDLE
                            return drop_trigger
                        self.low_balance_limit_notified = False
                        self.last_notified_limit_conditions = None
                        self.save_trade_state()
                    
                    # Refactored block using the helper method
                    success = await self._execute_buy_and_place_sell(price, order_size, "auto")
                    if success:
                        logger.info("on_price_update: _execute_buy_and_place_sell успешно завершен.")
                        self.state = TradingState.AWAITING_NOTIFICATION
                    else:
                        logger.error("on_price_update: _execute_buy_and_place_sell не удался.")
                        self.state = TradingState.IDLE # Reset state if helper failed
                    # End of refactored block
                except Exception as e:
                    logger.error(f"Ошибка в on_price_update (внешний try): {str(e)}")
                    await send_notification(self.telegram_app, f"⚠️ Критическая ошибка обработки цены: {str(e)}")
                finally:
                    if self.state == TradingState.PROCESSING:
                        self.state = TradingState.IDLE
                    logger.debug(f"Состояние изменено на {self.state}")
        return drop_trigger

    async def on_deal_update(self, deal_data):
        """Обрабатывает обновления сделок."""
        if deal_data["symbol"] != "SOLUSDT":
            logger.debug(f"Игнорируем сделку {deal_data['orderId']}, symbol={deal_data['symbol']} не SOLUSDT")
            return
        if not deal_data.get("clientOrderId", "").startswith("BOT_"):
            logger.debug(f"Игнорируем сделку {deal_data['orderId']}, clientOrderId={deal_data.get('clientOrderId')} не начинается с BOT_")
            return

        order_id = deal_data["orderId"]
        side = deal_data["side"]
        trade_id = deal_data["tradeId"]

        if trade_id in self.processed_deal_ids:
            logger.debug(f"Сделка {trade_id} для ордера {order_id} уже обработана, пропуск")
            return
        self.processed_deal_ids[trade_id] = time.time()

        getter = lambda key: float(deal_data[key]) if deal_data[key] else None
        price = getter("price")
        quantity = getter("quantity")
        amount = getter("amount")
        trade_time = deal_data["tradeTime"]

        execution_time = datetime.fromtimestamp(trade_time).strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"Обработка DealPush (сессия {self.session_id}): orderId={order_id}, side={side}, price={price}, quantity={quantity}, amount={amount}")

        self.reset_balance_cache()

        orders = self.order_manager.load_orders(self.order_manager.order_file)
        current_order = next((o for o in orders if o["order_id"] == order_id), None)
        trade_type = current_order.get("trade_type", "auto") if current_order else "auto"

        self.state = TradingState.AWAITING_NOTIFICATION
        for order in orders:
            if order["order_id"] == order_id and not order.get("notified", False):
                if side == "BUY" and order["type"] == "MARKET":
                    sell_price = self.sell_prices.get(self.order_id, round(price * (1 + settings["profit_percent"] / 100), 2))
                    await send_notification(
                        application=self.telegram_app,
                        message=(
                            f"🟢 Сделка подтверждена ({'Ручная покупка' if order['trade_type'] == 'manual' else 'Покупка (Autobuy)'})!\n"
                            f"🕒 Время: {execution_time}\n"
                            f"📈 Покупка: {quantity} SOL по {price:.2f} USDT\n"
                            f"💰 Продажа: {quantity} SOL по {sell_price:.2f} USDT\n"
                            f"💸 Сумма: {amount:.4f} USDT"
                        )
                    )
                    logger.info(f"Отправлено уведомление о покупке для ордера {order_id}, trade_type={order['trade_type']}")
                    order["notified"] = True
                    order["price"] = str(round(price, 2))
                    order["amount"] = str(amount)
                    self.update_trade_state("BUY", price)
                    self.order_manager.save_orders(self.order_manager.order_file, orders)
                elif side == "SELL" and order["type"] == "LIMIT":
                    buy_amount = None
                    buy_price = None
                    parent_order_id = order.get("parent_order_id", "")
                    for buy_order in orders:
                        if buy_order["order_id"] == parent_order_id and buy_order["side"] == "BUY" and buy_order["status"] == "completed":
                            buy_amount = float(buy_order["amount"])
                            buy_price = float(buy_order["price"])
                            break
                    if buy_amount and amount and buy_price:
                        taker_fee = buy_amount * (settings["taker_fee_percent"] / 100)
                        maker_fee = amount * (settings["maker_fee_percent"] / 100)
                        profit = round(amount - (buy_amount + taker_fee + maker_fee), 4)
                        logger.debug(f"Расчет прибыли для ордера {order_id}: sell={amount:.4f}, buy={buy_amount:.4f}, taker_fee={taker_fee:.4f}, maker_fee={maker_fee:.4f}, profit={profit:.4f}")
                        await send_notification(
                            application=self.telegram_app,
                            message=(
                                f"🔴 Сделка завершена ({'Продажа (Buy)' if order['trade_type'] == 'manual' else 'Продажа (Autobuy)'})!\n"
                                f"🕒 Время: {execution_time}\n"
                                f"📈 Покупка: {quantity} SOL по {buy_price:.2f} USDT\n"
                                f"💰 Продажа: {quantity} SOL по {price:.2f} USDT\n"
                                f"💳 Комиссия тейкера: {taker_fee:.4f} USDT\n"
                                f"💳 Комиссия мейкера: {maker_fee:.4f} USDT\n"
                                f"💸 Прибыль: {profit:.4f} USDT"
                            )
                        )
                        logger.info(f"Отправлено уведомление о продаже для ордера {order_id}: Прибыль {profit:.4f} USDT, trade_type={order['trade_type']}")
                        order["notified"] = True
                        order["status"] = "completed"
                        order["amount"] = str(amount)
                        order["profit"] = str(profit)
                        order["price"] = str(round(price, 2))
                        order["timestamp"] = int(time.time() * 1000)
                        try:
                            with open("state.json", "r", encoding="utf-8") as f:
                                state = json.load(f)
                            state["settings"]["total_profit"] = str(float(state["settings"].get("total_profit", "0")) + profit)
                            with open("state.json", "w", encoding="utf-8") as f:
                                json.dump(state, f, indent=4, ensure_ascii=False)
                            logger.info(f"Обновлена общая прибыль: {state['settings']['total_profit']} USDT")
                        except Exception as e:
                            logger.error(f"Ошибка обновления total_profit: {str(e)}")
                        if order_id in self.sell_prices:
                            del self.sell_prices[order_id]
                        self.position_active = False
                        self.order_id = None
                        self.buy_price = None
                        self.quantity = None
                        self.low_balance_limit_notified = False
                        self.update_trade_state("SELL", price)
                        self.order_manager.save_orders(self.order_manager.order_file, orders)

                        active_sell_orders = [o for o in orders if o["status"] == "active" and o["side"] == "SELL" and o.get("client_order_id", "").startswith("BOT_")]
                        if not active_sell_orders and settings["autobuy_enabled"]:
                            logger.info(f"Исполнен последний ордер на продажу {order_id}, инициируем новую покупку")
                            current_time = time.time()
                            if current_time - self.last_buy_time < 2:
                                logger.warning(f"Слишком частая покупка после продажи, пропуск (время с последней покупки: {current_time - self.last_buy_time:.2f} сек)")
                                self.state = TradingState.IDLE
                                return

                            self.state = TradingState.PROCESSING
                            try:
                                usdt_balance = await self.get_usdt_balance()
                                order_size = settings["order_size"]
                                if usdt_balance < order_size:
                                    logger.error(f"Недостаточно USDT для покупки после продажи: {usdt_balance} < {order_size}")
                                    if not self.low_balance_notified:
                                        await send_notification(self.telegram_app, f"⚠️ Недостаточно USDT: {usdt_balance:.4f} < {order_size}")
                                        self.low_balance_notified = True
                                        self.last_notified_balance = usdt_balance
                                        self.save_trade_state()
                                    self.state = TradingState.IDLE
                                    return

                                fixed_balance_limit = settings.get("fixed_balance_limit")
                                if fixed_balance_limit is not None:
                                    used_balance = await self.get_used_balance()
                                    available_balance = fixed_balance_limit - used_balance
                                    if available_balance < order_size:
                                        logger.warning(
                                            f"Покупка после продажи не выполнена: доступный лимит {available_balance:.4f} USDT < размер ордера {order_size} USDT "
                                            f"(задействовано {used_balance:.4f}/{fixed_balance_limit} USDT)"
                                        )
                                        if not self.low_balance_limit_notified:
                                            await send_notification(
                                                self.telegram_app,
                                                f"⚠️ Покупка (Autobuy) после продажи не выполнена!\n"
                                                f"💵 Лимит баланса: {fixed_balance_limit:.2f} USDT\n"
                                                f"💸 Задействовано: {used_balance:.4f} USDT\n"
                                                f"📊 Доступно: {available_balance:.4f} USDT\n"
                                                f"❌ Причина: Недостаточно средств в лимите для ордера {order_size} USDT"
                                            )
                                            self.low_balance_limit_notified = True
                                            self.save_trade_state()
                                        self.state = TradingState.IDLE
                                        return
                                    self.low_balance_limit_notified = False
                                    self.save_trade_state()

                                market_price = await self.exchange.get_market_price()
                                if not market_price:
                                    logger.error("Не удалось получить рыночную цену для покупки после продажи")
                                    self.state = TradingState.IDLE
                                    return

                                # Refactored block using the helper method for autobuy after sell
                                success = await self._execute_buy_and_place_sell(market_price, order_size, "auto")
                                if success:
                                    logger.info("on_deal_update (autobuy after sell): _execute_buy_and_place_sell успешно завершен.")
                                    self.state = TradingState.AWAITING_NOTIFICATION
                                else:
                                    logger.error("on_deal_update (autobuy after sell): _execute_buy_and_place_sell не удался.")
                                    self.state = TradingState.IDLE # Reset state if helper failed
                                # End of refactored block
                            except Exception as e:
                                logger.error(f"Ошибка покупки после продажи (внешний try): {str(e)}")
                                await send_notification(self.telegram_app, f"⚠️ Критическая ошибка покупки после продажи: {str(e)}")
                            finally:
                                # Ensure state is AWAITING_NOTIFICATION if processing was successful before helper,
                                # or IDLE if helper failed or outer try failed.
                                if self.state == TradingState.PROCESSING: # If helper wasn't called or failed early
                                    self.state = TradingState.IDLE
                                elif self.state != TradingState.AWAITING_NOTIFICATION: # If helper set to IDLE or other
                                    self.state = TradingState.IDLE


                    else:
                        logger.warning(f"Не найдена покупка для ордера {order_id} (parent_order_id={parent_order_id}), прибыль не рассчитана")
                        self.order_manager.save_orders(self.order_manager.order_file, orders)
                    break
        self.state = TradingState.IDLE
        logger.debug(f"Состояние изменено на {self.state}")
