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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

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

gemini_client = None
if GEMINI_API_KEY:
    try:
        from google import genai
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        logger.info("Клиент Gemini инициализирован.")
    except Exception:
        logger.exception("Gemini недоступен; будут работать резервные подсказки.")


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


def get_active_program(user_id):
    with connect_db() as conn:
        return conn.execute("""
            SELECT * FROM programs
            WHERE user_id = ? AND status = 'active'
            ORDER BY id DESC LIMIT 1
        """, (user_id,)).fetchone()


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
    habit_name = habit_name.strip()[:100]
    if not habit_name:
        raise ValueError("Название привычки пустое.")
    if duration_days not in ALLOWED_DURATIONS:
        raise ValueError("Недопустимая длительность программы.")

    now = datetime.now().isoformat(timespec="seconds")
    today = date.today().isoformat()

    with connect_db() as conn:
        conn.execute("""
            UPDATE programs SET status = 'ended', ended_at = ?
            WHERE user_id = ? AND status = 'active'
        """, (now, user_id))

        conn.execute("""
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


def reset_habit(user_id):
    now = datetime.now().isoformat(timespec="seconds")
    with connect_db() as conn:
        conn.execute("""
            UPDATE programs SET status = 'reset', ended_at = ?
            WHERE user_id = ? AND status = 'active'
        """, (now, user_id))
        conn.execute("""
            UPDATE user_habits SET status = 'inactive'
            WHERE user_id = ?
        """, (user_id,))
        conn.commit()


def check_in(user_id, note=""):
    program = get_active_program(user_id)
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
            conn.execute("""
                UPDATE programs SET status = 'completed', ended_at = ?
                WHERE id = ?
            """, (now, program["id"]))
            conn.execute("""
                UPDATE user_habits SET status = 'completed'
                WHERE user_id = ?
            """, (user_id,))
            conn.commit()
        return False, "Срок программы завершён. Можно начать новую."

    try:
        with connect_db() as conn:
            conn.execute("""
                INSERT INTO daily_checkins
                (program_id, user_id, checkin_date, note, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                program["id"], user_id, today.isoformat(), note[:500],
                datetime.now().isoformat(timespec="seconds")
            ))
            conn.execute("""
                UPDATE user_habits SET day_count = ? WHERE user_id = ?
            """, (day_number, user_id))
            conn.commit()
    except sqlite3.IntegrityError:
        return False, "Сегодня отметка уже сделана."

    _, completed, percentage = get_program_progress(program)
    return True, (
        f"Отметка за день {day_number} сохранена.\n"
        f"Привычка: {program['habit_name']}\n"
        f"Отмечено дней: {completed} из {duration} ({percentage}%)."
    )


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
    """Проверяет расписание раз в 20 секунд; дубли защищены SQLite."""
    while True:
        try:
            with connect_db() as conn:
                rows = conn.execute("""
                    SELECT rs.user_id, rs.enabled, rs.reminders_per_day, rs.timezone,
                           p.habit_name, p.duration_days, p.start_date, p.id AS program_id
                    FROM reminder_settings rs
                    JOIN programs p ON p.user_id = rs.user_id AND p.status = 'active'
                    WHERE rs.enabled = 1
                """).fetchall()

            now_utc = datetime.now().astimezone()

            for row in rows:
                tz_name = valid_timezone(row["timezone"])
                local_now = now_utc.astimezone(ZoneInfo(tz_name))
                local_time = local_now.strftime("%H:%M")
                local_date = local_now.date().isoformat()

                times = REMINDER_SCHEDULES.get(
                    int(row["reminders_per_day"]), ["20:00"]
                )
                if local_time not in times:
                    continue

                program_start = date.fromisoformat(row["start_date"])
                day_number = (local_now.date() - program_start).days + 1
                if day_number < 1 or day_number > int(row["duration_days"]):
                    continue

                with connect_db() as conn:
                    already_sent = conn.execute("""
                        SELECT 1 FROM sent_reminders
                        WHERE user_id = ? AND reminder_date = ? AND reminder_time = ?
                    """, (row["user_id"], local_date, local_time)).fetchone()

                    if already_sent:
                        continue

                    # Записываем попытку до отправки, чтобы избежать повторов
                    # после перезапуска. При ошибке запись удаляем.
                    conn.execute("""
                        INSERT INTO sent_reminders
                        (user_id, reminder_date, reminder_time, sent_at)
                        VALUES (?, ?, ?, ?)
                    """, (
                        row["user_id"], local_date, local_time,
                        datetime.now().isoformat(timespec="seconds")
                    ))
                    conn.commit()

                try:
                    await bot.send_message(
                        row["user_id"],
                        f"⏰ Напоминание из «Лаборатории привычек»\n\n"
                        f"Твоя цель: {row['habit_name']}\n"
                        f"День {day_number} из {row['duration_days']}.\n\n"
                        "Если выполнил действие, открой Mini App и отметь день."
                    )
                except Exception:
                    logger.exception("Не удалось отправить напоминание пользователю %s",
                                     row["user_id"])
                    with connect_db() as conn:
                        conn.execute("""
                            DELETE FROM sent_reminders
                            WHERE user_id = ? AND reminder_date = ? AND reminder_time = ?
                        """, (row["user_id"], local_date, local_time))
                        conn.commit()

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка планировщика напоминаний.")

        await asyncio.sleep(20)


