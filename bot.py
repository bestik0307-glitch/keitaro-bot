"""
Telegram-бот со статистикой Keitaro: меню с кнопками, статистика по каждому
человеку (по параметру sub_id_20), общий итог по команде и выбор периода.
"""

import os
import json
import logging
from datetime import date, timedelta

import requests
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8518822069:AAF6rqBc8pg47jf9o5enzMup8wxAOQY68Jw")
KEITARO_URL = os.getenv("KEITARO_URL", "https://lgmaxverd.sbs").strip().rstrip("/")
KEITARO_API_KEY = os.getenv("KEITARO_API_KEY", "cd02621ae03b3d9327efc05798cdd75b").strip()

SUB_ID_FIELD = "sub_id_20"

PEOPLE = {
    "44": "Bogdan",
    "45": "Aleksey",
    "46": "Alex",
    "47": "Maksim",
    "48": "Kostya",
    "49": "Sasha",
}

BASE_METRICS = ["clicks", "campaign_unique_clicks", "conversions", "cost", "revenue", "profit"]
STATUS_METRICS = {
    "confirmed": "sales",
    "declined": "rejected",
}
UNIQUE_CLICKS_KEY = "campaign_unique_clicks"

PROFILE_STORE_PATH = os.path.join(os.path.dirname(__file__), "user_profiles.json")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

MENU_MY_STATS = "📊 Моя стата"
MENU_TEAM = "👥 Команда"
MENU_TOTAL = "📋 Итого"
BACK = "⬅️ Назад"

PERIOD_TODAY = "Сегодня"
PERIOD_YESTERDAY = "Вчера"
PERIOD_7D = "7 дней"
PERIOD_30D = "30 дней"

PERIOD_RANGES = {
    PERIOD_TODAY: lambda: (date.today(), date.today()),
    PERIOD_YESTERDAY: lambda: (
        date.today() - timedelta(days=1),
        date.today() - timedelta(days=1),
    ),
    PERIOD_7D: lambda: (date.today() - timedelta(days=6), date.today()),
    PERIOD_30D: lambda: (date.today() - timedelta(days=29), date.today()),
}


