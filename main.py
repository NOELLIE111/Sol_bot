import asyncio
import sys
import os
import signal
import traceback
import aiohttp
import subprocess
from loguru import logger
from telegram_handler import setup_telegram_bot
from websocket import MEXCWebSocket
from trading import TradingBot
from config import send_notification
import telegram.error
import time
from memory_profiler import profile

def handle_shutdown(loop, tasks, telegram_app, ws):
    """Обработчик завершения для чистой остановки."""
    logger.info("Инициирована остановка бота (Ctrl+C или SIGTERM)")
    for task in tasks:
        task.cancel()
    async def shutdown():
        try:
            await asyncio.wait(tasks, timeout=3)
        except asyncio.TimeoutError:
            logger.warning("Некоторые задачи не завершились вовремя")
        if telegram_app:
            try:
                await telegram_app.updater.stop()
                await telegram_app.stop()
                await telegram_app.shutdown()
            except Exception as e:
                logger.warning(f"Ошибка остановки Telegram: {str(e)}")
        if ws:
            try:
                await ws.close()
            except Exception as e:
                logger.warning(f"Ошибка закрытия WebSocket: {str(e)}")
        logger.info("Бот полностью остановлен")
    loop.run_until_complete(shutdown())
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()
    logger.info("Завершение процесса main.py")
    sys.exit(0)

