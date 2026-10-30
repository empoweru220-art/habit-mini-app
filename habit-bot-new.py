import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ============================================================
# НАСТРОЙКИ
# ============================================================

TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).resolve().parent
WEB_APP_URL = os.getenv("WEB_APP_URL", "").strip()
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "habits.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

PORT = int(os.getenv("PORT", "8000"))
ALLOWED_DURATIONS = {7, 14, 21, 30}
REMINDER_SCHEDULES = {
    1: ["20:00"],
    2: ["09:00", "20:00"],
    3: ["09:00", "14:30", "20:00"],
    4: ["09:00", "12:40", "16:20", "20:00"],
    5: ["09:00", "11:45", "14:30", "17:15", "20:00"],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("habit_lab")

if not TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в переменных окружения.")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db():
    with connect_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_habits (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            habit_name TEXT,
            day_count INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS programs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            habit_name TEXT NOT NULL,
            duration_days INTEGER NOT NULL DEFAULT 7,
            start_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_programs_user_status
        ON programs(user_id, status);

        CREATE TABLE IF NOT EXISTS daily_checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            program_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            checkin_date TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(program_id, checkin_date),
            FOREIGN KEY(program_id) REFERENCES programs(id)
        );

        CREATE TABLE IF NOT EXISTS mentor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reminder_settings (
            user_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            reminders_per_day INTEGER NOT NULL DEFAULT 1,
            timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sent_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            reminder_date TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(user_id, reminder_date, reminder_time)
        );
        """)

        # Миграция старых активных привычек. Выполняется только,
        # если у пользователя ещё нет активной программы.
        old_users = conn.execute("""
            SELECT user_id, username, habit_name, day_count, status
            FROM user_habits
        """).fetchall()

        for row in old_users:
            if row["status"] != "active" or not row["habit_name"]:
                continue

            exists = conn.execute("""
                SELECT 1 FROM programs
                WHERE user_id = ? AND status = 'active'
                LIMIT 1
            """, (row["user_id"],)).fetchone()

            if exists:
                continue

            day_count = max(1, int(row["day_count"] or 1))
            start_date = date.today() - timedelta(days=day_count - 1)
            now = datetime.now().isoformat(timespec="seconds")

            conn.execute("""
                INSERT INTO programs
                (user_id, habit_name, duration_days, start_date, status, created_at)
                VALUES (?, ?, 7, ?, 'active', ?)
            """, (
                row["user_id"], row["habit_name"], start_date.isoformat(), now
            ))

        conn.commit()

    logger.info("База данных готова: %s", DB_PATH)


def get_active_program(user_id, program_id=None):
    """Возвращает выбранную программу; без ID — самую новую активную."""
    with connect_db() as conn:
        if program_id is not None:
            return conn.execute("""
                SELECT * FROM programs
                WHERE user_id = ? AND id = ? AND status = 'active'
            """, (user_id, int(program_id))).fetchone()
        return conn.execute("""
            SELECT * FROM programs
            WHERE user_id = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()


def get_active_programs(user_id):
    with connect_db() as conn:
        return conn.execute("""
            SELECT * FROM programs
            WHERE user_id = ? AND status = 'active'
            ORDER BY id DESC
        """, (user_id,)).fetchall()

def get_program_progress(program):
    today = date.today()
    start = date.fromisoformat(program["start_date"])
    day_number = max(1, (today - start).days + 1)
    duration = int(program["duration_days"])

    with connect_db() as conn:
        completed = conn.execute("""
            SELECT COUNT(*) AS total FROM daily_checkins
            WHERE program_id = ?
        """, (program["id"],)).fetchone()["total"]

    return day_number, completed, round(min(completed, duration) / duration * 100)


def save_habit(user_id, username, habit_name, duration_days=7):
    """Добавляет отдельную активную программу, не завершая другие."""
    habit_name = habit_name.strip()[:100]
    if not habit_name:
        raise ValueError("Название привычки пустое.")
    if duration_days not in ALLOWED_DURATIONS:
        raise ValueError("Недопустимая длительность программы.")

    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()
    with connect_db() as conn:
        existing = conn.execute("""
            SELECT id FROM programs
            WHERE user_id = ? AND habit_name = ? AND status = 'active'
            LIMIT 1
        """, (user_id, habit_name)).fetchone()
        if existing:
            # Повторный выбор той же привычки не создаёт дубликат.
            conn.execute("""
                UPDATE user_habits SET username = ?, habit_name = ?, status = 'active'
                WHERE user_id = ?
            """, (username or "", habit_name, user_id))
            conn.commit()
            return int(existing["id"])

        cur = conn.execute("""
            INSERT INTO programs
            (user_id, habit_name, duration_days, start_date, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
        """, (user_id, habit_name, duration_days, today, now))
        conn.execute("""
            INSERT INTO user_habits (user_id, username, habit_name, day_count, status)
            VALUES (?, ?, ?, 1, 'active')
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                habit_name = excluded.habit_name,
                day_count = 1,
                status = 'active'
        """, (user_id, username or "", habit_name))
        conn.commit()
        return int(cur.lastrowid)

def reset_habit(user_id, program_id=None):
    """Завершает одну программу по ID или все активные, если ID не указан."""
    now = datetime.now().isoformat(timespec="seconds")
    with connect_db() as conn:
        if program_id is None:
            conn.execute("""
                UPDATE programs SET status = 'ended', ended_at = ?
                WHERE user_id = ? AND status = 'active'
            """, (now, user_id))
        else:
            conn.execute("""
                UPDATE programs SET status = 'ended', ended_at = ?
                WHERE user_id = ? AND id = ? AND status = 'active'
            """, (now, user_id, int(program_id)))
        remaining = conn.execute("""
            SELECT habit_name FROM programs
            WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()
        if remaining:
            conn.execute("""
                UPDATE user_habits SET habit_name = ?, status = 'active'
                WHERE user_id = ?
            """, (remaining["habit_name"], user_id))
        else:
            conn.execute("UPDATE user_habits SET status = 'inactive' WHERE user_id = ?", (user_id,))
        conn.commit()

def check_in(user_id, note="", program_id=None):
    program = get_active_program(user_id, program_id)
    if not program:
        return False, "Активной программы нет. Выбери привычку в Mini App."

    today = date.today()
    start = date.fromisoformat(program["start_date"])
    day_number = (today - start).days + 1
    duration = int(program["duration_days"])
    if day_number < 1:
        return False, "Дата начала программы некорректна."
    if day_number > duration:
        now = datetime.now().isoformat(timespec="seconds")
        with connect_db() as conn:
            conn.execute("UPDATE programs SET status = 'completed', ended_at = ? WHERE id = ?", (now, program["id"]))
            remaining = conn.execute("SELECT habit_name FROM programs WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
            conn.execute("UPDATE user_habits SET status = ?, habit_name = COALESCE(?, habit_name) WHERE user_id = ?", ("active" if remaining else "completed", remaining["habit_name"] if remaining else None, user_id))
            conn.commit()
        return False, "Срок программы завершён. Можно продолжить другие программы или начать новую."

    try:
        with connect_db() as conn:
            conn.execute("""INSERT INTO daily_checkins
                (program_id, user_id, checkin_date, note, created_at)
                VALUES (?, ?, ?, ?, ?)""", (program["id"], user_id, today.isoformat(), note[:500], datetime.now().isoformat(timespec="seconds")))
            conn.execute("UPDATE user_habits SET day_count = ? WHERE user_id = ?", (day_number, user_id))
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "Сегодня отметка уже сделана для этой программы."

    _, completed, percentage = get_program_progress(program)
    return True, (f"Отметка за день {day_number} сохранена.\nПривычка: {program['habit_name']}\nОтмечено дней: {completed} из {duration} ({percentage}%).")

# ============================================================
# НАПОМИНАНИЯ
# ============================================================

def valid_timezone(name):
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return "Europe/Warsaw"


def get_reminder_settings(user_id):
    with connect_db() as conn:
        row = conn.execute("""
            SELECT * FROM reminder_settings WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return {
            "enabled": True,
            "reminders_per_day": 1,
            "timezone": "Europe/Warsaw",
        }

    return {
        "enabled": bool(row["enabled"]),
        "reminders_per_day": int(row["reminders_per_day"]),
        "timezone": row["timezone"],
    }


def save_reminder_settings(user_id, enabled, count, timezone_name):
    count = max(1, min(5, int(count)))
    timezone_name = valid_timezone(timezone_name)
    now = datetime.now().isoformat(timespec="seconds")

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO reminder_settings
            (user_id, enabled, reminders_per_day, timezone, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                enabled = excluded.enabled,
                reminders_per_day = excluded.reminders_per_day,
                timezone = excluded.timezone,
                updated_at = excluded.updated_at
        """, (user_id, int(enabled), count, timezone_name, now))
        conn.commit()

    return get_reminder_settings(user_id)


async def reminder_scheduler():
    """Отправляет одно напоминание со списком всех активных программ пользователя."""
    while True:
        try:
            with connect_db() as conn:
                users = conn.execute("""
                    SELECT user_id, enabled, reminders_per_day, timezone
                    FROM reminder_settings WHERE enabled = 1
                """).fetchall()

            now_utc = datetime.now().astimezone()
            for settings in users:
                tz_name = valid_timezone(settings["timezone"])
                local_now = now_utc.astimezone(ZoneInfo(tz_name))
                local_time = local_now.strftime("%H:%M")
                local_date = local_now.date().isoformat()
                times = REMINDER_SCHEDULES.get(int(settings["reminders_per_day"]), ["20:00"])
                if local_time not in times:
                    continue

                with connect_db() as conn:
                    already_sent = conn.execute("""
                        SELECT 1 FROM sent_reminders
                        WHERE user_id = ? AND reminder_date = ? AND reminder_time = ?
                    """, (settings["user_id"], local_date, local_time)).fetchone()
                    if already_sent:
                        continue
                    programs = conn.execute("""
                        SELECT id, habit_name, duration_days, start_date
                        FROM programs WHERE user_id = ? AND status = 'active'
                        ORDER BY id DESC
                    """, (settings["user_id"],)).fetchall()
                    if not programs:
                        continue
                    # Уникальный ключ — пользователь + локальная дата + время:
                    # одному человеку отправляется одно сообщение со всеми привычками.
                    conn.execute("""INSERT INTO sent_reminders
                        (user_id, reminder_date, reminder_time, sent_at)
                        VALUES (?, ?, ?, ?)""", (settings["user_id"], local_date, local_time, datetime.now().isoformat(timespec="seconds")))
                    conn.commit()

                lines = ["⏰ Напоминание из «Лаборатории привычек»", ""]
                active_lines = []
                for program in programs:
                    day = (local_now.date() - date.fromisoformat(program["start_date"])).days + 1
                    duration = int(program["duration_days"])
                    if 1 <= day <= duration:
                        active_lines.append(f"• {program['habit_name']} — день {day} из {duration}")
                if not active_lines:
                    with connect_db() as conn:
                        conn.execute("DELETE FROM sent_reminders WHERE user_id = ? AND reminder_date = ? AND reminder_time = ?", (settings["user_id"], local_date, local_time))
                        conn.commit()
                    continue
                lines.extend(active_lines)
                lines.extend(["", "Открой Mini App и отметь выполненные привычки."])
                try:
                    await bot.send_message(settings["user_id"], "\n".join(lines), reply_markup=main_keyboard(True))
                except Exception:
                    logger.exception("Не удалось отправить напоминание пользователю %s", settings["user_id"])
                    with connect_db() as conn:
                        conn.execute("DELETE FROM sent_reminders WHERE user_id = ? AND reminder_date = ? AND reminder_time = ?", (settings["user_id"], local_date, local_time))
                        conn.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка планировщика напоминаний.")
        await asyncio.sleep(20)

# ============================================================
# НАСТАВНИК
# ============================================================

# Локальная база советов наставника: совет выбирается по конкретной программе и дню.
MENTOR_ADVICE = {
    "💧 Водный баланс (2л)": [
        "Распредели воду равномерно в течение дня. Не пытайся выпить весь объём за один раз.",
        "Поставь стакан воды на видное место. Видимая подсказка помогает вспомнить о привычке.",
        "Свяжи воду с уже привычными действиями: после пробуждения и во время перерывов.",
        "Держи бутылку рядом, когда работаешь. Пей регулярно, ориентируясь также на жажду.",
        "Проверь, удобно ли тебе пить в течение дня. Лучше небольшие порции, чем попытка наверстать вечером.",
        "Подготовь воду заранее перед работой или выходом из дома — так меньше поводов забыть.",
        "Оцени, что помогало вспоминать о воде на этой неделе, и оставь самый удобный способ.",
    ],
    "📵 Цифровой детокс": [
        "Убери телефон подальше на 20 минут и займись одним делом без переключений.",
        "Отключи необязательные уведомления хотя бы на один рабочий блок.",
        "Перед тем как открыть приложение, остановись и спроси себя, зачем ты туда заходишь.",
        "Выбери одно время для проверки сообщений вместо постоянных коротких проверок.",
        "Во время еды или разговора положи телефон экраном вниз и вне досягаемости.",
        "Убери самые отвлекающие приложения с главного экрана, чтобы открыть их было не так автоматически.",
        "Заметь, в какие моменты чаще всего тянешься к телефону, и придумай замену на эти 5 минут.",
    ],
    "🌙 Ранний сон (до 23:00)": [
        "Начни вечернее замедление заранее: приглуши свет и подготовь вещи на завтра.",
        "Выбери постоянный сигнал к завершению дня — например, будильник на подготовку ко сну.",
        "За 30 минут до сна отложи рабочие дела и по возможности убери телефон от кровати.",
        "Если трудно лечь рано, сдвигай время отхода ко сну постепенно, небольшими шагами.",
        "Подготовь спальню заранее: проветри комнату и убери лишние дела из поля зрения.",
        "Сохраняй спокойный повторяющийся ритуал перед сном, даже если день был загруженным.",
        "Подумай, что больше всего мешало лечь вовремя, и измени сегодня только один фактор.",
    ],
    "📖 Чтение (20 минут)": [
        "Положи книгу на видное место и выдели 20 минут без телефона.",
        "Начни с небольшого отрезка: несколько страниц достаточно, чтобы войти в чтение.",
        "Привяжи чтение к стабильному моменту дня — например, после завтрака или перед сном.",
        "Если отвлекаешься, убери телефон из комнаты и вернись к следующему абзацу без самокритики.",
        "Заранее выбери, что будешь читать сегодня, чтобы не тратить время на поиск.",
        "После чтения запиши одну мысль или идею, которая запомнилась.",
        "Сделай короткий обзор недели: когда читать было легче всего и как повторить это завтра.",
    ],
    "🎯 Фокус / Спорт": [
        "Выбери одно конкретное дело или короткую тренировку. Сформулируй, что будет считаться завершением.",
        "Разбей задачу на первый шаг, который можно выполнить за пять минут.",
        "Убери один главный источник отвлечения и поработай коротким непрерывным блоком.",
        "Если цель — движение, подготовь одежду или инвентарь заранее, чтобы снизить порог начала.",
        "Сделай небольшой перерыв после завершённого блока, а затем реши, нужен ли следующий.",
        "Не пытайся компенсировать пропущенную тренировку чрезмерной нагрузкой — вернись к посильному шагу.",
        "Отметь, какое время и формат лучше всего сработали, и используй это наблюдение на следующей неделе.",
    ],
}

def advice_for_program(program):
    habit_name = str(program["habit_name"])
    advice_list = MENTOR_ADVICE.get(habit_name)
    if not advice_list:
        # Безопасный тематический fallback для программ, которых ещё нет в локальной базе.
        advice_list = [
            f"По привычке «{habit_name}»: выбери небольшой конкретный шаг на сегодня и привяжи его к понятному моменту дня.",
            f"Для программы «{habit_name}» уменьши действие до простого выполнимого шага, который реально повторить сегодня.",
            f"По программе «{habit_name}» заранее подготовь всё необходимое и вечером оцени, что получилось.",
        ]
    day_number, _, _ = get_program_progress(program)
    index = (max(1, min(day_number, int(program["duration_days"]))) - 1) % len(advice_list)
    return advice_list[index]


async def ask_mentor(user_id, user_message="", program_id=None):
    """Возвращает локальный совет именно для выбранной активной программы."""
    program = get_active_program(user_id, program_id)
    if not program:
        return "Сначала открой активную программу в Mini App.", False

    answer = advice_for_program(program)
    with connect_db() as conn:
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("""
            INSERT INTO mentor_history (user_id, role, message, created_at)
            VALUES (?, 'assistant', ?, ?)
        """, (user_id, answer[:5000], now))
        conn.commit()
    return answer, False


# ============================================================
# TELEGRAM-БОТ
# ============================================================

def main_keyboard(has_program=False):
    builder = InlineKeyboardBuilder()
    if WEB_APP_URL:
        builder.button(text="🧪 Открыть Лабораторию", web_app=types.WebAppInfo(url=WEB_APP_URL))
    if has_program:
        builder.button(text="📊 Мои программы", callback_data="progress")
        builder.button(text="✅ Отметить сегодня", callback_data="checkin")
        builder.button(text="🧠 Спросить наставника", callback_data="mentor")
        builder.button(text="🏠 В главное меню", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()

def progress_text(program):
    day, completed, percentage = get_program_progress(program)
    duration = int(program["duration_days"])
    state = (
        f"Текущий календарный день: {day} из {duration}."
        if day <= duration else "Срок программы истёк."
    )
    return (
        f"📊 Прогресс программы\n\n"
        f"🎯 Привычка: {program['habit_name']}\n"
        f"📅 {state}\n"
        f"✅ Отмечено дней: {completed} из {duration}\n"
        f"📈 Выполнение по отметкам: {percentage}%"
    )


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    programs = get_active_programs(message.from_user.id)
    program = programs[0] if programs else None
    if program:
        text = (
            f"С возвращением в «Лабораторию привычек»!\n"
            f"Активная привычка: {program['habit_name']}.\n"
            f"Длительность: {program['duration_days']} дней."
        )
    else:
        text = (
            "Добро пожаловать в «Лабораторию привычек»!\n"
            "Открой Mini App и выбери привычку."
        )
    await message.answer(text, reply_markup=main_keyboard(bool(programs)))


@dp.message(Command("progress"))
async def cmd_progress(message: types.Message):
    programs = get_active_programs(message.from_user.id)
    await message.answer(progress_list_text(programs) if programs else "Активных программ нет. Открой Mini App.")


def progress_list_text(programs):
    if not programs:
        return "Активных программ пока нет. Открой Mini App и выбери привычку."
    parts = [f"📊 Мои активные программы: {len(programs)}\n"]
    for i, program in enumerate(programs, 1):
        day, completed, percentage = get_program_progress(program)
        duration = int(program["duration_days"])
        day_label = f"день {min(day, duration)} из {duration}" if day <= duration else "срок истёк"
        parts.append(f"{i}. 🎯 {program['habit_name']}\n   📅 {day_label} · отметок {completed}/{duration} · {percentage}%")
    return "\n\n".join(parts)


@dp.message(Command("checkin"))
async def cmd_checkin(message: types.Message):
    programs = get_active_programs(message.from_user.id)
    if len(programs) == 1:
        _, result = check_in(message.from_user.id, program_id=programs[0]["id"])
        await message.answer(result, reply_markup=main_keyboard(True))
    elif programs:
        await message.answer("У тебя несколько программ. Открой Mini App и выбери нужную для отметки.", reply_markup=main_keyboard(True))
    else:
        await message.answer("Активных программ нет. Открой Mini App.", reply_markup=main_keyboard(False))


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    reset_habit(message.from_user.id)
    await message.answer("Все активные программы завершены. История сохранена.",
                         reply_markup=main_keyboard(False))


@dp.message(Command("mentor"))
async def cmd_mentor(message: types.Message):
    program = get_active_program(message.from_user.id)
    if not program:
        await message.answer("Сначала выбери привычку в Mini App.")
        return
    answer, _ = await ask_mentor(message.from_user.id, program_id=program["id"])
    await message.answer(f"🧠 Совет наставника — {program['habit_name']}\n\n" + answer)


@dp.callback_query(F.data == "progress")
async def cb_progress(callback: types.CallbackQuery):
    programs = get_active_programs(callback.from_user.id)
    await callback.message.answer(progress_list_text(programs), reply_markup=main_keyboard(bool(programs)))
    await callback.answer()


@dp.callback_query(F.data == "checkin")
async def cb_checkin(callback: types.CallbackQuery):
    programs = get_active_programs(callback.from_user.id)
    if len(programs) == 1:
        ok, result = check_in(callback.from_user.id, program_id=programs[0]["id"])
    elif programs:
        ok, result = False, "У тебя несколько программ. Открой Mini App и выбери программу для отметки."
    else:
        ok, result = False, "Активных программ нет. Открой Mini App."
    await callback.message.answer(result, reply_markup=main_keyboard(bool(programs)))
    await callback.answer("Готово" if ok else "Открой сообщение")


@dp.callback_query(F.data == "home")
async def cb_home(callback: types.CallbackQuery):
    programs = get_active_programs(callback.from_user.id)
    await callback.message.answer(
        "Главное меню. Прогресс сохранён. Открой Лабораторию, чтобы выбрать привычку или посмотреть программы.",
        reply_markup=main_keyboard(bool(programs)),
    )
    await callback.answer("Главное меню")


@dp.callback_query(F.data == "reset")
async def cb_reset(callback: types.CallbackQuery):
    reset_habit(callback.from_user.id)
    await callback.message.answer("Все активные программы завершены. История и отметки сохранены.", reply_markup=main_keyboard(False))
    await callback.answer("Программы завершены")




@dp.callback_query(F.data == "mentor")
async def cb_mentor(callback: types.CallbackQuery):
    program = get_active_program(callback.from_user.id)
    if not program:
        await callback.message.answer("Сначала выбери привычку в Mini App.")
        await callback.answer()
        return
    answer, _ = await ask_mentor(callback.from_user.id, program_id=program["id"])
    await callback.message.answer(
        f"🧠 Совет наставника — {program['habit_name']}\n\n" + answer,
        reply_markup=main_keyboard(True),
    )
    await callback.answer("Совет готов")


# ============================================================
# API MINI APP
# ============================================================

class InitDataRequest(BaseModel):
    initData: str = Field(min_length=1)


class ActionRequest(InitDataRequest):
    action: str
    habit_name: str = ""
    duration_days: int = 7
    program_id: int | None = None
    note: str = ""
    message: str = ""
    reminders_per_day: int = 1
    reminders_enabled: bool = True
    timezone: str = "Europe/Warsaw"


def verify_telegram_init_data(init_data):
    if not init_data:
        raise HTTPException(status_code=401, detail="Нет данных авторизации Telegram.")

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")
        auth_date = int(parsed.get("auth_date", "0"))

        if not received_hash:
            raise ValueError("Нет hash")
        if time.time() - auth_date > 3600 or auth_date > time.time() + 60:
            raise ValueError("Данные авторизации устарели")

        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(parsed.items())
        )
        secret_key = hmac.new(
            b"WebAppData", TOKEN.encode("utf-8"), hashlib.sha256
        ).digest()
        calculated = hmac.new(
            secret_key, check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated, received_hash):
            raise ValueError("Неверная подпись")

        user_data = json.loads(parsed["user"])
        return int(user_data["id"]), str(user_data.get("username", ""))

    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Не удалось проверить авторизацию Telegram. Закрой и снова открой Mini App.",
        )


def _program_payload(program):
    day, completed, percentage = get_program_progress(program)
    with connect_db() as conn:
        checked_today = conn.execute("SELECT 1 FROM daily_checkins WHERE program_id = ? AND checkin_date = ?", (program["id"], date.today().isoformat())).fetchone() is not None
    return {
        "id": int(program["id"]), "habit_name": program["habit_name"],
        "duration_days": int(program["duration_days"]), "start_date": program["start_date"],
        "day_number": day, "completed": completed, "percentage": percentage,
        "checked_today": checked_today,
    }


def api_state_for_user(user_id):
    programs = [_program_payload(p) for p in get_active_programs(user_id)]
    settings = get_reminder_settings(user_id)
    return {"program": programs[0] if programs else None, "programs": programs, "reminders": settings}

@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler_task = asyncio.create_task(reminder_scheduler())
    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )
    logger.info("Сервис запускается на порту %s", PORT)
    try:
        yield
    finally:
        scheduler_task.cancel()
        polling_task.cancel()
        await asyncio.gather(
            scheduler_task, polling_task, return_exceptions=True
        )
        await bot.session.close()


app = FastAPI(title="Лаборатория привычек", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "habit-lab"}


@app.post("/api/state")
async def api_state(data: InitDataRequest):
    user_id, _ = verify_telegram_init_data(data.initData)
    return api_state_for_user(user_id)


@app.post("/api/action")
async def api_action(data: ActionRequest):
    user_id, username = verify_telegram_init_data(data.initData)

    if data.action in ("select_habit", "start_habit"):
        if data.duration_days not in ALLOWED_DURATIONS:
            raise HTTPException(status_code=400, detail="Выбери длительность 7, 14, 21 или 30 дней.")
        try:
            save_habit(user_id, username, data.habit_name, data.duration_days)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        try:
            programs = get_active_programs(user_id)
            await bot.send_message(
                user_id,
                f"✅ Программа добавлена: {data.habit_name} ({data.duration_days} дней).\n"
                f"Активных программ сейчас: {len(programs)}. Прогресс сохранён.",
                reply_markup=main_keyboard(bool(programs)),
            )
        except Exception:
            logger.exception("Не удалось отправить меню после добавления программы")
        return {"ok": True, "message": "Программа начата.", **api_state_for_user(user_id)}

    if data.action == "checkin":
        ok, message = check_in(user_id, data.note, data.program_id)
        return {"ok": ok, "message": message, **api_state_for_user(user_id)}

    if data.action == "reset":
        reset_habit(user_id, data.program_id)
        state = api_state_for_user(user_id)
        try:
            await bot.send_message(user_id, "Программа завершена. Остальные программы и отметки сохранены.", reply_markup=main_keyboard(bool(state["programs"])))
        except Exception:
            logger.exception("Не удалось обновить меню после завершения программы")
        return {"ok": True, "message": "Программа завершена. История сохранена.", **state}

    if data.action == "mentor":
        answer, _ = await ask_mentor(user_id, data.message.strip(), data.program_id)
        return {"ok": True, "message": answer, "used_ai": False,
                **api_state_for_user(user_id)}

    if data.action == "reminder_settings":
        settings = save_reminder_settings(
            user_id,
            data.reminders_enabled,
            data.reminders_per_day,
            data.timezone,
        )
        return {"ok": True, "message": "Настройки напоминаний сохранены.",
                "reminders": settings, **api_state_for_user(user_id)}

    raise HTTPException(status_code=400, detail="Неизвестное действие")