def load_profiles() -> dict:
    if os.path.exists(PROFILE_STORE_PATH):
        with open(PROFILE_STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_profiles(profiles: dict) -> None:
    with open(PROFILE_STORE_PATH, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    rows = [[MENU_MY_STATS, MENU_TEAM], [MENU_TOTAL]]
    names = list(PEOPLE.values())
    for i in range(0, len(names), 2):
        rows.append(names[i:i + 2])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def people_list_keyboard() -> ReplyKeyboardMarkup:
    names = list(PEOPLE.values())
    rows = [names[i:i + 2] for i in range(0, len(names), 2)]
    rows.append([BACK])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def period_keyboard() -> ReplyKeyboardMarkup:
    rows = [
        [PERIOD_TODAY, PERIOD_YESTERDAY],
        [PERIOD_7D, PERIOD_30D],
        [BACK],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_payload(sub_id_value, date_from: date, date_to: date) -> dict:
    filters = []
    if sub_id_value is not None:
        filters.append({
            "name": SUB_ID_FIELD,
            "operator": "EQUALS",
            "expression": sub_id_value,
        })

    return {
        "range": {
            "timezone": "UTC",
            "from": date_from.strftime("%Y-%m-%d 00:00:00"),
            "to": date_to.strftime("%Y-%m-%d 23:59:59"),
        },
        "columns": [],
        "metrics": BASE_METRICS + list(STATUS_METRICS.values()),
        "grouping": [],
        "filters": filters,
        "limit": 1,
        "offset": 0,
    }


def fetch_stats(sub_id_value, date_from: date, date_to: date) -> dict:
    url = f"{KEITARO_URL}/admin_api/v1/report/build"
    headers = {
        "Api-Key": KEITARO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = build_payload(sub_id_value, date_from, date_to)
    response = requests.post(url, json=payload, headers=headers, timeout=20)
    if not response.ok:
        logger.error("Keitaro API error %s: %s", response.status_code, response.text)
    response.raise_for_status()
    data = response.json()
    rows = data.get("rows") or data.get("data") or []
    if rows:
        return rows[0]
    return data.get("summary") or data.get("totals") or {}


def format_stats(title: str, stats: dict, date_from: date, date_to: date) -> str:
    def g(key, default=0):
        return stats.get(key, default)

    if date_from == date_to:
        period = date_from.strftime("%d.%m.%Y")
    else:
        period = f"{date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}"

    return (
        f"📌 <b>{title}</b>\n"
        f"🗓 Период: {period}\n\n"
        f"👆 Клики: <b>{g('clicks')}</b>\n"
        f"🎯 Уникальные клики: <b>{g(UNIQUE_CLICKS_KEY)}</b>\n"
        f"🔁 Конверсии: <b>{g('conversions')}</b>\n"
        f"✅ Подтверждено: <b>{g(STATUS_METRICS['confirmed'])}</b>\n"
        f"❌ Отклонено: <b>{g(STATUS_METRICS['declined'])}</b>\n\n"
        f"💵 Revenue: <b>${g('revenue')}</b>\n"
        f"💸 Cost: <b>${g('cost')}</b>\n"
        f"📈 Profit: <b>${g('profit')}</b>"
    )


def safe_fetch_and_format(title: str, sub_id_value, date_from: date, date_to: date) -> str:
    try:
        stats = fetch_stats(sub_id_value, date_from, date_to)
        return format_stats(title, stats, date_from, date_to)
    except requests.exceptions.HTTPError as e:
        logger.exception("Keitaro API HTTP error")
        return f"Ошибка при обращении к Keitaro API (код {e.response.status_code})."
    except Exception:
        logger.exception("Unexpected error while fetching Keitaro stats")
        return "Не удалось получить статистику. Попробуй ещё раз чуть позже."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending", None)
    await update.message.reply_text(
        "Привет! Выбери, что показать:",
        reply_markup=main_menu_keyboard(),
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text
    user_id = str(update.effective_user.id)
    pending = context.user_data.get("pending")

    if pending and text in PERIOD_RANGES:
        date_from, date_to = PERIOD_RANGES[text]()
        message = safe_fetch_and_format(
            pending["title"], pending["sub_id"], date_from, date_to
        )
        await update.message.reply_html(message, reply_markup=main_menu_keyboard())
        context.user_data.pop("pending", None)
        return

    if text == BACK:
        context.user_data.pop("pending", None)
        await update.message.reply_text("Главное меню:", reply_markup=main_menu_keyboard())
        return

    if text == MENU_TEAM:
        await update.message.reply_text("Выбери человека:", reply_markup=people_list_keyboard())
        return

    if text == MENU_TOTAL:
        context.user_data["pending"] = {"sub_id": None, "title": "Итого (вся команда)"}
        await update.message.reply_text("Выбери период:", reply_markup=period_keyboard())
        return

    if text == MENU_MY_STATS:
        profiles = load_profiles()
        sub_id_value = profiles.get(user_id)
        if sub_id_value and sub_id_value in PEOPLE:
            context.user_data["pending"] = {
                "sub_id": sub_id_value,
                "title": f"Моя стата ({PEOPLE[sub_id_value]})",
            }
            await update.message.reply_text("Выбери период:", reply_markup=period_keyboard())
        else:
            await update.message.reply_text(
                "Сначала выбери, кто ты — нажми на своё имя в списке.",
                reply_markup=people_list_keyboard(),
            )
        return

    name_to_sub_id = {name: sub_id for sub_id, name in PEOPLE.items()}
    if text in name_to_sub_id:
        sub_id_value = name_to_sub_id[text]
        profiles = load_profiles()
        profiles[user_id] = sub_id_value
        save_profiles(profiles)
        context.user_data["pending"] = {"sub_id": sub_id_value, "title": text}
        await update.message.reply_text("Выбери период:", reply_markup=period_keyboard())
        return

    await update.message.reply_text("Не понял команду. Выбери пункт меню:", reply_markup=main_menu_keyboard())


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
