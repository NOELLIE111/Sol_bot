import os
import json
import asyncio
from datetime import datetime
from loguru import logger
from config import (
    ORDER_STATUS_ACTIVE, ORDER_STATUS_COMPLETED,
    ORDER_SIDE_SELL, ORDER_SIDE_BUY,
    CLIENT_ORDER_ID_PREFIX, TRADE_TYPE_AUTO
)

class OrderManager:
    def __init__(self, order_file="order.json"):
        self.order_file = order_file
        self.month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]
        self.initialize_files()
        asyncio.create_task(self.monitor_month_change())
        asyncio.create_task(self.archive_completed_orders())

    def initialize_files(self):
        """Проверяет и создаёт order.json и архивный файл для текущего месяца."""
        try:
            if not os.path.exists(self.order_file):
                logger.info(f"Файл {self.order_file} не найден, создаётся новый")
                self.save_orders(self.order_file, [])

            current_time = datetime.now()
            archive_file = self.get_archive_filename(current_time)
            if not os.path.exists(archive_file):
                logger.info(f"Архивный файл {archive_file} не найден, создаётся новый")
                self.save_orders(archive_file, [])

            self.transfer_completed_orders()
        except Exception as e:
            logger.error(f"Ошибка инициализации файлов: {str(e)}")

    def get_archive_filename(self, dt):
        """Возвращает имя архивного файла для указанной даты."""
        month = self.month_names[dt.month - 1]
        year = dt.year
        return f"order_archive_{month}_{year}.json"

    def transfer_completed_orders(self):
        """Переносит все завершённые ордера в архив, оставляя активные продажи и их покупки."""
        try:
            orders = self.load_orders(self.order_file)
            active_orders = []
            orders_to_archive = []
            active_sell_parent_ids = set()  # ID покупок, связанных с активными продажами

            # Собираем ID покупок, связанных с активными продажами
            for order in orders:
                if (order["status"] == ORDER_STATUS_ACTIVE and
                    order["side"] == ORDER_SIDE_SELL and
                    order.get("client_order_id", "").startswith(CLIENT_ORDER_ID_PREFIX)):
                    parent_id = order.get("parent_order_id", "")
                    if parent_id:
                        active_sell_parent_ids.add(parent_id)

            # Обрабатываем ордера
            for order in orders:
                if (order["status"] == ORDER_STATUS_ACTIVE and
                    order["side"] == ORDER_SIDE_SELL and
                    order.get("client_order_id", "").startswith(CLIENT_ORDER_ID_PREFIX)):
                    active_orders.append(order)
                elif (order["status"] == ORDER_STATUS_COMPLETED and
                      order["side"] == ORDER_SIDE_BUY and
                      order["order_id"] in active_sell_parent_ids):
                    active_orders.append(order)
                else:
                    orders_to_archive.append(order)

            if orders_to_archive:
                orders_by_archive = {}
                for order in orders_to_archive:
                    execution_time = datetime.fromtimestamp(order["timestamp"] / 1000)
                    archive_file = self.get_archive_filename(execution_time)
                    if archive_file not in orders_by_archive:
                        orders_by_archive[archive_file] = []
                    orders_by_archive[archive_file].append(order)

                for archive_file_path, archive_orders_list in orders_by_archive.items():
                    existing_orders = self.load_orders(archive_file_path)
                    existing_orders.extend(archive_orders_list)
                    self.save_orders(archive_file_path, existing_orders)
                    logger.info(f"Перенесено {len(archive_orders_list)} ордеров в {archive_file_path}")

                self.save_orders(self.order_file, active_orders)
                logger.info(f"Обновлен {self.order_file}: оставлено {len(active_orders)} ордеров")
        except Exception as e:
            logger.error(f"Ошибка переноса исполненных ордеров: {str(e)}")

    async def monitor_month_change(self):
        """Проверяет смену месяца и создаёт новый архивный файл при необходимости."""
        while True:
            try:
                current_time = datetime.now()
                archive_file = self.get_archive_filename(current_time)
                if not os.path.exists(archive_file):
                    logger.info(f"Новый месяц, создаётся архивный файл: {archive_file}")
                    self.save_orders(archive_file, [])
                await asyncio.sleep(86400)  # Check once a day
            except Exception as e:
                logger.error(f"Ошибка проверки смены месяца: {str(e)}")
                await asyncio.sleep(3600) # Retry in an hour if error

    def save_orders(self, filename, orders_to_save):
        """Сохраняет ордера в указанный файл."""
        try:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(orders_to_save, f, indent=4, ensure_ascii=False)
            logger.info(f"Ордера сохранены в {filename}: {len(orders_to_save)} записей")
        except Exception as e:
            logger.error(f"Ошибка сохранения файла {filename}: {str(e)}")

    def load_orders(self, filename):
        """Загружает ордера из указанного файла."""
        try:
            if not os.path.exists(filename):
                logger.info(f"Файл {filename} не найден, возвращается пустой список")
                return []
            with open(filename, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    logger.warning(f"Файл {filename} пуст, возвращается пустой список")
                    return []
                loaded_orders_list = json.loads(content)
                for order in loaded_orders_list:
                    if "notified" not in order:
                        order["notified"] = False
                    if "parent_order_id" not in order:
                        order["parent_order_id"] = ""
                    if "client_order_id" not in order:
                        order["client_order_id"] = ""
                    if "trade_type" not in order:
                        order["trade_type"] = TRADE_TYPE_AUTO
                logger.debug(f"Загружено {len(loaded_orders_list)} ордеров из {filename}")
                return loaded_orders_list
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования файла {filename}: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Ошибка загрузки файла {filename}: {str(e)}")
            return []

    async def archive_completed_orders(self):
        """Перемещает исполненные ордера в архив через 1 минуту, оставляя активные продажи и их покупки."""
        while True:
            try:
                current_time = int(datetime.now().timestamp() * 1000)
                orders_list = self.load_orders(self.order_file)
                active_orders = []
                orders_to_archive = []
                active_sell_parent_ids = set()

                # Собираем ID покупок, связанных с активными продажами
                for order in orders_list:
                    if (order["status"] == ORDER_STATUS_ACTIVE and
                        order["side"] == ORDER_SIDE_SELL and
                        order.get("client_order_id", "").startswith(CLIENT_ORDER_ID_PREFIX)):
                        parent_id = order.get("parent_order_id", "")
                        if parent_id:
                            active_sell_parent_ids.add(parent_id)

                # Обрабатываем ордера
                for order in orders_list:
                    if (order["status"] == ORDER_STATUS_ACTIVE and
                        order["side"] == ORDER_SIDE_SELL and
                        order.get("client_order_id", "").startswith(CLIENT_ORDER_ID_PREFIX)):
                        active_orders.append(order)
                    elif (order["status"] == ORDER_STATUS_COMPLETED and
                          order["side"] == ORDER_SIDE_BUY and
                          order["order_id"] in active_sell_parent_ids):
                        active_orders.append(order)
                    elif (order["status"] == ORDER_STATUS_COMPLETED and
                          order["side"] == ORDER_SIDE_SELL and
                          order.get("client_order_id", "").startswith(CLIENT_ORDER_ID_PREFIX) and
                          current_time - order.get("timestamp", 0) > 60000):  # 1 minute
                        orders_to_archive.append(order)
                    else:
                        if order["status"] == ORDER_STATUS_COMPLETED:
                             orders_to_archive.append(order)
                        elif order not in active_orders and order not in orders_to_archive:
                             orders_to_archive.append(order)

                if orders_to_archive:
                    orders_by_archive = {}
                    for order_item in orders_to_archive:
                        execution_time = datetime.fromtimestamp(order_item["timestamp"] / 1000)
                        archive_file = self.get_archive_filename(execution_time)
                        if archive_file not in orders_by_archive:
                            orders_by_archive[archive_file] = []
                        orders_by_archive[archive_file].append(order_item)

                    for archive_file_path, archive_orders_list_to_save in orders_by_archive.items():
                        existing_orders = self.load_orders(archive_file_path)
                        for o_to_save in archive_orders_list_to_save:
                            if not any(existing_o["order_id"] == o_to_save["order_id"] for existing_o in existing_orders):
                                existing_orders.append(o_to_save)
                        self.save_orders(archive_file_path, existing_orders)
                        logger.info(f"Архивировано {len(archive_orders_list_to_save)} ордеров (с учетом дубликатов) в {archive_file_path}")

                    self.save_orders(self.order_file, active_orders)
                    logger.info(f"Обновлен {self.order_file}: оставлено {len(active_orders)} ордеров")

                await asyncio.sleep(60)
            except Exception as e:
                logger.error(f"Ошибка архивирования ордеров: {str(e)}")
                await asyncio.sleep(60)