# ============================================================
# НАСТАВНИК
# ============================================================

def fallback_advice(habit_name, user_message=""):
    text = user_message.lower()

    if any(w in text for w in ("забыл", "пропустил", "сорвал", "не сделал")):
        return (
            "Не нужно компенсировать пропуск чрезмерной нагрузкой. "
            "Вернись к привычке при ближайшей возможности и продолжай "
            "фиксировать реальные результаты."
        )
    if any(w in text for w in ("лень", "тяжело", "нет сил", "не хочется")):
        return (
            "Уменьши действие до самого простого выполнимого шага. "
            "Сейчас важнее устойчивость, чем идеальный объём."
        )
    return (
        f"По привычке «{habit_name}»: выбери конкретное действие на сегодня, "
        "привяжи его к понятному моменту дня и вечером честно отметь результат."
    )


async def ask_mentor(user_id, user_message):
    program = get_active_program(user_id)
    habit_name = program["habit_name"] if program else "не выбрана"

    if not gemini_client:
        return fallback_advice(habit_name, user_message), False

    prompt = (
        "Ты — ИИ-наставник приложения «Лаборатория привычек». "
        "Отвечай по-русски, конкретно, без морализаторства и медицинских диагнозов. "
        "Не обещай гарантированного формирования привычки за фиксированный срок.\n"
        f"Привычка: {habit_name}\n"
        f"Сообщение пользователя: {user_message[:2000]}\n"
        "Ответь практично, обычно в 3–6 предложениях."
    )

    try:
        response = await asyncio.wait_for(
            asyncio.to_thread(
                gemini_client.models.generate_content,
                model=GEMINI_MODEL,
                contents=prompt,
            ),
            timeout=25,
        )
        answer = (response.text or "").strip()
        if answer:
            with connect_db() as conn:
                now = datetime.now().isoformat(timespec="seconds")
                conn.execute("""
                    INSERT INTO mentor_history (user_id, role, message, created_at)
                    VALUES (?, 'user', ?, ?)
                """, (user_id, user_message[:2000], now))
                conn.execute("""
                    INSERT INTO mentor_history (user_id, role, message, created_at)
                    VALUES (?, 'assistant', ?, ?)
                """, (user_id, answer[:5000], now))
                conn.commit()
            return answer, True
    except Exception:
        logger.exception("Ошибка Gemini.")

    return fallback_advice(habit_name, user_message), False


# ============================================================
# TELEGRAM-БОТ
# ============================================================

def main_keyboard(has_program=False):
    builder = InlineKeyboardBuilder()
    if WEB_APP_URL:
        builder.button(
            text="🧪 Открыть Лабораторию",
            web_app=types.WebAppInfo(url=WEB_APP_URL),
        )
    if has_program:
        builder.button(text="📊 Мой прогресс", callback_data="progress")
        builder.button(text="✅ Отметить сегодня", callback_data="checkin")
        builder.button(text="🧠 Спросить наставника", callback_data="mentor")
        builder.button(text="🔄 Завершить программу", callback_data="reset")
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
    program = get_active_program(message.from_user.id)
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
    await message.answer(text, reply_markup=main_keyboard(bool(program)))


