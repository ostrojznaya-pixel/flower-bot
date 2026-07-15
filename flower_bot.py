# -*- coding: utf-8 -*-
"""
Бот-нагадувач для поливу квітів (кав'ярня/книгарня і т.п.)

Функції:
1. Нагадування "полити квіти в закладі" — вт, чт, нд о 10:00
2. Нагадування "полив квітів на вулиці" — щодня о 10:30
3. Кнопка "Відмітити полив" -> вибір відповідального (книгарка/адмін)
4. Щотижневе зведення (неділя, 20:00) — хто і коли поливав

Встановлення залежностей:
    pip install python-telegram-bot==21.4 apscheduler pytz

Запуск:
    python flower_bot.py

Перед запуском:
1. Отримайте токен бота у @BotFather в Telegram
2. Вставте токен у змінну BOT_TOKEN нижче
3. Додайте бота у потрібну групу/чат
4. Надішліть у цей чат команду /register — бот запам'ятає chat_id
   і надсилатиме туди всі нагадування
5. Відредагуйте список RESPONSIBLE_PEOPLE під реальних співробітників
"""

import os
import sqlite3
import logging
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ========================= НАСТРОЙКИ =========================

# Токен береться зі змінної середовища BOT_TOKEN (налаштовується в Render).
# Якщо запускаєте локально — можна тимчасово підставити рядок напряму.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬТЕ_СЮДА_ТОКЕН_ОТ_BOTFATHER")

TIMEZONE = pytz.timezone("Europe/Kyiv")

# Список ответственных — отредактируйте под реальных людей.
# Можно смешивать книгарок и админов, бот просто покажет весь список.
RESPONSIBLE_PEOPLE = [
    "Аня (книгарка)",
    "Оля (книгарка)",
    "Марина (адмін)",
    "Настя (адмін)",
]

DB_PATH = "flowers.db"

# Время напоминаний (можно поменять под себя)
INDOOR_DAYS = "tue,thu,sun"   # квіти в закладі
INDOOR_HOUR, INDOOR_MINUTE = 10, 0
OUTDOOR_HOUR, OUTDOOR_MINUTE = 10, 30
SUMMARY_HOUR, SUMMARY_MINUTE = 20, 0   # воскресная сводка вечером

# На скільки хвилин переносити нагадування при натисканні "Перенести"
POSTPONE_MINUTES = 30

# Через скільки хвилин після нагадування слати ескалацію, якщо ніхто не відповів
ESCALATION_MINUTES = 60

# Кого згадувати в ескалації (телеграм-юзернейми через @, без пробілів)
# Наприклад: ["@marina_admin", "@nastya_admin"]
ESCALATION_CONTACTS = ["@admin1", "@admin2"]

# ========================= БАЗА ДАННЫХ =========================

