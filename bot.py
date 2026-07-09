cat > /opt/keitaro_deposit_bot/bot.py << 'PYEOF'
import json
import logging
import os
import time
from pathlib import Path

import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("deposit-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
KEITARO_URL = os.environ["KEITARO_URL"].rstrip("/")
KEITARO_API_KEY = os.environ["KEITARO_API_KEY"]
CHAT_ID = os.environ.get("CHAT_ID")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "90"))
CLICK_LOOKBACK_HOURS = int(os.environ.get("CLICK_LOOKBACK_HOURS", "720"))
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "20"))
SEEN_IDS_FILE = Path(os.environ.get("SEEN_IDS_FILE", "/opt/keitaro_deposit_bot/seen_ids.json"))
SEEN_IDS_MAX_AGE_HOURS = 48

BUYERS = {
    "44": "Bogdan",
    "45": "Aleksey",
    "46": "Alex",
    "47": "Maksim",
    "48": "Kostya",
    "49": "Sasha",
}

COLUMNS = [
    "sub_id_3",
    "sub_id_4",
    "sub_id",
    "sub_id_20",
    "country_code",
    "status",
    "offer",
    "sale_datetime",
    "revenue",
    "conversion_id",
]


def load_seen_ids() -> dict:
    if SEEN_IDS_FILE.exists():
        try:
            return json.loads(SEEN_IDS_FILE.read_text())
        except Exception:
            logger.exception("Не удалось прочитать файл seen_ids, начинаю с чистого листа")
    return {}


def save_seen_ids(seen: dict) -> None:
    SEEN_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_IDS_FILE.write_text(json.dumps(seen))


def prune_seen_ids(seen: dict) -> dict:
    cutoff = time.time() - SEEN_IDS_MAX_AGE_HOURS * 3600
    return {cid: ts for cid, ts in seen.items() if ts > cutoff}


def fetch_recent_deposits() -> list:
    now = time.time()

    click_from_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - CLICK_LOOKBACK_HOURS * 3600))
    click_to_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now + 60))

    page_size = 1000
    offset = 0
    rows = []

    while True:
        payload = {
            "range": {"from": click_from_dt, "to": click_to_dt, "timezone": "UTC"},
            "columns": COLUMNS,
            "filters": [
                {"name": "status", "operator": "EQUALS", "expression": "sale"},
            ],
            "sort": [{"name": "sale_datetime", "order": "desc"}],
            "limit": page_size,
            "offset": offset,
        }
        resp = requests.post(
            f"{KEITARO_URL}/admin_api/v1/conversions/log",
            headers={
                "Api-Key": KEITARO_API_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        page_rows = data.get("rows", [])
        rows.extend(page_rows)

        if len(page_rows) < page_size:
            break
        offset += page_size

        if offset > 50000:
            logger.warning("Достигнут предохранитель пагинации (50000 строк), останавливаю сбор")
            break

    sale_cutoff_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - LOOKBACK_MINUTES * 60))
    recent = [r for r in rows if (r.get("sale_datetime") or "") >= sale_cutoff_str]
    return recent


def format_message(row: dict) -> str:
    buyer_code = str(row.get("sub_id_20", "")).strip()
    buyer_name = BUYERS.get(buyer_code, f"ID{buyer_code}" if buyer_code else "Unknown")
    geo = (row.get("country_code") or "").lower()
    creo = row.get("sub_id_3") or "-"
    adset = row.get("sub_id_4") or "-"
    offer = row.get("offer") or "-"
    click_id = row.get("sub_id") or "-"
    revenue = row.get("revenue", 0)
    sale_time = row.get("sale_datetime") or "-"

    return (
        "💰 <b>DEPOSIT</b>\n"
        f"#{buyer_name}\n"
        "Status: dep\n"
        f"GEO: {geo}\n"
        f"Creo: {creo}\n"
        f"Adset: {adset}\n"
        f"Offer: {offer}\n"
        f"ClickID: {click_id}\n"
        f"Sum: {revenue}\n"
        f"Time: {sale_time}"
    )


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.data["chat_id"]
    if not chat_id:
        logger.warning("CHAT_ID не задан — уведомления некуда слать.")
        return

    try:
        rows = fetch_recent_deposits()
    except Exception:
        logger.exception("Не удалось получить данные из Keitaro API")
        return

    seen = load_seen_ids()
    new_rows = [r for r in rows if r.get("conversion_id") not in seen]

    for row in sorted(new_rows, key=lambda r: r.get("sale_datetime") or ""):
        cid = row.get("conversion_id")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=format_message(row),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("Не удалось отправить сообщение для conversion_id=%s", cid)
            continue
        seen[cid] = time.time()

    if new_rows:
        seen = prune_seen_ids(seen)
        save_seen_ids(seen)
        logger.info("Отправлено новых депозитов: %d", len(new_rows))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat ID: <code>{chat.id}</code>\n\n"
        "Добавьте это значение в переменную окружения CHAT_ID на сервере "
        "и перезапустите бота.",
        parse_mode="HTML",
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    seen = load_seen_ids()
    await update.message.reply_text(
        "Бот работает.\n"
        f"Запомненных депозитов: {len(seen)}\n"
        f"Опрос каждые {POLL_INTERVAL} сек.\n"
        f"Окно поиска депозитов (по sale_datetime): {LOOKBACK_MINUTES} мин.\n"
        f"Окно поиска кликов (по click_datetime): {CLICK_LOOKBACK_HOURS} ч."
    )


def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("status", status_cmd))

    application.job_queue.run_repeating(
        poll_job,
        interval=POLL_INTERVAL,
        first=5,
        data={"chat_id": CHAT_ID},
    )

    logger.info("Бот запущен, опрос каждые %s сек", POLL_INTERVAL)
    application.run_polling()


if __name__ == "__main__":
    main()
PYEOF
