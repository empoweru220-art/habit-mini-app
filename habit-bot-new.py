import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
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
PROGRAM_DURATIONS = {
    "Похудение без жёстких диет": {21},
    "Ходьба 30 минут": {7, 14, 21},
    "Домашняя тренировка 30 минут": {14, 21},
    "Сон по режиму": {14, 21},
    "Фокус: 25 минут без отвлечений": {7, 14, 21},
}
ADDITIONAL_HABITS = {
    "Чтение (20 минут)",
    "Цифровой детокс",
    "Питьевой режим",
}
ADDITIONAL_DURATIONS = {7, 14, 21}

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

        CREATE TABLE IF NOT EXISTS program_settings (
            program_id INTEGER PRIMARY KEY,
            target_bedtime TEXT,
            FOREIGN KEY(program_id) REFERENCES programs(id)
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

        CREATE TABLE IF NOT EXISTS program_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            program_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            event_key TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            UNIQUE(program_id, event_key),
            FOREIGN KEY(program_id) REFERENCES programs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_program_events_pending
        ON program_events(user_id, sent_at);
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

MAX_CALENDAR_DAYS = {7: 11, 14: 21, 21: 32, 30: 45}


def user_timezone(user_id):
    settings = get_reminder_settings(user_id)
    return ZoneInfo(valid_timezone(settings.get("timezone", "Europe/Warsaw")))


def local_today(user_id):
    return datetime.now().astimezone(user_timezone(user_id)).date()


def program_limits(program):
    duration = int(program["duration_days"])
    max_days = MAX_CALENDAR_DAYS.get(duration, duration)
    start = date.fromisoformat(program["start_date"])
    deadline = start + timedelta(days=max_days - 1)
    return max_days, deadline


def queue_program_event(conn, program, event_key, message):
    conn.execute("""INSERT OR IGNORE INTO program_events
        (program_id, user_id, event_key, message, created_at)
        VALUES (?, ?, ?, ?, ?)""",
        (program["id"], program["user_id"], event_key, message,
         datetime.now().isoformat(timespec="seconds")))


async def dispatch_program_events(user_id):
    """Доставляет накопленные события; sent_at защищает от повторной отправки."""
    with connect_db() as conn:
        rows = conn.execute("""SELECT id, message FROM program_events
            WHERE user_id = ? AND sent_at IS NULL ORDER BY id""", (user_id,)).fetchall()
    has_active = bool(get_active_programs(user_id))
    for row in rows:
        try:
            await bot.send_message(user_id, row["message"], reply_markup=main_keyboard(has_active))
            with connect_db() as conn:
                conn.execute("UPDATE program_events SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
                             (datetime.now().isoformat(timespec="seconds"), row["id"]))
                conn.commit()
        except Exception:
            logger.exception("Не удалось отправить событие программы %s", row["id"])


def evaluate_programs_for_user(user_id, today=None):
    """Проверяет пропуски, предельный срок и разовое предупреждение."""
    today = today or local_today(user_id)
    messages = []
    now = datetime.now().isoformat(timespec="seconds")
    with connect_db() as conn:
        programs = conn.execute("SELECT * FROM programs WHERE user_id = ? AND status = 'active'", (user_id,)).fetchall()
        for program in programs:
            start = date.fromisoformat(program["start_date"])
            max_days, deadline = program_limits(program)
            duration = int(program["duration_days"])
            completed = conn.execute("SELECT COUNT(*) FROM daily_checkins WHERE program_id = ?", (program["id"],)).fetchone()[0]
            if completed >= duration:
                conn.execute("UPDATE programs SET status='completed', ended_at=? WHERE id=? AND status='active'", (now, program["id"]))
                queue_program_event(conn, program, "completed", f"🏁 Отличная работа! Программа «{program['habit_name']}» завершена: {duration} из {duration} отметок. История сохранена.\n\nМожно выбрать следующую ступень — более длинную программу — или начать другую привычку.")
                messages.append(user_id)
                continue
            if today > deadline:
                conn.execute("UPDATE programs SET status='interrupted', ended_at=? WHERE id=? AND status='active'", (now, program["id"]))
                queue_program_event(conn, program, "deadline_expired", f"⌛ Программа «{program['habit_name']}» завершена досрочно: предельный срок в {max_days} календарных дней закончился. Отметки и история сохранены. Можно начать её заново или выбрать другую привычку.")
                messages.append(user_id)
                continue
            # Два последних полностью завершившихся календарных дня без отметок.
            yesterday = today - timedelta(days=1)
            day_before = today - timedelta(days=2)
            if day_before >= start:
                missed = conn.execute("""SELECT COUNT(*) FROM daily_checkins
                    WHERE program_id=? AND checkin_date IN (?, ?)""",
                    (program["id"], day_before.isoformat(), yesterday.isoformat())).fetchone()[0]
                if missed == 0:
                    conn.execute("UPDATE programs SET status='interrupted', ended_at=? WHERE id=? AND status='active'", (now, program["id"]))
                    queue_program_event(conn, program, "two_missed_days", f"Программа «{program['habit_name']}» остановлена: два календарных дня подряд прошли без отметок. Это не обнуляет твои результаты — вся история сохранена. Если хочешь, можно начать программу снова.")
                    messages.append(user_id)
                    continue
            if deadline - timedelta(days=2) <= today <= deadline:
                days_remaining = max(0, (deadline - today).days)
                queue_program_event(conn, program, "deadline_warning", f"⏳ Напоминание: у программы «{program['habit_name']}» осталось {days_remaining} дн. до окончания допустимого срока. Нужно ещё {duration - completed} отметок, чтобы завершить её успешно.")
                messages.append(user_id)
        # Поддерживаем сводную таблицу совместимой с несколькими программами.
        remaining = conn.execute("SELECT habit_name FROM programs WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
        status = 'active' if remaining else 'inactive'
        conn.execute("UPDATE user_habits SET status=?, habit_name=COALESCE(?, habit_name) WHERE user_id=?",
                     (status, remaining["habit_name"] if remaining else None, user_id))
        conn.commit()
    return bool(messages)


def get_program_progress(program):
    today = local_today(int(program["user_id"]))
    start = date.fromisoformat(program["start_date"])
    day_number = max(1, (today - start).days + 1)
    duration = int(program["duration_days"])
    with connect_db() as conn:
        completed = conn.execute("SELECT COUNT(*) AS total FROM daily_checkins WHERE program_id = ?", (program["id"],)).fetchone()["total"]
    return day_number, completed, round(min(completed, duration) / duration * 100)


def save_habit(user_id, username, habit_name, duration_days=7, target_bedtime=""):
    """Добавляет отдельную активную программу, не завершая другие."""
    habit_name = habit_name.strip()[:100]
    if not habit_name:
        raise ValueError("Название привычки пустое.")
    allowed = PROGRAM_DURATIONS.get(habit_name, ADDITIONAL_DURATIONS if habit_name in ADDITIONAL_HABITS else set())
    if duration_days not in allowed:
        raise ValueError("Для выбранной программы недоступна такая длительность.")
    if habit_name == "Сон по режиму" and target_bedtime:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", target_bedtime):
            raise ValueError("Укажи время отхода ко сну в формате ЧЧ:ММ.")
    else:
        target_bedtime = ""

    now = datetime.now().isoformat(timespec="seconds")
    today = local_today(user_id).isoformat()
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
            if habit_name == "Сон по режиму" and target_bedtime:
                conn.execute("""INSERT INTO program_settings(program_id, target_bedtime) VALUES (?, ?)
                    ON CONFLICT(program_id) DO UPDATE SET target_bedtime=excluded.target_bedtime""",
                    (int(existing["id"]), target_bedtime))
            conn.commit()
            return int(existing["id"])

        cur = conn.execute("""
            INSERT INTO programs
            (user_id, habit_name, duration_days, start_date, status, created_at)
            VALUES (?, ?, ?, ?, 'active', ?)
        """, (user_id, habit_name, duration_days, today, now))
        if habit_name == "Сон по режиму" and target_bedtime:
            conn.execute("INSERT INTO program_settings(program_id, target_bedtime) VALUES (?, ?)",
                         (int(cur.lastrowid), target_bedtime))
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
    today = local_today(user_id)
    evaluate_programs_for_user(user_id, today)
    program = get_active_program(user_id, program_id)
    if not program:
        return False, "Активной программы нет или она уже завершена. Выбери привычку в Mini App."
    start = date.fromisoformat(program["start_date"])
    day_number = (today - start).days + 1
    duration = int(program["duration_days"])
    max_days, deadline = program_limits(program)
    if day_number < 1:
        return False, "Дата начала программы некорректна."
    if today > deadline:
        evaluate_programs_for_user(user_id, today)
        return False, "Допустимый срок программы завершён. История отметок сохранена."
    try:
        with connect_db() as conn:
            conn.execute("INSERT INTO daily_checkins (program_id, user_id, checkin_date, note, created_at) VALUES (?, ?, ?, ?, ?)",
                         (program["id"], user_id, today.isoformat(), note[:500], datetime.now().isoformat(timespec="seconds")))
            completed = conn.execute("SELECT COUNT(*) FROM daily_checkins WHERE program_id=?", (program["id"],)).fetchone()[0]
            conn.execute("UPDATE user_habits SET day_count=? WHERE user_id=?", (completed, user_id))
            percentage = round(min(completed, duration) / duration * 100)
            if completed >= duration:
                conn.execute("UPDATE programs SET status='completed', ended_at=? WHERE id=? AND status='active'",
                             (datetime.now().isoformat(timespec="seconds"), program["id"]))
                queue_program_event(conn, program, "completed", f"🏁 Поздравляю! Программа «{program['habit_name']}» завершена: {duration} из {duration} отметок.\n\nТы довёл дело до конца. История сохранена. Следующий шаг — перейти на программу подлиннее, выбрать другую привычку или пока остановиться.")
                remaining = conn.execute("SELECT habit_name FROM programs WHERE user_id=? AND status='active' AND id<>? ORDER BY id DESC LIMIT 1", (user_id, program["id"])).fetchone()
                conn.execute("UPDATE user_habits SET status=?, habit_name=COALESCE(?, habit_name) WHERE user_id=?", ("active" if remaining else "completed", remaining["habit_name"] if remaining else None, user_id))
            else:
                for threshold, label in ((50, "50"), (75, "75")):
                    if percentage >= threshold:
                        queue_program_event(conn, program, f"milestone_{label}", f"🔥 Отличный темп! По программе «{program['habit_name']}» выполнено {percentage}%. Продолжай в своём ритме — осталось {duration-completed} отметок.")
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "Сегодня отметка уже сделана для этой программы."
    if completed >= duration:
        message = f"🏁 Программа завершена! Отмечено {completed} из {duration} дней (100%). История сохранена."
    else:
        message = f"Отметка сохранена.\nПривычка: {program['habit_name']}\nОтмечено дней: {completed} из {duration} ({percentage}%).\nКалендарный день: {day_number} из {max_days}."
    return True, message

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
                    SELECT p.user_id, COALESCE(r.enabled, 1) AS enabled,
                           COALESCE(r.reminders_per_day, 1) AS reminders_per_day,
                           COALESCE(r.timezone, 'Europe/Warsaw') AS timezone
                    FROM (
                        SELECT DISTINCT user_id FROM programs WHERE status='active'
                        UNION
                        SELECT DISTINCT user_id FROM program_events WHERE sent_at IS NULL
                    ) p
                    LEFT JOIN reminder_settings r ON r.user_id=p.user_id
                """).fetchall()

            now_utc = datetime.now().astimezone()
            for settings in users:
                uid = int(settings["user_id"])
                evaluate_programs_for_user(uid, now_utc.astimezone(ZoneInfo(valid_timezone(settings["timezone"]))).date())
                await dispatch_program_events(uid)
            for settings in users:
                if not bool(settings["enabled"]):
                    continue
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
                        SELECT id, user_id, habit_name, duration_days, start_date
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
                    max_days = MAX_CALENDAR_DAYS.get(duration, duration)
                    if 1 <= day <= max_days:
                        active_lines.append(f"• {program['habit_name']} — календарный день {day} · отметок {get_program_progress(program)[1]}/{duration}")
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
# СТАТИЧЕСКИЕ ЕЖЕДНЕВНЫЕ СОВЕТЫ
# В каждом дне — три самостоятельных подсказки. Они не циклируются.
# ============================================================

DAILY_ADVICE = {
    "Похудение без жёстких диет": [
        ("Запиши обычный рацион за день без попытки сразу всё исправить.", "Добавь к одному приёму пищи овощи или другой привычный источник клетчатки.", "Не пропускай еду ради компенсации вчерашнего дня."),
        ("Выбери один простой завтрак, после которого тебе комфортно несколько часов.", "Ешь сидя и по возможности без экрана — так легче заметить насыщение.", "Сравни голод до еды и после неё, не оценивая себя."),
        ("Проверь, какие напитки дают много сахара, и выбери один вариант замены.", "Поставь воду или несладкий напиток в доступное место.", "Не запрещай любимые продукты: оценивай общий баланс, а не один продукт."),
        ("Собери тарелку так, чтобы в ней были белок, овощи и привычный гарнир.", "Добавь источник белка в тот приём пищи, где его обычно мало.", "Не уменьшай порции резко — начни с осознанного выбора."),
        ("Пройдись 10–15 минут в удобном темпе, если самочувствие позволяет.", "Выбери короткий маршрут, который легко повторить.", "Движение сегодня — поддержка самочувствия, а не наказание за еду."),
        ("Заметь, в какое время чаще появляется желание перекусить без голода.", "Подготовь заранее удобный перекус на случай длинного промежутка между едой.", "Если голод настоящий, поешь; не нужно терпеть его ради плана."),
        ("Подведи итоги первой недели: что оказалось простым и устойчивым?", "Оставь одну полезную перемену, которая не требует силы воли.", "Не делай вывод о результате по одному взвешиванию или одному дню."),
        ("Планируя покупки, добавь продукты для двух простых домашних блюд.", "Проверь, есть ли дома быстрый сбалансированный вариант на занятый день.", "Не ходи за покупками голодным, если это провоцирует импульсивный выбор."),
        ("Во время еды сделай паузу в середине и оцени насыщение.", "Попробуй есть немного медленнее, не превращая это в соревнование.", "Остановиться можно тогда, когда стало комфортно, а не когда тарелка пуста."),
        ("Выбери один источник белка, который тебе нравится и доступен.", "Добавь его в меню на завтра без усложнения рецепта.", "Учитывай личные ограничения и рекомендации врача, если они есть."),
        ("Посмотри, как сон влияет на аппетит и энергию в течение дня.", "Подготовь спокойный вечерний ритуал вместо позднего бесцельного перекуса.", "Не вини себя за повышенный аппетит после недосыпа — сначала восстановись."),
        ("Замени идею «всё или ничего» на один следующий разумный выбор.", "Если план питания нарушился, вернись к обычному режиму со следующего приёма пищи.", "Не используй голодание или чрезмерные тренировки для компенсации."),
        ("Попробуй новый простой овощной гарнир или способ приготовления.", "Сохрани рецепт, если он оказался вкусным и удобным.", "Полезный рацион должен оставаться приемлемым по вкусу и бюджету."),
        ("Оцени размер порции по комфорту, а не по жёсткому запрету.", "Положи добавку не автоматически, а после небольшой паузы.", "При сильном голоде не ограничивай себя искусственно."),
        ("Найди один момент дня для спокойной прогулки или лёгкой активности.", "Подготовь одежду заранее, чтобы начать было проще.", "Выбирай нагрузку с учётом здоровья и текущей формы."),
        ("Проверь, не превращается ли усталость или стресс в единственный повод поесть.", "Подготовь ещё один способ переключиться: прогулка, душ, разговор или отдых.", "Эмоциональный голод не повод для стыда; наблюдай за ним спокойно."),
        ("Составь список из трёх блюд, которые можно быстро приготовить.", "Добавь недостающие ингредиенты в список покупок.", "Повторять удобные блюда нормально — разнообразие не обязано быть сложным."),
        ("Отметь, какие изменения дают больше энергии и сытости.", "Сохрани полезные наблюдения, а не только цифры.", "Не сравнивай свой темп с чужими результатами."),
        ("Проверь, хватает ли времени на полноценный приём пищи.", "Если день плотный, запланируй перерыв заранее.", "Регулярность важнее попытки питаться идеально."),
        ("Выбери привычку, которую сможешь сохранить после программы.", "Продумай простой план на загруженный день.", "Не вводи новые ограничения на финише ради быстрого результата."),
        ("Подведи итог трёх недель: какие действия стали естественными?", "Выбери две практики для продолжения на следующей неделе.", "Оцени прогресс по самочувствию, устойчивости привычек и другим важным лично тебе признакам."),
    ],
    "Ходьба 30 минут": [
        ("Выбери ровный знакомый маршрут и пройди комфортное время без спешки.", "Подготовь удобную обувь и одежду по погоде.", "Начинай с темпа, при котором можешь спокойно разговаривать."),
        ("Раздели прогулку на два коротких выхода, если так проще.", "Отметь место, где удобно сделать паузу.", "Не пытайся компенсировать вчерашний пропуск длинной прогулкой."),
        ("Добавь к прогулке пять минут, только если вчерашняя нагрузка была комфортной.", "Проверь, не натирает ли обувь.", "При боли или плохом самочувствии сократи маршрут или отдохни."),
        ("Выбери время прогулки, которое меньше всего конфликтует с делами.", "Поставь напоминание не о результате, а о выходе из дома.", "Сделай маршрут приятнее музыкой, подкастом или тишиной."),
        ("Попробуй пройти часть привычного пути пешком.", "Найди безопасный участок с удобными тротуарами.", "Не ускоряйся ради цифр, если дыхание становится некомфортным."),
        ("Обрати внимание на осанку и естественный шаг.", "Расслабь плечи и не сжимай кисти.", "Не нужно специально менять походку, если это вызывает дискомфорт."),
        ("Подведи итоги первой недели и выбери любимый маршрут.", "Проверь, сколько времени реально удаётся выделять.", "Постоянство важнее рекорда по расстоянию."),
        ("Добавь небольшой новый участок к маршруту, если хочется разнообразия.", "Заранее проверь погоду и освещение.", "Безопасность важнее выполнения плана любой ценой."),
        ("Попробуй прогулку после еды в спокойном темпе.", "Оставь телефон в кармане и замечай окружающее.", "Если чувствуешь тяжесть или недомогание, не форсируй ходьбу."),
        ("Сравни самочувствие до и после прогулки.", "Запиши, что помогает выходить из дома вовремя.", "Не оценивай успех только по шагомеру."),
        ("Выбери один день для прогулки с близким человеком.", "Договорись о времени заранее.", "Разговорный темп — хороший ориентир для умеренной нагрузки."),
        ("Подготовь запасной короткий маршрут на случай нехватки времени.", "Если погода плохая, выбери безопасную альтернативу в помещении.", "Короткая прогулка лучше, чем рискованная прогулка в опасных условиях."),
        ("Проверь состояние обуви и носков.", "После ходьбы дай ногам немного отдыха.", "При стойкой боли не продолжай нагрузку через силу."),
        ("Сделай привычную прогулку чуть осознаннее: замечай дыхание и темп.", "Не сравнивай скорость с другими пешеходами.", "Сбавить темп — нормальная часть тренировки выносливости."),
        ("Пройди знакомый маршрут в обратном направлении, если он безопасен.", "Добавь короткую остановку в приятном месте.", "Не увеличивай одновременно и скорость, и продолжительность."),
        ("Оцени, какие дни недели подходят для более длинной прогулки.", "Запланируй один резервный временной слот.", "Отдых тоже помогает сохранять регулярность."),
        ("Попробуй пройти часть маршрута без наушников, сохраняя внимание к дороге.", "Выбери спокойный участок, где можно идти без спешки.", "Сохраняй внимание к транспорту и окружающей обстановке."),
        ("Проверь, стало ли легче выделять время на ходьбу.", "Отметь не только километры, но и настроение после выхода.", "Не добавляй нагрузку резко, даже если появилась мотивация."),
        ("При желании добавь несколько минут лёгкого темпа в середине прогулки.", "Вернись к комфортному шагу, если дыхание участилось слишком сильно.", "Не делай интервалы при плохом самочувствии или противопоказаниях."),
        ("Продумай, как сохранить ходьбу в загруженные дни.", "Подготовь обувь у выхода заранее.", "План должен помогать жизни, а не мешать ей."),
        ("Подведи итог: какой маршрут и время оказались самыми удобными?", "Выбери реалистичную частоту прогулок на следующий этап.", "Сохрани привычку в комфортном темпе, не гонясь за рекордами."),
    ],
    "Домашняя тренировка 30 минут": [
        ("Подготовь свободное место и сделай лёгкую разминку перед нагрузкой.", "Выбери упражнения, которые уже умеешь выполнять безопасно.", "Сегодня важнее техника, чем количество повторений."),
        ("Составь короткий комплекс из нескольких знакомых движений.", "Оставляй паузы между упражнениями.", "Остановись, если появляется острая боль, головокружение или необычная одышка."),
        ("Проверь технику одного упражнения перед зеркалом или по надёжной инструкции.", "Уменьши амплитуду, если движение пока даётся тяжело.", "Не соревнуйся с видео и чужими результатами."),
        ("Выдели отдельное время на тренировку и убери лишние отвлечения.", "Подготовь воду и полотенце заранее.", "Не тренируйся через болезнь или выраженное недомогание."),
        ("Сделай акцент на контролируемых движениях и ровном дыхании.", "Лучше меньше повторений с хорошей техникой.", "Не задерживай дыхание во время усилия."),
        ("Проведи восстановительный день с мягкой подвижностью или отдыхом.", "Оцени, какие мышцы устали после предыдущих занятий.", "Восстановление — часть программы, а не пропуск из-за слабости."),
        ("Подведи итоги первой недели и выбери упражнения, которые подходят тебе.", "Убери движение, вызывающее дискомфорт.", "Не увеличивай объём только ради выполнения плана."),
        ("Попробуй распределить нагрузку на верх тела, низ тела и корпус.", "Не тренируй одну и ту же уставшую группу мышц интенсивно каждый день.", "Если нет опыта, начни с облегчённых вариантов упражнений."),
        ("Сделай разминку чуть внимательнее: суставы и мышцы должны подготовиться.", "Проверь устойчивость поверхности и свободное пространство.", "Избегай скользкого пола и неустойчивой мебели."),
        ("Выбери одно упражнение и сосредоточься на качестве каждого повтора.", "Сделай паузу, если техника начинает ухудшаться.", "Не выполняй движения рывками."),
        ("Запланируй лёгкий день между более интенсивными занятиями.", "Сон и обычное питание помогают восстановлению.", "Боль в суставах — сигнал остановиться, а не преодолеть себя."),
        ("Если чувствуешь себя хорошо, добавь один подход только к знакомому упражнению.", "Не повышай одновременно вес, число подходов и сложность.", "Прогресс должен оставаться управляемым."),
        ("Проверь, какие упражнения занимают слишком много времени.", "Собери короткую версию комплекса на занятый день.", "Не нужно наверстывать пропущенную тренировку двойной нагрузкой."),
        ("Попробуй упражнение на равновесие рядом с устойчивой опорой.", "Двигайся медленно и контролируй положение тела.", "Пропусти упражнение, если есть риск падения или головокружение."),
        ("Оцени дыхание и общее самочувствие во время занятия.", "Делай паузы до того, как усталость испортит технику.", "Не игнорируй необычные симптомы."),
        ("Выбери день для восстановления без силовой нагрузки.", "Можно ограничиться спокойной прогулкой или отдыхом.", "Отдых не снижает ценность уже выполненной работы."),
        ("Сравни ощущения с первой неделей, не требуя резкого роста результатов.", "Отметь, какое движение стало увереннее.", "Не измеряй прогресс только весом или внешним видом."),
        ("Подготовь план тренировки на следующие два занятия.", "Заранее реши, какие упражнения можно заменить при дискомфорте.", "Планируй нагрузку с учётом своего уровня подготовки."),
        ("Сделай разминку и заминку полноценными частями занятия.", "После тренировки спокойно восстанови дыхание.", "Не растягивайся через боль."),
        ("Оставь в комплексе упражнения, которые тебе подходят и нравятся.", "Убери лишнюю сложность, если она мешает регулярности.", "Устойчивая программа лучше экстремальной."),
        ("Подведи итог трёх недель и выбери комфортный график дальше.", "Запланируй дни нагрузки и дни восстановления.", "Если есть хронические заболевания или ограничения, согласуй нагрузку со специалистом."),
    ],
    "Сон по режиму": [
        ("Выбери личное целевое время отхода ко сну и время подъёма.", "Сдвигай привычный график постепенно, если сейчас он сильно отличается.", "Не ориентируйся на универсальное время вроде 23:00 — режим должен подходить твоей жизни."),
        ("Поставь спокойное напоминание о начале подготовки ко сну.", "Подготовь одежду и вещи на завтра заранее.", "Сделай вечерний ритуал коротким и реалистичным."),
        ("За некоторое время до сна приглуши освещение.", "Убери яркий экран, если он мешает расслабиться.", "Если экран нужен, снизь яркость и избегай возбуждающего контента."),
        ("Старайся вставать примерно в одно время, насколько позволяет график.", "Не компенсируй поздний отход ко сну чрезмерно длинным дневным сном.", "При выраженной сонливости безопасность важнее расписания."),
        ("Проветри спальню и проверь, комфортна ли температура.", "Убери лишний свет и шум настолько, насколько это возможно.", "Не трать деньги на гаджеты, пока не проверил простые условия."),
        ("Заметь, какие дела чаще всего задерживают тебя вечером.", "Перенеси одно из них на более раннее время.", "Не пытайся изменить весь вечер за один день."),
        ("Подведи итоги первой недели: что помогало заснуть вовремя?", "Оставь один наиболее полезный вечерний сигнал.", "Разовые сбои не означают, что режим потерян."),
        ("Ограничь поздний кофеин, если замечаешь его влияние на сон.", "Проверь состав напитков, которые пьёшь во второй половине дня.", "Чувствительность к кофеину индивидуальна."),
        ("Сделай последние минуты перед сном спокойными и предсказуемыми.", "Подготовь книгу или тихое занятие вместо бесконечной ленты.", "Не превращай подготовку ко сну в длинный список обязательств."),
        ("Если мысли мешают расслабиться, запиши короткий список дел на завтра.", "Выдели отдельное время для решения тревожащих задач днём.", "Если бессонница стойкая, обратись за профессиональной помощью."),
        ("Проверь, не слишком ли поздно проходит плотный ужин именно для тебя.", "Понаблюдай, какие привычки перед сном влияют на комфорт.", "Не вводи строгие пищевые ограничения без необходимости."),
        ("Сохрани выбранное время подъёма и в выходной в разумных пределах.", "Если график сменный, ориентируйся на собственный стабильный ритуал.", "Не жертвуй необходимой продолжительностью сна ради формального времени."),
        ("Попробуй короткое спокойное дыхание или расслабление мышц.", "Не оценивай успех по скорости засыпания.", "Если упражнение вызывает дискомфорт, выбери другой способ расслабиться."),
        ("Убери будильник так, чтобы не проверять время постоянно.", "Подготовь комфортную обстановку у кровати.", "Ночные пробуждения иногда случаются и сами по себе не означают провал."),
        ("Посмотри, как дневная активность отражается на вечернем засыпании.", "Добавь прогулку или движение в удобное время дня.", "Избегай интенсивной нагрузки прямо перед сном, если она тебя бодрит."),
        ("Проверь, не слишком ли много задач оставляешь на поздний вечер.", "Назначь время завершения рабочих дел.", "Границы рабочего дня помогают подготовиться к отдыху."),
        ("Сделай спальню местом отдыха: убери работу и лишние уведомления.", "Если возможно, заряжай телефон не у кровати.", "Не ругай себя, если условия дома не идеальны."),
        ("Оцени, хватает ли тебе времени на сон при текущем расписании.", "При необходимости пересмотри вечерние обязательства.", "Большинству взрослых нужно не менее семи часов сна, но индивидуальные потребности различаются."),
        ("Повтори самый работающий вечерний ритуал.", "Не добавляй новые правила, если текущие уже помогают.", "Последовательность важнее идеального выполнения."),
        ("Продумай, как сохранить режим в дни встреч и поездок.", "Выбери один опорный элемент ритуала, который можно взять с собой.", "Гибкость помогает не бросать привычку после одного сбоя."),
        ("Подведи итог: какое расписание оказалось реалистичным именно для тебя?", "Сохрани удобное время подготовки ко сну и подъёма.", "При длительных проблемах со сном обсуди их с врачом."),
    ],
    "Фокус: 25 минут без отвлечений": [
        ("Выбери одну задачу и сформулируй результат одного 25-минутного блока.", "Убери со стола всё, что не относится к задаче.", "Не пытайся вместить в один блок весь проект."),
        ("Перед стартом запиши самый первый физический шаг.", "Поставь таймер на 25 минут и начни без дополнительной подготовки.", "Если задача большая, достаточно продвинуть её на один шаг."),
        ("Отключи уведомления на время рабочего блока.", "Закрой вкладки и приложения, не нужные для текущей задачи.", "Оставь способ связи для действительно срочных ситуаций."),
        ("Запиши отвлекающую мысль на бумаге и вернись к работе.", "Не переключайся на мелкие задачи сразу после появления идеи.", "Вернуться к задаче важнее, чем ругать себя за отвлечение."),
        ("Начни с самой понятной части задачи, чтобы войти в работу.", "Подготовь нужные документы до запуска таймера.", "Не меняй цель блока на ходу без веской причины."),
        ("После блока сделай короткую паузу без бесконечной прокрутки ленты.", "Встань, потянись или посмотри вдаль.", "Перерыв нужен для восстановления внимания, а не для новой перегрузки."),
        ("Подведи итоги первой недели: в какое время фокус даётся легче?", "Запланируй один блок на наиболее продуктивный промежуток.", "Не требуй от себя одинаковой концентрации каждый день."),
        ("Разбей сложную задачу на шаги размером с один рабочий блок.", "Выбери только следующий шаг, а не весь список.", "Чёткая граница задачи снижает сопротивление началу."),
        ("Попробуй оставить телефон в другой комнате на один блок.", "Если это невозможно, положи его экраном вниз и включи режим фокусировки.", "Не отключай важные каналы безопасности и связи."),
        ("Перед началом сформулируй, что сегодня точно не будешь делать.", "Убери одну лишнюю задачу из текущего списка.", "Приоритет — это также осознанный отказ от второстепенного."),
        ("Используй заметку для фиксации места, на котором остановился.", "В конце блока запиши следующий шаг на будущее.", "Так проще вернуться к работе после перерыва."),
        ("Проведи один блок над задачей, которую давно откладываешь.", "Сделай вход в неё максимально простым — открой файл или подготовь материалы.", "Не оценивай весь результат до завершения первого шага."),
        ("Проверь, не слишком ли часто переключаешься между мессенджерами.", "Назначь отдельное время для проверки сообщений.", "Если работа требует связи, оставь только необходимые уведомления."),
        ("Попробуй работать с одним открытым окном или документом.", "Сверни лишние панели и вкладки.", "Организуй среду под задачу, а не под идеальную картинку."),
        ("Оцени уровень энергии перед блоком и выбери посильную задачу.", "Сложные задачи ставь на время, когда обычно легче думать.", "Если усталость сильная, сначала восстановись."),
        ("После блока отметь конкретный результат, даже если он небольшой.", "Не измеряй продуктивность только количеством закрытых пунктов.", "Качество внимания важнее постоянной занятости."),
        ("Попробуй начать блок без музыки, если слова отвлекают.", "Если тишина мешает, выбери нейтральный фоновый звук.", "Подбирай среду под себя, а не по чужому правилу."),
        ("Собери список повторяющихся отвлечений за день.", "Выбери одно из них и измени условие, которое его запускает.", "Работай с причиной отвлечения, а не только с силой воли."),
        ("Запланируй два блока с полноценным перерывом между ними, если есть ресурс.", "Не ставь блоки подряд без паузы.", "Один качественный блок лучше нескольких изнурительных."),
        ("Заверши блок короткой записью о достигнутом.", "Подготовь рабочее место для следующего старта.", "Не продолжай автоматически, если пора отдыхать."),
        ("Подведи итог: какие условия чаще всего помогали сосредоточиться?", "Выбери реалистичное число блоков на следующую неделю.", "Сохрани гибкость — концентрация зависит от задач и состояния."),
    ],
}

# Для дополнительных привычек используются отдельные, нециклические подсказки по дням.
EXTRA_ADVICE = {
    "Чтение (20 минут)": [
        ("Выбери книгу заранее и положи её на видное место.", "Начни с нескольких страниц, не требуя от себя большого объёма.", "Запиши одну мысль, которая зацепила."),
        ("Привяжи чтение к удобному времени дня.", "Убери телефон на время чтения.", "Если текст не подходит, разреши себе выбрать другой."),
        ("Продолжи с того места, где остановился, без долгого разгона.", "Подготовь комфортное освещение.", "Отметь, что помогло не отвлекаться."),
        ("Попробуй читать в другом спокойном месте.", "Оставь закладку, чтобы легко вернуться.", "Не превращай чтение в гонку за страницами."),
        ("Выдели короткое время для чтения до просмотра ленты.", "Отключи уведомления на этот период.", "Сохрани интересную цитату своими словами."),
        ("Выбери главу или небольшой фрагмент как цель.", "Сделай паузу, если внимание устало.", "Вернись к тексту без самокритики."),
        ("Подведи итог недели и выбери удобное время для чтения.", "Подготовь следующую книгу или главу.", "Продолжай в комфортном темпе."),
    ],
    "Цифровой детокс": [
        ("Убери телефон подальше на один короткий отрезок времени.", "Отключи одно необязательное уведомление.", "Заметь, в какой момент рука тянется к экрану."),
        ("Не бери телефон во время одного приёма пищи.", "Положи его экраном вниз и вне досягаемости.", "Замени автоматическую проверку короткой паузой."),
        ("Убери отвлекающее приложение с главного экрана.", "Проверь настройки уведомлений.", "Оставь доступными важные звонки и сообщения."),
        ("Назначь конкретное время для проверки ленты.", "Не открывай приложение без понятной цели.", "Если сорвался, просто вернись к выбранному правилу."),
        ("Проведи 20 минут без экрана перед сном.", "Подготовь альтернативу: книга, музыка или спокойная беседа.", "Не требуй полного отказа от телефона."),
        ("Проверь экранное время и выбери одно приложение для сокращения.", "Установи реалистичный лимит.", "Смотри на тенденцию, а не на один день."),
        ("Подведи итог недели и оставь самое полезное ограничение.", "Выбери время дня, когда экран особенно мешает.", "Используй телефон намеренно, а не автоматически."),
    ],
    "Питьевой режим": [
        ("Держи напиток под рукой и ориентируйся на жажду.", "Отметь, в какие моменты забываешь пить.", "Не нужно заставлять себя выпивать фиксированные два литра."),
        ("Свяжи глоток воды с естественным перерывом.", "Выбери удобную бутылку или стакан.", "Учитывай воду из напитков и еды."),
        ("Проверь, удобно ли тебе пить в течение рабочего дня.", "Поставь стакан в заметное место.", "Не выпивай большой объём залпом."),
        ("Возьми воду с собой, если планируешь выход из дома.", "Пей по потребности и с учётом активности и погоды.", "При медицинских ограничениях следуй рекомендациям врача."),
        ("Заметь, как меняется жажда в течение дня.", "Подготовь воду заранее перед занятым периодом.", "Не используй цвет мочи как единственный диагностический показатель."),
        ("Подбери удобный для себя ритм небольших порций.", "Не пытайся наверстать пропущенное вечером.", "Избыточное питьё тоже может быть вредным."),
        ("Подведи итог недели и оставь удобный способ напоминать себе о воде.", "Оцени комфорт, а не выполнение чужой нормы.", "Сохраняй гибкость в поездках и выходные."),
    ],
}

EXTRA_EXTENSION = {
    "Чтение (20 минут)": [
        ("Попробуй прочитать главу целиком, если она короткая.", "Отметь, какой вопрос возник по ходу текста.", "Не возвращайся к абзацу многократно ради идеального понимания."),
        ("Выдели одну мысль и перескажи её своими словами.", "Подготовь заметку для следующего чтения.", "Если внимание устало, сделай паузу."),
        ("Попробуй бумажную книгу или другой формат, если экран отвлекает.", "Настрой удобное освещение.", "Сохраняй комфорт для глаз."),
        ("Выбери спокойный отрезок дня без параллельных дел.", "Предупреди домашних, если тебе нужно немного тишины.", "Не стремись читать быстрее обычного."),
        ("Вернись к отложенной книге, если интерес к ней сохранился.", "Если нет — выбери чтение, которое действительно увлекает.", "Разреши себе менять книгу."),
        ("Составь короткий список книг или текстов на будущее.", "Выбери только следующий материал, не перегружая список.", "Чтение должно оставаться удовольствием."),
        ("Попробуй обсудить прочитанное с кем-нибудь.", "Запиши одну цитату или идею без копирования больших фрагментов.", "Подумай, как идея связана с твоим опытом."),
        ("Сделай чтение частью уже существующего вечернего ритуала.", "Подготовь книгу заранее.", "Не жертвуй необходимым сном ради выполнения нормы."),
        ("Попробуй научно-популярный или художественный текст другого жанра.", "Оцени, какой формат помогает удерживать интерес.", "Не сравнивай свой темп с другими читателями."),
        ("Вернись к заметкам и просмотри, что запомнилось.", "Выбери одну идею для дальнейшего размышления.", "Не обязательно применять каждую прочитанную мысль."),
        ("Проведи чтение без цели дочитать до конца главы.", "Остановись на естественной паузе.", "Регулярность важнее объёма за один подход."),
        ("Найди удобное место для чтения вне дома, если это возможно.", "Возьми книгу или электронную читалку с собой.", "Соблюдай безопасность и не читай во время движения."),
        ("Подумай, какой тип текстов сейчас отвечает твоим интересам.", "Сохрани рекомендацию на будущее.", "Не превращай список чтения в обязательство."),
        ("Подведи итог дополнительной недели чтения.", "Выбери реалистичное время и формат на следующий этап.", "Оставь привычку гибкой и приятной."),
    ],
    "Цифровой детокс": [
        ("Оставь один промежуток дня без коротких проверок телефона.", "Предупреди близких, когда будешь недоступен для несрочных сообщений.", "Сохрани доступ к экстренной связи."),
        ("Замени привычку открывать ленту короткой прогулкой по комнате.", "Убери телефон с рабочего стола.", "Не запрещай себе пользоваться им по делу."),
        ("Проверь, какие уведомления действительно требуют немедленного внимания.", "Отключи остальные на выбранный период.", "Возвращай настройки, если пропускаешь важные сообщения."),
        ("Устрой один час без развлекательных приложений.", "Подготовь занятие на это время заранее.", "Начни с посильного интервала."),
        ("Не бери телефон в руки сразу после пробуждения.", "Сначала выполни одно спокойное утреннее действие.", "Если телефон нужен как будильник, положи его подальше от кровати."),
        ("Проведи встречу или разговор без параллельного просмотра экрана.", "Убери телефон из поля зрения.", "Объясни близким, что хочешь уделить им внимание."),
        ("Проверь, какие приложения чаще всего открываешь автоматически.", "Убери одно из них с первого экрана.", "Сделай полезные приложения доступнее отвлекающих."),
        ("Выбери одно окно времени для новостей.", "Не обновляй ленту автоматически.", "При необходимости проверяй важную информацию из надёжных источников."),
        ("Попробуй вечер без коротких видео.", "Замени их заранее выбранным занятием.", "Не ругай себя за случайный просмотр — вернись к плану."),
        ("Оставь телефон в сумке во время прогулки.", "Смотри на дорогу и окружение.", "Не используй наушники так, чтобы терять внимание к безопасности."),
        ("Настрой режим фокусировки на время одной важной задачи.", "Разреши уведомления только от нужных контактов.", "Проверь, что важные сигналы не заблокированы."),
        ("Оцени, стало ли легче переключаться на отдых без экрана.", "Запиши одну положительную перемену.", "Не измеряй цифровой детокс только минутами экранного времени."),
        ("Попробуй один приём пищи без телефона.", "Сосредоточься на вкусе и разговоре.", "Не превращай это правило в повод для конфликта."),
        ("Подведи итог дополнительной недели и выбери одно постоянное ограничение.", "Оставь гибкие исключения для работы и связи.", "Цель — осознанное использование, а не полный отказ от технологий."),
    ],
    "Питьевой режим": [
        ("Проверь, удобно ли тебе пить воду в дороге.", "Возьми небольшой запас воды, если это уместно.", "Не пей больше, чем комфортно и безопасно."),
        ("Свяжи питьё с началом рабочего перерыва.", "Оставь стакан в доступном месте.", "Учитывай индивидуальные медицинские ограничения."),
        ("Попробуй обычную воду комфортной температуры.", "Если не нравится вкус, выбери безопасный вариант без избытка сахара.", "Не обязательно покупать специальные напитки."),
        ("Заметь, меняется ли потребность в воде в жару или при активности.", "Пей чаще небольшими порциями по потребности.", "Не используй универсальные нормы без учёта здоровья."),
        ("Подготовь напиток перед длительным рабочим блоком.", "Не забывай делать перерывы на движение.", "Не заменяй водой полноценное питание."),
        ("Оцени, не мешает ли тебе слишком позднее обильное питьё.", "Распредели привычные порции удобнее в течение дня.", "Не ограничивай необходимое питьё ради расписания."),
        ("Проверь, есть ли вода там, где проводишь большую часть дня.", "Подготовь запасной стакан или бутылку.", "Следи за чистотой ёмкости."),
        ("В дни прогулок учитывай погоду и продолжительность активности.", "Возьми воду при необходимости.", "Не пытайся восполнить всё сразу после возвращения."),
        ("Заметь, какие напоминания помогают, а какие раздражают.", "Оставь только удобный способ напоминать себе.", "Не нужно превращать питьё в постоянный контроль."),
        ("Проверь, не путаешь ли жажду с привычкой пить сладкие напитки.", "Попробуй один раз выбрать воду вместо сладкого напитка.", "Не запрещай себе напитки жёстко."),
        ("Учитывай супы, фрукты и другие продукты с водой в составе.", "Сохраняй разнообразное питание.", "Не пытайся рассчитывать каждую каплю."),
        ("Если день проходит вне дома, продумай доступ к воде заранее.", "Выбери удобную бутылку.", "Не пей из небезопасных источников."),
        ("Оцени комфорт выбранного ритма в течение дня.", "При необходимости измени напоминания.", "При назначенном врачом ограничении жидкости соблюдай его."),
        ("Подведи итог дополнительной недели и оставь подходящий ритм.", "Сохрани простую привычку, не привязанную к жёсткой цифре.", "Ориентируйся на потребности организма и медицинские рекомендации."),
    ],
}

def daily_advice_for_program(program):
    name = str(program["habit_name"])
    day_number, completed, _ = get_program_progress(program)
    if name in DAILY_ADVICE:
        days = DAILY_ADVICE[name]
    elif name in EXTRA_ADVICE:
        days = EXTRA_ADVICE[name]
        if day_number > len(days):
            if day_number > len(days) + len(EXTRA_EXTENSION[name]):
                return [
                    f"Сегодня календарный день {day_number}: продолжай привычку без попыток наверстать пропуски.",
                    f"У тебя уже {completed} отметок. Выбери следующий небольшой шаг, который реально выполнить.",
                    "Оставь только те действия, которые подходят твоему самочувствию и расписанию.",
                ]
            extension = EXTRA_EXTENSION[name]
            idx = day_number - len(days) - 1
            return list(extension[idx])
    else:
        # Для исторических программ — уникальная по календарному дню подсказка.
        return [
            f"День {day_number}: выбери небольшой выполнимый шаг по программе «{name}».",
            f"На день {day_number} подготовь всё необходимое заранее, чтобы начать без лишних препятствий.",
            f"В конце дня {day_number} отметь, что помогло выполнить действие и что стоит упростить.",
        ]
    if day_number > len(days):
        return [
            f"Сегодня календарный день {day_number}: продолжай программу в комфортном темпе.",
            f"У тебя уже {completed} отметок. Сосредоточься на следующем посильном шаге.",
            "Не пытайся наверстать пропуски чрезмерной нагрузкой или ограничениями.",
        ]
    return list(days[max(0, day_number - 1)])


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
        builder.button(text="🏠 В главное меню", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()

def progress_text(program):
    day, completed, percentage = get_program_progress(program)
    duration = int(program["duration_days"])
    max_days, deadline = program_limits(program)
    state = f"Календарный день: {day} из {max_days} (допустимый срок)."
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
        max_days, _ = program_limits(program)
        day_label = f"календарный день {min(day, max_days)} из {max_days}"
        parts.append(f"{i}. 🎯 {program['habit_name']}\n   📅 {day_label} · отметок {completed}/{duration} · {percentage}%")
    return "\n\n".join(parts)


@dp.message(Command("checkin"))
async def cmd_checkin(message: types.Message):
    programs = get_active_programs(message.from_user.id)
    if len(programs) == 1:
        _, result = check_in(message.from_user.id, program_id=programs[0]["id"])
        await message.answer(result, reply_markup=main_keyboard(bool(get_active_programs(message.from_user.id))))
        await dispatch_program_events(message.from_user.id)
    elif programs:
        await message.answer("У тебя несколько программ. Открой Mini App и выбери нужную для отметки.", reply_markup=main_keyboard(True))
    else:
        await message.answer("Активных программ нет. Открой Mini App.", reply_markup=main_keyboard(False))


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    reset_habit(message.from_user.id)
    await message.answer("Все активные программы завершены. История сохранена.",
                         reply_markup=main_keyboard(False))


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
    await callback.message.answer(result, reply_markup=main_keyboard(bool(get_active_programs(callback.from_user.id))))
    await dispatch_program_events(callback.from_user.id)
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
    target_bedtime: str = ""
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
    user_id = int(program["user_id"])
    today = local_today(user_id)
    day, completed, percentage = get_program_progress(program)
    max_days, deadline = program_limits(program)
    with connect_db() as conn:
        checked_today = conn.execute("SELECT 1 FROM daily_checkins WHERE program_id=? AND checkin_date=?", (program["id"], today.isoformat())).fetchone() is not None
        setting = conn.execute("SELECT target_bedtime FROM program_settings WHERE program_id=?", (program["id"],)).fetchone()
    advice = daily_advice_for_program(program)
    return {
        "id": int(program["id"]), "habit_name": program["habit_name"],
        "duration_days": int(program["duration_days"]), "start_date": program["start_date"],
        "day_number": day, "completed": completed, "percentage": percentage,
        "checked_today": checked_today, "max_calendar_days": max_days,
        "days_left": max(0, (deadline - today).days),
        "deadline_date": deadline.isoformat(),
        "daily_advice": advice,
        "target_bedtime": setting["target_bedtime"] if setting else None,
    }


def api_state_for_user(user_id):
    evaluate_programs_for_user(user_id)
    programs = [_program_payload(p) for p in get_active_programs(user_id)]
    settings = get_reminder_settings(user_id)
    return {"program": programs[0] if programs else None, "programs": programs, "reminders": settings}

@asynccontextmanager
async def lifespan(app):
    init_db()
    scheduler_task = asyncio.create_task(reminder_scheduler())
    backup_task = asyncio.create_task(backup_scheduler())
    polling_task = asyncio.create_task(
        dp.start_polling(bot, handle_signals=False)
    )
    logger.info("Сервис запускается на порту %s", PORT)
    try:
        yield
    finally:
        scheduler_task.cancel()
        backup_task.cancel()
        polling_task.cancel()
        await asyncio.gather(
            scheduler_task, backup_task, polling_task, return_exceptions=True
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
        try:
            save_habit(user_id, username, data.habit_name, data.duration_days, data.target_bedtime)
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
        await dispatch_program_events(user_id)
        return {"ok": ok, "message": message, **api_state_for_user(user_id)}

    if data.action == "reset":
        reset_habit(user_id, data.program_id)
        state = api_state_for_user(user_id)
        try:
            await bot.send_message(user_id, "Программа завершена. Остальные программы и отметки сохранены.", reply_markup=main_keyboard(bool(state["programs"])))
        except Exception:
            logger.exception("Не удалось обновить меню после завершения программы")
        return {"ok": True, "message": "Программа завершена. История сохранена.", **state}

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