async def check_internet_connection(retries=2, retry_delay=2):
    """Проверяет наличие интернет-соединения с несколькими резервными методами.
    
    Args:
        retries (int): Количество повторных попыток для HTTP-запросов.
        retry_delay (int): Задержка между попытками в секундах.
    
    Returns:
        bool: True, если соединение активно, False в противном случае.
    """
    async def try_http_check(url, timeout=5, attempt=1):
        """Пытается выполнить HTTP-запрос к указанному URL."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=timeout) as resp:
                    if resp.status == 200:
                        logger.debug(f"Успешное соединение с {url}")
                        return True
                    logger.warning(f"Не удалось подключиться к {url}: HTTP {resp.status}")
                    return False
        except aiohttp.ClientConnectorError as e:
            logger.warning(f"Ошибка соединения с {url} (попытка {attempt}/{retries}): {str(e)}")
            return False
        except aiohttp.ClientError as e:
            logger.warning(f"Ошибка HTTP-запроса к {url} (попытка {attempt}/{retries}): {str(e)}")
            return False
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут запроса к {url} (попытка {attempt}/{retries})")
            return False
        except Exception as e:
            logger.warning(f"Неизвестная ошибка при запросе к {url} (попытка {attempt}/{retries}): {str(e)}")
            return False

    # Список URL для проверки (резервные варианты)
    check_urls = [
        "https://api.mexc.com/api/v3/ping",  # Основной API MEXC
        "https://1.1.1.1",                    # Cloudflare DNS
        "https://8.8.8.8",                    # Google DNS
    ]

    # Проверка HTTP-соединений
    for url in check_urls:
        for attempt in range(1, retries + 1):
            if await try_http_check(url, timeout=5, attempt=attempt):
                return True
            if attempt < retries:
                logger.debug(f"Ожидание {retry_delay} сек перед повторной попыткой для {url}")
                await asyncio.sleep(retry_delay)

    # Проверка пинга, если HTTP не сработал
    try:
        ping_cmd = ["ping", "-c", "1", "-W", "2", "8.8.8.8"]
        result = subprocess.run(
            ping_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            logger.debug("Успешный пинг до 8.8.8.8")
            return True
        logger.warning(f"Пинг до 8.8.8.8 не удался: {result.stderr}")
    except FileNotFoundError:
        logger.error("Команда ping не найдена. Установите iputils-ping: sudo apt-get install iputils-ping")
    except subprocess.TimeoutExpired:
        logger.warning("Таймаут выполнения команды ping")
    except Exception as e:
        logger.warning(f"Ошибка системного пинга: {str(e)}")

    logger.error("Все проверки соединения не удались")
    return False

async def watchdog(telegram_app, ws, trading_bot, max_retries=5):
    """Watchdog для отслеживания состояния бота и завершения процесса."""
    await asyncio.sleep(10)  # Даем время на инициализацию
    retry_count = 0
    internet_lost_notified = False
    was_internet_lost = False
    while retry_count < max_retries:
        try:
            # Проверяем состояние Telegram
            if not telegram_app.updater.running:
                logger.error("Telegram-поллинг не активен")
                raise Exception("Telegram stopped")
            # Проверяем состояние WebSocket
            if ws.ws is None or ws.ws.closed:
                logger.error("WebSocket не подключен")
                raise Exception("WebSocket closed")
            # Проверяем активность WebSocket (сообщения в последние 120 секунд)
            if time.time() - ws.last_message_time > 120:
                logger.error(f"WebSocket неактивен (нет сообщений более 120 секунд, последнее сообщение: {ws.last_message_time})")
                raise Exception("WebSocket inactive")
            # Проверяем, отвечает ли TradingBot
            if await check_internet_connection():
                usdt_balance = await trading_bot.exchange.get_balance("USDT")
                if usdt_balance is None:
                    logger.error("TradingBot не отвечает (ошибка API)")
                    raise Exception("TradingBot API error")
                # Интернет восстановлен, завершаем процесс
                if was_internet_lost:
                    logger.info("Интернет-соединение восстановлено, завершаем процесс для перезапуска")
                    await send_notification(telegram_app, "🌐 Интернет-соединение восстановлено. Перезапуск бота.")
                    logger.info("Завершение процесса main.py с кодом 42 (интернет восстановлен)")
                    sys.exit(42)
            else:
                if not internet_lost_notified:
                    logger.warning("Потеряно интернет-соединение. Ожидание восстановления...")
                    await send_notification(telegram_app, "⚠️ Потеряно интернет-соединение. Ожидание восстановления...")
                    internet_lost_notified = True
                was_internet_lost = True
                await asyncio.sleep(60)
                continue
            # Сбрасываем счетчик и уведомления
            retry_count = 0
            if internet_lost_notified:
                logger.info("Интернет-соединение восстановлено, завершаем процесс для перезапуска")
                await send_notification(telegram_app, "🌐 Интернет-соединение восстановлено. Перезапуск бота.")
                logger.info("Завершение процесса main.py с кодом 42 (интернет восстановлен)")
                sys.exit(42)
            internet_lost_notified = False
            was_internet_lost = False
            logger.debug(f"WebSocket активен, последнее сообщение: {ws.last_message_time}")
            await asyncio.sleep(60)
        except Exception as e:
            retry_count += 1
            logger.error(f"Watchdog: Обнаружен сбой ({retry_count}/{max_retries}): {str(e)}")
            if retry_count >= max_retries:
                logger.critical("Watchdog: Превышен лимит попыток, завершаем процесс для перезапуска")
                await send_notification(telegram_app, "⚠️ Критический сбой! Перезапуск бота.")
                logger.info("Завершение процесса main.py с кодом 42 (критический сбой)")
                sys.exit(42)
            await asyncio.sleep(10)

async def run_polling(telegram_app):
    """Запускает Telegram-поллинг с обработкой сбоев сети."""
    reconnect_delay = 10
    attempt = 0
    while True:
        try:
            await telegram_app.updater.start_polling()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("Telegram-поллинг отменён")
            break
        except telegram.error.NetworkError as e:
            attempt += 1
            logger.warning(f"Сетевая ошибка Telegram-поллинга (попытка {attempt}): {str(e)}")
            if not await check_internet_connection():
                logger.warning("Отсутствует интернет. Ожидание восстановления...")
                while not await check_internet_connection():
                    await asyncio.sleep(reconnect_delay * (2 ** min(attempt, 5)))
            else:
                logger.warning(f"Временный сбой Telegram. Повтор через {reconnect_delay * (2 ** min(attempt, 5))} сек")
            await asyncio.sleep(reconnect_delay * (2 ** min(attempt, 5)))
        except Exception as e:
            logger.error(f"Ошибка Telegram-поллинга: {str(e)}\n{traceback.format_exc()}")
            attempt += 1
            await asyncio.sleep(reconnect_delay * (2 ** min(attempt, 5)))

async def run_websocket(ws, trading_bot):
    """Запускает WebSocket с обработкой сбоев сети."""
    reconnect_delay = 10
    attempt = 0
    while True:
        try:
            if not ws.listen_key or not await ws.extend_listen_key():
                logger.info("Создание нового listenKey")
                await ws.create_listen_key()
            await ws.connect()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            logger.info("WebSocket отменён")
            break
        except (aiohttp.ClientError, ConnectionError) as e:
            attempt += 1
            logger.error(f"Ошибка WebSocket (попытка {attempt}): {str(e)}\n{traceback.format_exc()}")
            if not await check_internet_connection():
                logger.warning("Отсутствует интернет. Ожидание восстановления...")
                await send_notification(trading_bot.telegram_app, "⚠️ WebSocket отключен из-за потери интернета")
                while not await check_internet_connection():
                    await asyncio.sleep(reconnect_delay * (2 ** min(attempt, 5)))
            else:
                logger.warning(f"Временный сбой WebSocket. Повтор через {reconnect_delay * (2 ** min(attempt, 5))} сек")
            await asyncio.sleep(reconnect_delay * (2 ** min(attempt, 5)))
            await trading_bot.sync_orders()
        except Exception as e:
            attempt += 1
            logger.error(f"Неожиданная ошибка WebSocket (попытка {attempt}): {str(e)}\n{traceback.format_exc()}")
            await send_notification(trading_bot.telegram_app, f"⚠️ Неожиданная ошибка WebSocket: {str(e)}")
            await asyncio.sleep(reconnect_delay * (2 ** min(attempt, 5)))

async def run_bot_with_reconnect():
    """Запускает бота с автоматическим перезапуском."""
    max_attempts = 10
    attempt = 0
    tasks = []
    telegram_app = None
    ws = None

    while attempt < max_attempts:
        try:
            if not await check_internet_connection():
                logger.error(f"Отсутствует интернет-соединение (попытка {attempt + 1}/{max_attempts})")
                attempt += 1
                await asyncio.sleep(10 * (2 ** min(attempt, 5)))
                continue

            logger.info(f"Попытка запуска бота #{attempt + 1}")
            attempt = 0

            logger.remove()
            logger.add("bot.log", rotation="10 MB", level="INFO")
            logger.add(sys.stdout, colorize=True, level="INFO")
            logger.info("Запуск бота")

            API_KEY = os.getenv("MEXC_API_KEY")
            SECRET_KEY = os.getenv("MEXC_SECRET_KEY")
            if not API_KEY or not SECRET_KEY:
                logger.error("MEXC_API_KEY или MEXC_SECRET_KEY не установлены в .env")
                return

            telegram_app = setup_telegram_bot()
            trading_bot = TradingBot(telegram_app)
            ws = MEXCWebSocket(
                on_price_update=trading_bot.on_price_update,
                trading_bot=trading_bot
            )

            telegram_app.bot_data["trading_bot"] = trading_bot

            await telegram_app.initialize()
            await telegram_app.start()
            await trading_bot.sync_orders()

            try:
                open_orders = await trading_bot.exchange.get_open_orders()
                for order_id, status, price, client_order_id in open_orders:
                    if status in ["NEW", "PARTIALLY_FILLED"] and client_order_id.startswith("BOT_"):
                        orders = trading_bot.order_manager.load_orders(trading_bot.order_manager.order_file)
                        if not any(o["order_id"] == order_id for o in orders):
                            order_details = await trading_bot.exchange.check_order_status(order_id)
                            quantity = "0"
                            if order_details[0]:
                                quantity = str(round(float(order_details[1]), 2)) if order_details[1] else "0"
                            orders.append({
                                "order_id": order_id,
                                "client_order_id": client_order_id,
                                "side": "SELL",
                                "type": "LIMIT",
                                "status": "active",
                                "quantity": quantity,
                                "price": str(round(price, 2)),
                                "amount": "0",
                                "timestamp": int(time.time() * 1000),
                                "profit": "0",
                                "notified": False,
                                "parent_order_id": ""
                            })
                            trading_bot.order_manager.save_orders(trading_bot.order_manager.order_file, orders)
                            trading_bot.position_active = True
                            trading_bot.order_id = order_id
                            logger.info(f"Восстановлен активный ордер: {order_id}, clientOrderId: {client_order_id}, quantity: {quantity}")
            except aiohttp.ClientError as e:
                logger.error(f"Ошибка получения открытых ордеров: {str(e)}")
                continue

            tasks = [
                asyncio.create_task(run_polling(telegram_app)),
                asyncio.create_task(run_websocket(ws, trading_bot)),
                asyncio.create_task(watchdog(telegram_app, ws, trading_bot))
            ]

            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            logger.info("Основной цикл бота отменён")
            break
        except aiohttp.ClientError as e:
            attempt += 1
            logger.error(f"Критическая сетевая ошибка (попытка {attempt}/{max_attempts}): {str(e)}\n{traceback.format_exc()}")
            await send_notification(telegram_app, f"⚠️ Критический сбой (попытка {attempt}/{max_attempts}): {str(e)}")
            if attempt >= max_attempts:
                logger.critical("Превышен лимит попыток перезапуска")
                await send_notification(telegram_app, "🚨 Бот остановлен из-за превышения попыток перезапуска")
                logger.info("Завершение процесса main.py с кодом 42 (превышен лимит попыток)")
                sys.exit(42)
            await asyncio.sleep(10 * (2 ** min(attempt, 5)))
        except Exception as e:
            attempt += 1
            logger.error(f"Критическая ошибка (попытка {attempt}/{max_attempts}): {str(e)}\n{traceback.format_exc()}")
            await send_notification(telegram_app, f"⚠️ Критический сбой (попытка {attempt}/{max_attempts}): {str(e)}")
            if attempt >= max_attempts:
                logger.critical("Превышен лимит попыток перезапуска")
                await send_notification(telegram_app, "🚨 Бот остановлен из-за превышения попыток перезапуска")
                logger.info("Завершение процесса main.py с кодом 42 (превышен лимит попыток)")
                sys.exit(42)
            await asyncio.sleep(10 * (2 ** min(attempt, 5)))

        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.wait(tasks, timeout=3)
            except asyncio.TimeoutError:
                logger.warning("Некоторые задачи не завершились вовремя")
            if telegram_app:
                try:
                    await telegram_app.updater.stop()
                    await telegram_app.stop()
                    await telegram_app.shutdown()
                except Exception as e:
                    logger.warning(f"Ошибка остановки Telegram: {str(e)}")
            if ws:
                try:
                    await ws.close()
                except Exception as e:
                    logger.warning(f"Ошибка закрытия WebSocket: {str(e)}")
            logger.info("Бот остановлен")

async def main():
    """Главная функция для запуска бота."""
    loop = asyncio.get_event_loop()
    tasks = []
    telegram_app = None
    ws = None

    def signal_handler(sig, frame):
        handle_shutdown(loop, tasks, telegram_app, ws)
        release_lock(lock_fd)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await run_bot_with_reconnect()
    finally:
        release_lock(lock_fd)

if __name__ == "__main__":
    asyncio.run(main())