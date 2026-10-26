import asyncio
import logging
import sqlite3
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Токен вашего бота от BotFather
TOKEN = ""
WEB_APP_URL = "https://joyful-cannoli-d85e2a.netlify.app"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Инициализация базы данных SQLite
def init_db():
    conn = sqlite3.connect("habits.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_habits (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            habit_name TEXT,
            day_count INTEGER,
            status TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    
    # Проверяем, есть ли у пользователя уже активная привычка в базе
    conn = sqlite3.connect("habits.db")
    cursor = conn.cursor()
    cursor.execute("SELECT habit_name, day_count FROM user_habits WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    builder = InlineKeyboardBuilder()
    
    if row:
        # Если привычка уже есть, предлагаем открыть кабинет
        habit_name, day = row
        builder.button(
            text=f"📊 Мой прогресс ({habit_name})", 
            web_app=types.WebAppInfo(url=WEB_APP_URL)
        )
        await message.answer(
        f"👋 С возвращением в Лабораторию!\n\n"
        f"У тебя уже идет активный челлендж: **{habit_name}** (День {day} из 7).\n"
        "Жми кнопку ниже, чтобы посмотреть статус и сделать отметку.",
        reply_markup=builder.as_markup(),
        parse_mode="Markdown"
    )
    else:
        # Если первый раз — предлагаем выбрать привычку
        builder.button(
            text="🧪 Открыть Лабораторию привычек", 
            web_app=types.WebAppInfo(url=WEB_APP_URL)
        )
        await message.answer(
            "👋 **Добро пожаловать в Лабораторию привычек!**\n\n"
            "Выбери цель на 7 дней, брось вызов системе и прокачай дисциплину.",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown"
        )

# Обработчик данных из Mini App при выборе привычки
@dp.message(lambda msg: msg.web_app_data is not None)
async def handle_web_app_data(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "1"
    habit_choice = message.web_app_data.data

    # Сохраняем или обновляем выбор пользователя в базе данных
    conn = sqlite3.connect("habits.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO user_habits (user_id, username, habit_name, day_count, status)
        VALUES (?, ?, ?, 1, 'active')
    """, (user_id, username, habit_choice))
    conn.commit()
    conn.close()
    
    await message.answer(
        f"🎯 **Цель зафиксирована: {habit_choice}!**\n\n"
        "⚡️ *Статус:* Соло-режим (ИИ-контроль) активирован.\n"
        "📅 День 1 из 7 успешно начат. Ждем твой первый отчет завтра!",
        parse_mode="Markdown"
    )

async def main():
    print("Бот с базой данных успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())