def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS watering_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watering_type TEXT,     -- 'indoor' або 'outdoor'
            person TEXT,            -- NULL, якщо пропущено
            status TEXT,            -- 'done', 'skipped' або 'postponed'
            date TEXT,
            time TEXT
        )
    """)
    conn.commit()
    conn.close()


def db_register_chat(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()


def db_get_chats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM chats")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def db_log_watering(watering_type: str, person: str = None, status: str = "done"):
    now = datetime.now(TIMEZONE)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO watering_log (watering_type, person, status, date, time) VALUES (?, ?, ?, ?, ?)",
        (watering_type, person, status, now.strftime("%Y-%m-%d"), now.strftime("%H:%M")),
    )
    conn.commit()
    conn.close()


def db_get_week_log():
    """Повертає записи за останні 7 днів."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT watering_type, person, status, date, time
        FROM watering_log
        WHERE date >= date('now', '-7 days')
        ORDER BY date, time
    """)
    rows = cur.fetchall()
    conn.close()
    return rows


def db_has_response_today(watering_type: str) -> bool:
    """Перевіряє, чи була хоч якась реакція (полито/пропущено/перенесено) сьогодні."""
    today = datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM watering_log WHERE watering_type = ? AND date = ?",
        (watering_type, today),
    )
    count = cur.fetchone()[0]
    conn.close()
    return count > 0

# ========================= КЛАВИАТУРЫ =========================

TYPE_LABELS = {
    "indoor": "🌸 Квіти в закладі",
    "outdoor": "🌿 Квіти на вулиці",
}


def build_mark_keyboard(watering_type: str):
    keyboard = [
        [InlineKeyboardButton("✅ Відмітити полив", callback_data=f"pick:{watering_type}")],
        [
            InlineKeyboardButton(f"⏰ Перенести на {POSTPONE_MINUTES} хв", callback_data=f"postpone:{watering_type}"),
            InlineKeyboardButton("❌ Пропустити сьогодні", callback_data=f"skip:{watering_type}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_people_keyboard(watering_type: str):
    keyboard = []
    for person in RESPONSIBLE_PEOPLE:
        keyboard.append([InlineKeyboardButton(person, callback_data=f"log:{watering_type}:{person}")])
    keyboard.append([InlineKeyboardButton("« Назад", callback_data=f"back:{watering_type}")])
    return InlineKeyboardMarkup(keyboard)

# ========================= КОМАНДЫ =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт! Я стежу за поливом квітів 🌱\n"
        "Надішли /register у чаті, куди потрібно надсилати нагадування."
    )


async def cmd_register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db_register_chat(chat_id)
    await update.message.reply_text("Цей чат зареєстровано для нагадувань про полив ✅")


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = build_summary_text()
    await update.message.reply_text(text)

# ========================= КНОПКИ =========================

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("pick:"):
        watering_type = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=build_people_keyboard(watering_type))

    elif data.startswith("back:"):
        watering_type = data.split(":", 1)[1]
        await query.edit_message_reply_markup(reply_markup=build_mark_keyboard(watering_type))

    elif data.startswith("log:"):
        _, watering_type, person = data.split(":", 2)
        db_log_watering(watering_type, person=person, status="done")
        now = datetime.now(TIMEZONE)
        label = TYPE_LABELS.get(watering_type, watering_type)
        await query.edit_message_text(
            f"{label}\n✅ Полив виконано: {person}\n🕒 {now.strftime('%d.%m %H:%M')}"
        )

    elif data.startswith("postpone:"):
        watering_type = data.split(":", 1)[1]
        chat_id = query.message.chat_id
        label = TYPE_LABELS.get(watering_type, watering_type)
        run_time = datetime.now(TIMEZONE) + timedelta(minutes=POSTPONE_MINUTES)

        db_log_watering(watering_type, person=None, status="postponed")

        scheduler = context.application.bot_data["scheduler"]
        scheduler.add_job(
            send_single_reminder,
            "date",
            run_date=run_time,
            args=[context.application, chat_id, watering_type],
        )

        await query.edit_message_text(
            f"{label}\n⏰ Перенесено на {POSTPONE_MINUTES} хв "
            f"(нагадаю о {run_time.strftime('%H:%M')})"
        )

    elif data.startswith("skip:"):
        watering_type = data.split(":", 1)[1]
        db_log_watering(watering_type, person=None, status="skipped")
        label = TYPE_LABELS.get(watering_type, watering_type)
        await query.edit_message_text(f"{label}\n❌ Полив пропущено сьогодні")

# ========================= ПЛАНИРОВЩИК =========================

async def send_single_reminder(app: Application, chat_id: int, watering_type: str):
    """Надсилає нагадування в один конкретний чат (використовується для 'Перенести')."""
    texts = {
        "indoor": "🌸 Нагадування: час полити квіти в закладі!",
        "outdoor": "🌿 Нагадування: полив квітів на вулиці!",
    }
    await app.bot.send_message(
        chat_id=chat_id,
        text=texts.get(watering_type, "🌱 Нагадування про полив!"),
        reply_markup=build_mark_keyboard(watering_type),
    )
    schedule_escalation(app, chat_id, watering_type)


def schedule_escalation(app: Application, chat_id: int, watering_type: str):
    """Ставить перевірку через ESCALATION_MINUTES: чи відповів хтось на нагадування."""
    scheduler = app.bot_data["scheduler"]
    run_time = datetime.now(TIMEZONE) + timedelta(minutes=ESCALATION_MINUTES)
    scheduler.add_job(
        check_escalation,
        "date",
        run_date=run_time,
        args=[app, chat_id, watering_type],
    )


async def check_escalation(app: Application, chat_id: int, watering_type: str):
    """Якщо ніхто не відповів за ESCALATION_MINUTES — шле тривожне нагадування адмінам."""
    if db_has_response_today(watering_type):
        return  # хтось вже відреагував — ескалація не потрібна

    label = TYPE_LABELS.get(watering_type, watering_type)
    mentions = " ".join(ESCALATION_CONTACTS)
    text = (
        f"⚠️ УВАГА: {label}\n"
        f"Ніхто не відповів на нагадування вже {ESCALATION_MINUTES} хв!\n"
        f"{mentions}"
    )
    await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=build_mark_keyboard(watering_type))

async def send_indoor_reminder(app: Application):
    for chat_id in db_get_chats():
        await app.bot.send_message(
            chat_id=chat_id,
            text="🌸 Нагадування: час полити квіти в закладі!",
            reply_markup=build_mark_keyboard("indoor"),
        )
        schedule_escalation(app, chat_id, "indoor")


async def send_outdoor_reminder(app: Application):
    for chat_id in db_get_chats():
        await app.bot.send_message(
            chat_id=chat_id,
            text="🌿 Нагадування: полив квітів на вулиці!",
            reply_markup=build_mark_keyboard("outdoor"),
        )
        schedule_escalation(app, chat_id, "outdoor")


def build_summary_text():
    rows = db_get_week_log()
    if not rows:
        return "📊 За цей тиждень ще немає відміток про полив."

    lines = ["📊 Зведення поливу за тиждень:\n"]
    for watering_type, person, status, date, time in rows:
        label = TYPE_LABELS.get(watering_type, watering_type)
        if status == "skipped":
            lines.append(f"{date} {time} — {label} — ❌ пропущено")
        else:
            lines.append(f"{date} {time} — {label} — ✅ {person}")
    return "\n".join(lines)


async def send_weekly_summary(app: Application):
    text = build_summary_text()
    for chat_id in db_get_chats():
        await app.bot.send_message(chat_id=chat_id, text=text)


def setup_scheduler(app: Application):
    scheduler = AsyncIOScheduler(timezone=TIMEZONE)

    scheduler.add_job(
        send_indoor_reminder, CronTrigger(day_of_week=INDOOR_DAYS, hour=INDOOR_HOUR, minute=INDOOR_MINUTE),
        args=[app],
    )
    scheduler.add_job(
        send_outdoor_reminder, CronTrigger(hour=OUTDOOR_HOUR, minute=OUTDOOR_MINUTE),
        args=[app],
    )
    scheduler.add_job(
        send_weekly_summary, CronTrigger(day_of_week="sun", hour=SUMMARY_HOUR, minute=SUMMARY_MINUTE),
        args=[app],
    )

    scheduler.start()
    app.bot_data["scheduler"] = scheduler

# ========================= ЗАПУСК =========================

def main():
    logging.basicConfig(level=logging.INFO)

    if not BOT_TOKEN or BOT_TOKEN.startswith("ВСТАВЬТЕ"):
        raise RuntimeError(
            "Не заданий BOT_TOKEN. Встановіть змінну середовища BOT_TOKEN "
            "або вкажіть токен напряму у файлі для локального тесту."
        )

    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("register", cmd_register))
    app.add_handler(CommandHandler("summary", cmd_summary))
    app.add_handler(CallbackQueryHandler(on_callback))

    setup_scheduler(app)

    app.run_polling()


if __name__ == "__main__":
    main()
