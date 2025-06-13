# config.py
import os
import json
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
STATE_FILE = "state.json"

DEFAULT_SETTINGS = {
    "drop_percent": None,
    "profit_percent": None,
    "order_size": None,
    "autobuy_enabled": False,
    "total_profit": "0.0",
    "fixed_balance_limit": None,
    "taker_fee_percent": 0.05,  # Комиссия тейкера по умолчанию (0.05%)
    "maker_fee_percent": 0.0    # Комиссия мейкера по умолчанию (0.00%)
}

settings = DEFAULT_SETTINGS.copy()

def load_settings():
    """Загружает настройки из state.json или создает новый файл с настройками по умолчанию."""
    global settings
    try:
        if not os.path.exists(STATE_FILE) or os.path.getsize(STATE_FILE) == 0:
            logger.warning("Файл state.json не найден или пуст, создаётся новый")
            settings = DEFAULT_SETTINGS.copy()
            save_state()
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        loaded_settings = state.get("settings", {})
        settings = DEFAULT_SETTINGS.copy()
        for key in DEFAULT_SETTINGS:
            settings[key] = loaded_settings.get(key, DEFAULT_SETTINGS[key])
        logger.info(f"Настройки загружены: {settings}")
    except json.JSONDecodeError as e:
        logger.error(f"Ошибка декодирования state.json: {str(e)}. Создаётся новый файл")
        settings = DEFAULT_SETTINGS.copy()
        save_state()
    except Exception as e:
        logger.error(f"Ошибка загрузки настроек: {str(e)}. Создаётся новый файл")
        settings = DEFAULT_SETTINGS.copy()
        save_state()

def save_state():
    """Сохраняет настройки и chat_id в state.json."""
    global settings
    try:
        state = {}
        if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except json.JSONDecodeError:
                logger.warning("Некорректный JSON в state.json, создаётся новый")
        state["settings"] = settings
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4, ensure_ascii=False)
        logger.info(f"Настройки сохранены: {settings}")
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек: {str(e)}")

async def send_notification(application, message):
    """Отправляет уведомление в Telegram."""
    try:
        state = {}
        if os.path.exists(STATE_FILE) and os.path.getsize(STATE_FILE) > 0:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        chat_id = state.get("chat_id")
        if chat_id:
            await application.bot.send_message(chat_id=chat_id, text=message)
            logger.info(f"Уведомление: {message}")
        else:
            logger.warning("chat_id не установлен")
    except Exception as e:
        logger.warning(f"Ошибка отправки уведомления: {str(e)}")

load_settings()