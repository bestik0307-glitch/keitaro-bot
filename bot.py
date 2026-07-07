"""
Telegram-бот со статистикой Keitaro: меню с кнопками, статистика по каждому
человеку (по параметру sub_id_20), общий итог по команде, выбор периода
и календарь для выбора произвольной даты.
"""

import os
import json
import logging
import calendar
from datetime import date, timedelta

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from telegram import ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

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

# ---- Таблица с расходами (Google Sheets) ----
GOOGLE_SHEETS_CREDENTIALS_PATH = os.getenv(
    "GOOGLE_SHEETS_CREDENTIALS_PATH", "/opt/keitaro_bot/gsheet_credentials.json"
)
EXPENSE_SPREADSHEET_ID = os.getenv(
    "EXPENSE_SPREADSHEET_ID", "1_HtfLM1i_oh-utbFRwAHUFNL_mPho6-Hpcjt-sZ5J8Y"
)
EXPENSE_ROW_ORDER = list(PEOPLE.keys())

_sheets_service = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

MENU_MY_STATS = "📊 Моя стата"
MENU_TEAM = "👥 Команда"
MENU_TOTAL = "📋 Итого"
MENU_CALENDAR = "📅 Выбрать период"
CALENDAR_PICK = "📅 Своя дата"
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

MONTH_NAMES_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


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
        [CALENDAR_PICK],
        [BACK],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def build_calendar_markup(year: int, month: int) -> InlineKeyboardMarkup:
    keyboard = []

    keyboard.append([
        InlineKeyboardButton(
            f"{MONTH_NAMES_RU[month]} {year}", callback_data="cal:ignore"
        )
    ])

    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    keyboard.append([InlineKeyboardButton(d, callback_data="cal:ignore") for d in week_days])

    month_calendar = calendar.monthcalendar(year, month)
    today = date.today()

    for week in month_calendar:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="cal:ignore"))
            else:
                label = f"[{day}]" if date(year, month, day) == today else str(day)
                row.append(InlineKeyboardButton(
                    label, callback_data=f"cal:pick:{year}-{month:02d}-{day:02d}"
                ))
        keyboard.append(row)

    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    keyboard.append([
        InlineKeyboardButton("« Пред.", callback_data=f"cal:nav:{prev_year}-{prev_month:02d}"),
        InlineKeyboardButton("След. »", callback_data=f"cal:nav:{next_year}-{next_month:02d}"),
    ])

    return InlineKeyboardMarkup(keyboard)


def get_sheets_service():
    global _sheets_service
    if _sheets_service is None:
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SHEETS_CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        _sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return _sheets_service


def fetch_expense(sub_id_value, date_from: date, date_to: date) -> float:
    total = 0.0
    try:
        service = get_sheets_service()
    except Exception:
        logger.exception("Не удалось инициализировать Google Sheets API")
        return 0.0

    months = set()
    d = date_from
    while d <= date_to:
        months.add((d.year, d.month))
        d += timedelta(days=1)

    for year, month in months:
        tab_name = f"{MONTH_NAMES_RU[month]} {year}"
        try:
            result = service.spreadsheets().values().get(
                spreadsheetId=EXPENSE_SPREADSHEET_ID,
                range=f"'{tab_name}'!A1:AZ8",
                valueRenderOption="UNFORMATTED_VALUE",
            ).execute()
        except Exception:
            logger.exception("Не удалось прочитать вкладку %s в таблице расходов", tab_name)
            continue

        rows = result.get("values", [])
        if not rows:
            continue

        header = rows[0]
        date_col = {}
        for idx, cell in enumerate(header):
            if isinstance(cell, str) and cell.count(".") == 2:
                try:
                    dd, mm, yyyy = cell.split(".")
                    date_col[date(int(yyyy), int(mm), int(dd))] = idx
                except ValueError:
                    continue

        if sub_id_value is None:
            target_rows = list(range(1, 1 + len(EXPENSE_ROW_ORDER)))
        else:
            try:
                pos = EXPENSE_ROW_ORDER.index(sub_id_value)
                target_rows = [1 + pos]
            except ValueError:
                target_rows = []

        d2 = max(date_from, date(year, month, 1))
        last_day = calendar.monthrange(year, month)[1]
        month_end = min(date_to, date(year, month, last_day))
        while d2 <= month_end:
            col = date_col.get(d2)
            if col is not None:
                for r_idx in target_rows:
                    if r_idx < len(rows) and col < len(rows[r_idx]):
                        val = rows[r_idx][col]
                        if isinstance(val, (int, float)):
                            total += float(val)
                        elif isinstance(val, str):
                            try:
                                total += float(val.replace(",", "."))
                            except ValueError:
                                pass
            d2 += timedelta(days=1)

    return total


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