@dp.message(Command("progress"))
async def cmd_progress(message: types.Message):
    program = get_active_program(message.from_user.id)
    await message.answer(
        progress_text(program) if program
        else "Активной программы нет. Открой Mini App."
    )


@dp.message(Command("checkin"))
async def cmd_checkin(message: types.Message):
    _, result = check_in(message.from_user.id)
    await message.answer(result)


@dp.message(Command("reset"))
async def cmd_reset(message: types.Message):
    reset_habit(message.from_user.id)
    await message.answer("Программа завершена. История сохранена.",
                         reply_markup=main_keyboard(False))


@dp.message(Command("mentor"))
async def cmd_mentor(message: types.Message):
    question = message.text.partition(" ")[2].strip()
    if not question:
        await message.answer(
            "Напиши вопрос после команды, например:\n"
            "/mentor Как вернуться к привычке после пропуска?"
        )
        return
    answer, used_ai = await ask_mentor(message.from_user.id, question)
    prefix = "🧠 ИИ-наставник\n\n" if used_ai else "💡 Автоматическая подсказка\n\n"
    await message.answer(prefix + answer)


@dp.callback_query(F.data == "progress")
async def cb_progress(callback: types.CallbackQuery):
    program = get_active_program(callback.from_user.id)
    await callback.message.answer(
        progress_text(program) if program else "Активной программы нет."
    )
    await callback.answer()


@dp.callback_query(F.data == "checkin")
async def cb_checkin(callback: types.CallbackQuery):
    ok, result = check_in(callback.from_user.id)
    await callback.message.answer(result)
    await callback.answer("Готово" if ok else "Проверь сообщение")


@dp.callback_query(F.data == "reset")
async def cb_reset(callback: types.CallbackQuery):
    reset_habit(callback.from_user.id)
    await callback.message.answer(
        "Программа завершена. История сохранена.",
        reply_markup=main_keyboard(False),
    )
    await callback.answer("Программа завершена")


@dp.callback_query(F.data == "mentor")
async def cb_mentor(callback: types.CallbackQuery):
    await callback.message.answer(
        "Напиши вопрос командой:\n"
        "/mentor Как не бросить привычку?"
    )
    await callback.answer()


# ============================================================
# API MINI APP
# ============================================================

class InitDataRequest(BaseModel):
    initData: str = Field(min_length=1)


class ActionRequest(InitDataRequest):
    action: str
    habit_name: str = ""
    duration_days: int = 7
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


def api_state_for_user(user_id):
    program = get_active_program(user_id)
    settings = get_reminder_settings(user_id)

    if not program:
        return {"program": None, "reminders": settings}

    day, completed, percentage = get_program_progress(program)
    with connect_db() as conn:
        checked_today = conn.execute("""
            SELECT 1 FROM daily_checkins
            WHERE program_id = ? AND checkin_date = ?
        """, (program["id"], date.today().isoformat())).fetchone() is not None

    return {
        "program": {
            "habit_name": program["habit_name"],
            "duration_days": int(program["duration_days"]),
            "start_date": program["start_date"],
            "day_number": day,
            "completed": completed,
            "percentage": percentage,
            "checked_today": checked_today,
        },
        "reminders": settings,
    }


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
        if gemini_client:
            try:
                gemini_client.close()
            except Exception:
                pass


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
        return {"ok": True, "message": "Программа начата.", **api_state_for_user(user_id)}

    if data.action == "checkin":
        ok, message = check_in(user_id, data.note)
        return {"ok": ok, "message": message, **api_state_for_user(user_id)}

    if data.action == "reset":
        reset_habit(user_id)
        return {"ok": True, "message": "Программа завершена. История сохранена.",
                **api_state_for_user(user_id)}

    if data.action == "mentor":
        answer, used_ai = await ask_mentor(user_id, data.message.strip())
        return {"ok": True, "message": answer, "used_ai": used_ai,
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