# telegram_handler.py
import json
import os
import re
from loguru import logger
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from telegram.error import BadRequest, NetworkError
from config import settings, save_state, TELEGRAM_TOKEN
from trading import TradingBot
from datetime import datetime

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start, сохраняет chat_id и показывает меню."""
    chat_id = update.message.chat_id
    try:
        state = {}
        from config import STATE_FILE
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        state["chat_id"] = chat_id
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        logger.info(f"chat_id сохранён: {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка сохранения chat_id: {str(e)}")

    await show_main_menu(update, context, text=(
        "👋 Добро пожаловать!\n\n"
        "Перед началом установите настройки:\n"
        "1. Процент падения (мин. 0.5%)\n"
        "2. Процент прибыли (мин. 0.5%)\n"
        "3. Размер ордера (мин. 2 USDT)\n"
        "4. Лимит баланса (опционально, мин. 2 USDT)\n"
        "5. Комиссии (тейкер и мейкер, опционально)\n\n"
        "Используйте кнопки для настройки.\n"
        "Команды:\n"
        "/autobuy - Запустить автоматическую торговлю\n"
        "/buy - Совершить ручную покупку\n"
        "/balance - Показать баланс USDT\n"
        "/price - Показать текущую цену SOL/USDT\n"
        "/settings - Проверить настройки\n"
        "/stop - Остановить торговлю\n"
        "/stats - Показать статистику прибыли\n"
        "/orders - Показать активные ордера\n"
        "/limiter - Установить лимит баланса\n"
        "/fee_taker - Установить комиссию тейкера\n"
        "/fee_maker - Установить комиссию мейкера\n"
        "Формат: /stats [DD.MM.YYYY | MM.YYYY | all]"
    ))

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Показывает главное меню с кнопками."""
    keyboard = [
        [InlineKeyboardButton("📉 Установить % падения", callback_data="set_drop")],
        [InlineKeyboardButton("💰 Установить % прибыли", callback_data="set_profit")],
        [InlineKeyboardButton("💸 Установить размер ордера", callback_data="set_order")],
        [InlineKeyboardButton("💵 Установить лимит баланса", callback_data="limiter")],
        [InlineKeyboardButton("💳 Установить комиссии", callback_data="set_fees")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Ошибка отправки меню: {str(e)}")

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущий баланс USDT."""
    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    try:
        usdt_balance = await trading_bot.get_usdt_balance()
        used_balance = await trading_bot.get_used_balance()
        fixed_limit = settings.get("fixed_balance_limit", "не установлен")
        message = (
            f"💰 Баланс USDT: {usdt_balance:.4f} USDT\n"
            f"💸 Задействовано: {used_balance:.4f} USDT\n"
            f"📊 Лимит баланса: {fixed_limit}{' USDT' if isinstance(fixed_limit, (int, float)) else ''}"
        )
        await update.message.reply_text(message)
        logger.info(f"Запрошен баланс USDT: {usdt_balance:.4f}, задействовано: {used_balance:.4f}")
    except Exception as e:
        await update.message.reply_text("Ошибка при получении баланса.")
        logger.error(f"Ошибка в /balance: {str(e)}")

async def price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущую цену SOL/USDT."""
    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    try:
        current_price, _ = await trading_bot.get_price_info()
        if current_price is None:
            await update.message.reply_text("⚠️ Текущая цена SOL/USDT недоступна.")
            logger.warning("Текущая цена SOL/USDT не получена")
            return
        await update.message.reply_text(f"📈 Текущая цена SOL/USDT: {current_price:.2f} USDT")
        logger.info(f"Запрошена цена SOL/USDT: {current_price:.2f}")
    except Exception as e:
        await update.message.reply_text("Ошибка при получении цены.")
        logger.error(f"Ошибка в /price: {str(e)}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику прибыли за указанный период."""
    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    args = context.args
    period = args[0] if args else None
    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }

    try:
        if not period:
            await update.message.reply_text("Укажите период: /stats [DD.MM.YYYY | MM.YYYY | all]")
            return
        elif period.lower() == "all":
            total_trades, total_profit = await trading_bot.calculate_profit("all")
            message = (
                f"📊 *Статистика за всё время:*\n"
                f"📈 *Количество сделок:* `{total_trades}`\n"
                f"💰 *Прибыль:* `{total_profit:.4f} USDT`"
            )
        elif len(period.split('.')) == 3:
            day, month, year = map(int, period.split('.'))
            date = datetime(year, month, day)
            total_trades, total_profit = await trading_bot.calculate_profit("day", date)
            message = (
                f"📊 *Статистика за {period}:*\n"
                f"📈 *Количество сделок:* `{total_trades}`\n"
                f"💰 *Прибыль:* `{total_profit:.4f} USDT`"
            )
        elif len(period.split('.')) == 2:
            month, year = map(int, period.split('.'))
            if month < 1 or month > 12:
                await update.message.reply_text("Месяц должен быть от 1 до 12. Пример: /stats 05.2025")
                return
            month_name = month_names.get(month, "Неизвестный месяц")
            total_trades, total_profit = await trading_bot.calculate_profit("month", datetime(year, month, 1))
            message = (
                f"📊 *Статистика за {month_name} {year}:*\n"
                f"📈 *Количество сделок:* `{total_trades}`\n"
                f"💰 *Прибыль:* `{total_profit:.4f} USDT`"
            )
        else:
            await update.message.reply_text("Неверный формат! Используйте: /stats [DD.MM.YYYY | MM.YYYY | all]")
            return

        await update.message.reply_text(message, parse_mode="Markdown")
        logger.info(f"Запрошена статистика за {period}")
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: Неверный формат даты. Пример: /stats 13.05.2025")
        logger.error(f"Ошибка в /stats: {str(e)}")
    except Exception as e:
        await update.message.reply_text("Ошибка при получении статистики.")
        logger.error(f"Ошибка в /stats: {str(e)}")

async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список активных ордеров с фильтрами."""
    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    try:
        args = context.args
        filters = {"sell_price": None, "buy_price": None}
        for arg in args:
            match = re.match(r"^(sell_price|buy_price)\s*(>|>=|<|<=|=)\s*(\d+\.?\d*)$", arg)
            if match:
                key, operator, value = match.groups()
                try:
                    filters[key] = {"operator": operator, "value": float(value)}
                except ValueError:
                    await update.message.reply_text(f"Ошибка: Неверное значение для {key}: {value}")
                    logger.error(f"Неверное значение фильтра: {arg}")
                    return
            else:
                await update.message.reply_text(
                    "Ошибка: Неверный формат фильтра. Пример: /orders sell_price>168.00 buy_price<167.00\n"
                    "Поддерживаемые операторы: >, <, >=, <=, ="
                )
                logger.error(f"Неверный формат фильтра: {arg}")
                return

        orders = trading_bot.order_manager.load_orders(trading_bot.order_manager.order_file)
        active_orders = []

        for order in orders:
            if (order["status"] == "active" and
                order["side"] == "SELL" and
                order.get("client_order_id", "").startswith("BOT_")):
                try:
                    sell_price = float(order["price"])
                    parent_id = order.get("parent_order_id", "")
                    buy_price = None
                    for buy_order in orders:
                        if buy_order["order_id"] == parent_id and buy_order["side"] == "BUY":
                            buy_price = float(buy_order["price"])
                            break

                    passes_filters = True
                    if filters["sell_price"]:
                        f = filters["sell_price"]
                        if f["operator"] == ">":
                            passes_filters = passes_filters and sell_price > f["value"]
                        elif f["operator"] == ">=":
                            passes_filters = passes_filters and sell_price >= f["value"]
                        elif f["operator"] == "<":
                            passes_filters = passes_filters and sell_price < f["value"]
                        elif f["operator"] == "<=":
                            passes_filters = passes_filters and sell_price <= f["value"]
                        elif f["operator"] == "=":
                            passes_filters = passes_filters and sell_price == f["value"]

                    if filters["buy_price"] and buy_price is not None:
                        f = filters["buy_price"]
                        if f["operator"] == ">":
                            passes_filters = passes_filters and buy_price > f["value"]
                        elif f["operator"] == ">=":
                            passes_filters = passes_filters and buy_price >= f["value"]
                        elif f["operator"] == "<":
                            passes_filters = passes_filters and buy_price < f["value"]
                        elif f["operator"] == "<=":
                            passes_filters = passes_filters and buy_price <= f["value"]
                        elif f["operator"] == "=":
                            passes_filters = passes_filters and buy_price == f["value"]
                    elif filters["buy_price"]:
                        passes_filters = False

                    if passes_filters:
                        active_orders.append({
                            "order": order,
                            "buy_price": buy_price
                        })
                except (ValueError, TypeError) as e:
                    logger.warning(f"Пропущен ордер {order['order_id']}: некорректные данные ({str(e)})")
                    continue

        if not active_orders:
            message = "📊 Нет активных ордеров, соответствующих фильтрам." if args else "📊 Активных ордеров нет."
            await update.message.reply_text(message)
            logger.info(f"Запрошены активные ордера с фильтрами {args}: список пуст")
            return

        chat_id = update.message.chat_id
        if chat_id not in context.user_data:
            context.user_data[chat_id] = {}
        context.user_data[chat_id]["orders"] = active_orders
        context.user_data[chat_id]["filters"] = args
        context.user_data[chat_id]["orders_page"] = 0
        context.user_data[chat_id]["message_id"] = None

        message, reply_markup = await format_orders_page(active_orders, 0)
        sent_message = await update.message.reply_text(message, parse_mode="Markdown", reply_markup=reply_markup)
        context.user_data[chat_id]["message_id"] = sent_message.message_id
        logger.info(f"Запрошены активные ордера с фильтрами {args}: найдено {len(active_orders)} ордеров")
    except Exception as e:
        await update.message.reply_text("Ошибка при получении списка ордеров.")
        logger.error(f"Ошибка в /orders: {str(e)}")

async def orders_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок постраничного вывода."""
    query = update.callback_query
    await query.answer()
    logger.debug(f"Callback получен: {query.data}")

    chat_id = query.message.chat_id
    if chat_id not in context.user_data or "orders" not in context.user_data[chat_id]:
        await query.message.reply_text("Ошибка: Сессия истекла. Выполните /orders снова.")
        logger.warning(f"Сессия /orders истекла для chat_id {chat_id}")
        return

    active_orders = context.user_data[chat_id]["orders"]
    current_page = context.user_data[chat_id]["orders_page"]
    action = query.data

    if action == "orders_prev" and current_page > 0:
        current_page -= 1
    elif action == "orders_next":
        max_page = (len(active_orders) - 1) // 5
        if current_page < max_page:
            current_page += 1
        else:
            return

    context.user_data[chat_id]["orders_page"] = current_page
    message, reply_markup = await format_orders_page(active_orders, current_page)

    try:
        await query.message.edit_text(message, parse_mode="Markdown", reply_markup=reply_markup)
        logger.debug(f"Обновлена страница /orders: страница {current_page + 1}")
    except BadRequest as e:
        logger.warning(f"Ошибка редактирования сообщения /orders: {str(e)}")
        await query.message.reply_text("Ошибка при обновлении списка ордеров.")

async def format_orders_page(active_orders, page):
    """Форматирует страницу активных ордеров."""
    orders_per_page = 5
    start_idx = page * orders_per_page
    end_idx = min(start_idx + orders_per_page, len(active_orders))
    max_page = (len(active_orders) - 1) // orders_per_page

    message = f"📊 *Активные ордера (Страница {page + 1}/{max_page + 1}):*\n\n"
    for idx, item in enumerate(active_orders[start_idx:end_idx], start_idx + 1):
        order = item["order"]
        buy_price = item["buy_price"]
        order_time = datetime.fromtimestamp(order["timestamp"] / 1000).strftime("%d.%m.%Y %H:%M:%S")
        trade_type = "Автоматическая" if order.get("trade_type", "auto") == "auto" else "Ручная"
        message += (
            f"{idx}️⃣ *Ордер:* `{order['order_id']}`\n"
            f"📈 *Тип:* {trade_type}\n"
            f"💰 *Цена продажи:* {order['price']} USDT\n"
            f"💸 *Цена покупки:* {'Не найдена' if buy_price is None else f'{buy_price:.2f} USDT'}\n"
            f"📦 *Количество:* {order['quantity']} SOL\n"
            f"🕒 *Время:* {order_time}\n\n"
        )

    if len(message) > 4096:
        message = message[:4000] + "\n⚠️ Сообщение обрезано из-за лимита Telegram."

    keyboard = []
    if page > 0:
        keyboard.append(InlineKeyboardButton("⬅️ Назад", callback_data="orders_prev"))
    if page < max_page:
        keyboard.append(InlineKeyboardButton("Вперёд ➡️", callback_data="orders_next"))
    reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None

    return message, reply_markup

async def autobuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает автоматическую торговлю."""
    if any(settings[key] is None for key in ["drop_percent", "profit_percent", "order_size"]):
        await update.message.reply_text("Ошибка: Установите все настройки!")
        logger.warning("Попытка запуска торговли без всех настроек")
        return

    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    usdt_balance = await trading_bot.get_usdt_balance()
    order_size = settings["order_size"]
    if usdt_balance < order_size:
        settings["autobuy_enabled"] = True
        save_state()
        await update.message.reply_text(f"⚠️ Недостаточно USDT: {usdt_balance} < {order_size}\nТорговля запущена!")
        logger.error(f"Недостаточно USDT: {usdt_balance} < {order_size}")
        return

    orders = trading_bot.order_manager.load_orders(trading_bot.order_manager.order_file)
    active_sell_orders = [order for order in orders if order["status"] == "active" and order["side"] == "SELL"]
    if active_sell_orders:
        settings["autobuy_enabled"] = True
        save_state()
        await update.message.reply_text("Торговля уже запущена!")
        logger.info(f"Торговля не запущена: найдено {len(active_sell_orders)} активных ордеров")
        return

    settings["autobuy_enabled"] = True
    save_state()
    await trading_bot.start_trading()
    await update.message.reply_text("Торговля запущена!")
    logger.info("Торговля запущена")

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Совершает ручную покупку."""
    if any(settings[key] is None for key in ["drop_percent", "profit_percent", "order_size"]):
        await update.message.reply_text("Ошибка: Установите все настройки!")
        logger.warning("Попытка ручной покупки без всех настроек")
        return

    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    success = await trading_bot.manual_buy()
    if success:
        await update.message.reply_text("Ручная покупка выполнена!")
        logger.info("Ручная покупка выполнена")
    else:
        logger.debug("Ручная покупка не выполнена, уведомление отправлено в manual_buy")

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает текущие настройки."""
    drop = settings["drop_percent"] if settings["drop_percent"] is not None else "не установлен"
    profit = settings["profit_percent"] if settings["profit_percent"] is not None else "не установлен"
    size = settings["order_size"] if settings["order_size"] is not None else "не установлен"
    balance_limit = settings["fixed_balance_limit"] if settings["fixed_balance_limit"] is not None else "не установлен"
    taker_fee = settings["taker_fee_percent"] if settings["taker_fee_percent"] is not None else "не установлен"
    maker_fee = settings["maker_fee_percent"] if settings["maker_fee_percent"] is not None else "не установлен"
    autobuy = "включена" if settings["autobuy_enabled"] else "выключена"

    message = (
        f"⚙️ *Настройки:*\n"
        f"📉 *Падение:* {drop}{' %' if isinstance(drop, (int, float)) else ''}\n"
        f"💰 *Прибыль:* {profit}{' %' if isinstance(profit, (int, float)) else ''}\n"
        f"💸 *Ордер:* {size}{' USDT' if isinstance(size, (int, float)) else ''}\n"
        f"💵 *Лимит баланса:* {balance_limit}{' USDT' if isinstance(balance_limit, (int, float)) else ''}\n"
        f"💳 *Комиссия тейкера:* {taker_fee}{' %' if isinstance(taker_fee, (int, float)) else ''}\n"
        f"💳 *Комиссия мейкера:* {maker_fee}{' %' if isinstance(maker_fee, (int, float)) else ''}\n"
        f"🤖 *Торговля:* {autobuy}"
    )

    await show_main_menu(update, context, text=message)
    logger.info("Показаны настройки")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Останавливает автоматическую торговлю, сохраняя обработку активных ордеров и доступ к другим функциям."""
    if not settings["autobuy_enabled"]:
        await update.message.reply_text("Автоматическая торговля уже остановлена.")
        logger.info("Попытка остановки торговли, но autobuy_enabled уже False")
        return

    settings["autobuy_enabled"] = False
    save_state()
    
    trading_bot = context.bot_data.get("trading_bot")
    if trading_bot:
        orders = trading_bot.order_manager.load_orders(trading_bot.order_manager.order_file)
        active_sell_orders = [order for order in orders if order["status"] == "active" and order["side"] == "SELL" and order.get("client_order_id", "").startswith("BOT_")]
        active_orders_count = len(active_sell_orders)
        message = (
            f"🤖 Автоматическая торговля остановлена.\n"
            f"📊 Активные ордера на продажу ({active_orders_count}) будут обработаны.\n"
            f"🔄 Ручные покупки и другие команды остаются доступны."
        )
    else:
        message = "🤖 Автоматическая торговля остановлена."

    await update.message.reply_text(message)
    logger.info(f"Автоматическая торговля остановлена, активных ордеров на продажу: {active_orders_count if trading_bot else 'неизвестно'}")

async def limiter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает лимит баланса для торговли."""
    trading_bot = context.bot_data.get("trading_bot")
    if not trading_bot:
        await update.message.reply_text("Ошибка: Бот не инициализирован.")
        logger.error("TradingBot не найден в context.bot_data")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Укажите лимит баланса (мин. 2 USDT, например: /limiter 50) или 0 для снятия лимита.")
        return

    try:
        value = float(args[0])
        if value == 0:
            settings["fixed_balance_limit"] = None
            save_state()
            await update.message.reply_text("✅ Лимит баланса снят.")
            logger.info("Лимит баланса снят")
            return
        if value < 2.0:
            await update.message.reply_text("Ошибка: Лимит баланса должен быть не менее 2 USDT.")
            logger.error("Попытка установить лимит баланса менее 2 USDT")
            return

        usdt_balance = await trading_bot.get_usdt_balance()
        if value > usdt_balance:
            await update.message.reply_text(f"Ошибка: Лимит ({value:.2f} USDT) превышает текущий баланс ({usdt_balance:.4f} USDT).")
            logger.error(f"Попытка установить лимит {value} USDT при балансе {usdt_balance}")
            return

        used_balance = await trading_bot.get_used_balance()
        if used_balance > value:
            await update.message.reply_text(
                f"Ошибка: Задействовано {used_balance:.4f} USDT в активных ордерах, что превышает новый лимит {value:.2f} USDT.\n"
                "Дождитесь исполнения ордеров или увеличьте лимит."
            )
            logger.error(f"Попытка установить лимит {value} USDT при задействованном балансе {used_balance}")
            return

        settings["fixed_balance_limit"] = value
        save_state()
        await update.message.reply_text(f"✅ Лимит баланса установлен: {value:.2f} USDT")
        logger.info(f"Лимит баланса установлен: {value:.2f} USDT")
    except ValueError:
        await update.message.reply_text("Ошибка: Введите число, например, 50.0")
        logger.error(f"Неверный формат ввода для /limiter: {args[0]}")
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")
        logger.error(f"Ошибка в /limiter: {str(e)}")

async def fee_taker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает комиссию тейкера."""
    args = context.args
    if not args:
        await update.message.reply_text("Укажите комиссию тейкера в процентах (например: /fee_taker 0.05)")
        return

    try:
        value = float(args[0])
        if value < 0:
            await update.message.reply_text("Ошибка: Комиссия не может быть отрицательной.")
            logger.error("Попытка установить отрицательную комиссию тейкера")
            return

        settings["taker_fee_percent"] = value
        save_state()
        await update.message.reply_text(f"✅ Комиссия тейкера установлена: {value:.4f}%")
        logger.info(f"Комиссия тейкера установлена: {value:.4f}%")
    except ValueError:
        await update.message.reply_text("Ошибка: Введите число, например, 0.05")
        logger.error(f"Неверный формат ввода для /fee_taker: {args[0]}")
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")
        logger.error(f"Ошибка в /fee_taker: {str(e)}")

async def fee_maker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Устанавливает комиссию мейкера."""
    args = context.args
    if not args:
        await update.message.reply_text("Укажите комиссию мейкера в процентах (например: /fee_maker 0.0)")
        return

    try:
        value = float(args[0])
        if value < 0:
            await update.message.reply_text("Ошибка: Комиссия не может быть отрицательной.")
            logger.error("Попытка установить отрицательную комиссию мейкера")
            return

        settings["maker_fee_percent"] = value
        save_state()
        await update.message.reply_text(f"✅ Комиссия мейкера установлена: {value:.4f}%")
        logger.info(f"Комиссия мейкера установлена: {value:.4f}%")
    except ValueError:
        await update.message.reply_text("Ошибка: Введите число, например, 0.0")
        logger.error(f"Неверный формат ввода для /fee_maker: {args[0]}")
    except Exception as e:
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")
        logger.error(f"Ошибка в /fee_maker: {str(e)}")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатий на кнопки."""
    query = update.callback_query
    await query.answer()
    logger.debug(f"Callback получен: {query.data}")

    try:
        if query.data == "set_drop":
            await query.message.reply_text("Введите процент падения (мин. 0.5%, например, 2.0):")
            context.user_data["setting"] = "drop_percent"
            context.user_data["message_id"] = query.message.message_id
        elif query.data == "set_profit":
            await query.message.reply_text("Введите процент прибыли (мин. 0.5%, например, 1.0):")
            context.user_data["setting"] = "profit_percent"
            context.user_data["message_id"] = query.message.message_id
        elif query.data == "set_order":
            await query.message.reply_text("Введите размер ордера (мин. 2 USDT, например, 10):")
            context.user_data["setting"] = "order_size"
            context.user_data["message_id"] = query.message.message_id
        elif query.data == "limiter":
            await query.message.reply_text("Введите лимит баланса (мин. 2 USDT, например, 50) или 0 для снятия лимита:")
            context.user_data["setting"] = "fixed_balance_limit"
            context.user_data["message_id"] = query.message.message_id
        elif query.data == "set_fees":
            keyboard = [
                [InlineKeyboardButton("💳 Установить комиссию тейкера", callback_data="fee_taker")],
                [InlineKeyboardButton("💳 Установить комиссию мейкера", callback_data="fee_maker")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text("Выберите тип комиссии:", reply_markup=reply_markup)
        elif query.data == "fee_taker":
            await query.message.reply_text("Введите комиссию тейкера в процентах (например, 0.05):")
            context.user_data["setting"] = "taker_fee_percent"
            context.user_data["message_id"] = query.message.message_id
        elif query.data == "fee_maker":
            await query.message.reply_text("Введите комиссию мейкера в процентах (например, 0.0):")
            context.user_data["setting"] = "maker_fee_percent"
            context.user_data["message_id"] = query.message.message_id
    except Exception as e:
        logger.error(f"Ошибка обработки callback: {str(e)}")
        await query.message.reply_text("Ошибка при обработке кнопки. Попробуйте снова.")

async def set_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик ввода настроек."""
    setting = context.user_data.get("setting")
    message_id = context.user_data.get("message_id")
    if not setting:
        await update.message.reply_text("Ошибка: Выберите настройку через кнопку.")
        await show_main_menu(update, context, text="Выберите действие:")
        context.user_data.clear()
        return

    text = update.message.text.strip()
    try:
        value = float(text)
        if setting == "drop_percent" and value < 0.5:
            raise ValueError("Процент падения должен быть не менее 0.5%")
        elif setting == "profit_percent" and value < 0.5:
            raise ValueError("Процент прибыли должен быть не менее 0.5%")
        elif setting == "order_size" and value < 2.0:
            raise ValueError("Размер ордера должен быть не менее 2 USDT")
        elif setting == "fixed_balance_limit":
            if value == 0:
                settings["fixed_balance_limit"] = None
                save_state()
                await update.message.reply_text("✅ Лимит баланса снят.")
                logger.info("Лимит баланса снят")
                context.user_data.clear()
                return
            if value < 2.0:
                raise ValueError("Лимит баланса должен быть не менее 2 USDT")
            trading_bot = context.bot_data.get("trading_bot")
            if trading_bot:
                usdt_balance = await trading_bot.get_usdt_balance()
                if value > usdt_balance:
                    raise ValueError(f"Лимит ({value:.2f} USDT) превышает текущий баланс ({usdt_balance:.4f} USDT)")
                used_balance = await trading_bot.get_used_balance()
                if used_balance > value:
                    raise ValueError(
                        f"Задействовано {used_balance:.4f} USDT в активных ордерах, что превышает лимит {value:.2f} USDT"
                    )
        elif setting in ["taker_fee_percent", "maker_fee_percent"] and value < 0:
            raise ValueError("Комиссия не может быть отрицательной")

        settings[setting] = value
        save_state()
        logger.info(f"{setting}: {value}{' USDT' if setting in ['order_size', 'fixed_balance_limit'] else '%'}")

        setting_name = {
            "drop_percent": "Процент падения",
            "profit_percent": "Процент прибыли",
            "order_size": "Размер ордера",
            "fixed_balance_limit": "Лимит баланса",
            "taker_fee_percent": "Комиссия тейкера",
            "maker_fee_percent": "Комиссия мейкера"
        }.get(setting, setting)
        await update.message.reply_text(
            f"✅ {setting_name} установлен: {value}{' USDT' if setting in ['order_size', 'fixed_balance_limit'] else '%'}"
        )

        drop = settings["drop_percent"] if settings["drop_percent"] is not None else "не установлен"
        profit = settings["profit_percent"] if settings["profit_percent"] is not None else "не установлен"
        size = settings["order_size"] if settings["order_size"] is not None else "не установлен"
        balance_limit = settings["fixed_balance_limit"] if settings["fixed_balance_limit"] is not None else "не установлен"
        taker_fee = settings["taker_fee_percent"] if settings["taker_fee_percent"] is not None else "не установлен"
        maker_fee = settings["maker_fee_percent"] if settings["maker_fee_percent"] is not None else "не установлен"
        autobuy = "включена" if settings["autobuy_enabled"] else "выключена"

        new_message = (
            f"⚙️ *Настройки:*\n"
            f"📉 *Падение:* {drop}{' %' if isinstance(drop, (int, float)) else ''}\n"
            f"💰 *Прибыль:* {profit}{' %' if isinstance(profit, (int, float)) else ''}\n"
            f"💸 *Ордер:* {size}{' USDT' if isinstance(size, (int, float)) else ''}\n"
            f"💵 *Лимит баланса:* {balance_limit}{' USDT' if isinstance(balance_limit, (int, float)) else ''}\n"
            f"💳 *Комиссия тейкера:* {taker_fee}{' %' if isinstance(taker_fee, (int, float)) else ''}\n"
            f"💳 *Комиссия мейкера:* {maker_fee}{' %' if isinstance(maker_fee, (int, float)) else ''}\n"
            f"🤖 *Торговля:* {autobuy}"
        )

        keyboard = [
            [InlineKeyboardButton("📉 Установить % падения", callback_data="set_drop")],
            [InlineKeyboardButton("💰 Установить % прибыли", callback_data="set_profit")],
            [InlineKeyboardButton("💸 Установить размер ордера", callback_data="set_order")],
            [InlineKeyboardButton("💵 Установить лимит баланса", callback_data="limiter")],
            [InlineKeyboardButton("💳 Установить комиссии", callback_data="set_fees")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            if message_id:
                await context.bot.edit_message_text(
                    chat_id=update.message.chat_id,
                    message_id=message_id,
                    text=new_message,
                    parse_mode="Markdown",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(new_message, parse_mode="Markdown", reply_markup=reply_markup)
        except BadRequest as e:
            logger.warning(f"Ошибка редактирования сообщения: {str(e)}")
            await update.message.reply_text(new_message, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception as e:
            logger.error(f"Ошибка при попытке редактирования сообщения: {str(e)}")
            await update.message.reply_text(new_message, parse_mode="Markdown", reply_markup=reply_markup)

        context.user_data.clear()
    except ValueError as e:
        await update.message.reply_text(f"Ошибка: {str(e)}. Введите число, например, 2.0")
        logger.error(f"Ошибка валидации ввода: {str(e)}")
    except Exception as e:
        logger.error(f"Ошибка в set_setting: {str(e)}")
        await update.message.reply_text("Произошла ошибка. Попробуйте снова.")
        await show_main_menu(update, context, text="Выберите действие:")
        context.user_data.clear()

def setup_telegram_bot():
    """Настраивает Telegram-бота."""
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("autobuy", autobuy))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("price", price))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("orders", orders))
    application.add_handler(CommandHandler("limiter", limiter))
    application.add_handler(CommandHandler("fee_taker", fee_taker))
    application.add_handler(CommandHandler("fee_maker", fee_maker))
    application.add_handler(CallbackQueryHandler(orders_page_callback, pattern="^(orders_prev|orders_next)$"))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(set_drop|set_profit|set_order|limiter|set_fees|fee_taker|fee_maker)$"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, set_setting))

    return application