def format_stats(title: str, stats: dict, date_from: date, date_to: date, expense: float) -> str:
    def g(key, default=0):
        return stats.get(key, default)

    if date_from == date_to:
        period = date_from.strftime("%d.%m.%Y")
    else:
        period = f"{date_from.strftime('%d.%m.%Y')} - {date_to.strftime('%d.%m.%Y')}"

    try:
        revenue = float(g("revenue", 0) or 0)
    except (TypeError, ValueError):
        revenue = 0.0

    profit = revenue - expense

    return (
        f"📌 <b>{title}</b>\n"
        f"🗓 Период: {period}\n\n"
        f"👆 Клики: <b>{g('clicks')}</b>\n"
        f"🎯 Уникальные клики: <b>{g(UNIQUE_CLICKS_KEY)}</b>\n"
        f"🔁 Конверсии: <b>{g('conversions')}</b>\n"
        f"✅ Подтверждено: <b>{g(STATUS_METRICS['confirmed'])}</b>\n"
        f"❌ Отклонено: <b>{g(STATUS_METRICS['declined'])}</b>\n\n"
        f"💵 Доход (Keitaro): <b>${revenue:.2f}</b>\n"
        f"💸 Расход (Excel): <b>${expense:.2f}</b>\n"
        f"📈 Profit: <b>${profit:.2f}</b>"
    )


def safe_fetch_and_format(title: str, sub_id_value, date_from: date, date_to: date) -> str:
    try:
        stats = fetch_stats(sub_id_value, date_from, date_to)
    except requests.exceptions.HTTPError as e:
        logger.exception("Keitaro API HTTP error")
        return f"Ошибка при обращении к Keitaro API (код {e.response.status_code})."
    except Exception:
        logger.exception("Unexpected error while fetching Keitaro stats")
        return "Не удалось получить статистику. Попробуй ещё раз чуть позже."

    expense = fetch_expense(sub_id_value, date_from, date_to)
    return format_stats(title, stats, date_from, date_to, expense)


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

    if pending and text == CALENDAR_PICK:
        today = date.today()
        await update.message.reply_text(
            f"Выбери дату для «{pending['title']}»:",
            reply_markup=build_calendar_markup(today.year, today.month),
        )
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

    if text == MENU_CALENDAR:
        context.user_data.pop("pending", None)
        today = date.today()
        await update.message.reply_text(
            "Выбери дату — покажу статистику всей команды за этот день:",
            reply_markup=build_calendar_markup(today.year, today.month),
        )
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


async def handle_calendar_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data

    if data == "cal:ignore":
        await query.answer()
        return

    if data.startswith("cal:nav:"):
        year_month = data.split(":")[2]
        year, month = map(int, year_month.split("-"))
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_calendar_markup(year, month))
        return

    if data.startswith("cal:pick:"):
        date_str = data.split(":", 2)[2]
        picked_date = date.fromisoformat(date_str)
        await query.answer("Считаю статистику…")
        pending = context.user_data.get("pending")
        if pending:
            title, sub_id_value = pending["title"], pending["sub_id"]
        else:
            title, sub_id_value = "Итого (вся команда)", None
        message = safe_fetch_and_format(title, sub_id_value, picked_date, picked_date)
        await query.message.reply_html(message, reply_markup=main_menu_keyboard())
        context.user_data.pop("pending", None)
        return

    await query.answer()


def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_calendar_callback, pattern=r"^cal:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
