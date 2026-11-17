import asyncio
from datetime import datetime
import hashlib
import json
import logging
import os
import random
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BusinessConnection, Message
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# --- НАСТРОЙКИ ---
TOKEN = "9234797737:AAFzTG64sZX3KgkdMfIENlIazl-nFpzSjtE"
OWNER_ID = 7450045362
TIMEZONE = ZoneInfo("Europe/Kyiv")
DB_FILE = "morning_db.json"

# Окно отправки: с 07:00 до 07:59
START_HOUR = 7
WINDOW_MINUTES = 60

PHRASES = [
    "доброе утро, как спалось?",
    "утречко",
    "доброе, как самочуствие?",
    "доброе утро) как ты?",
    "привет, доброе утро, как спалось?",
]

bot = Bot(token=TOKEN)
dp = Dispatcher()


def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"connection_id": None, "targets": {}}


def save_db(data: dict):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения БД: {e}")


db = load_db()


# Подключение / отключение бизнес-бота
@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    if connection.user.id != OWNER_ID:
        return

    if connection.is_enabled:
        db["connection_id"] = connection.id
        save_db(db)
        await bot.send_message(
            OWNER_ID,
            "✅ Бизнес-бот подключён!\n\n"
            "Заходи в личку к нужным людям и пиши:\n"
            "• `/add` — добавить в утреннюю рассылку\n"
            "• `/del` — убрать из рассылки\n\n"
            "Команды сразу удаляются из чата, собеседник их не увидит.",
            parse_mode="Markdown",
        )
    else:
        db["connection_id"] = None
        save_db(db)
        await bot.send_message(
            OWNER_ID, "⚠️ Бизнес-бот отключён в настройках Telegram."
        )


# Секретные команды /add и /del в личных переписках
@dp.business_message()
async def on_business_message(message: Message):
    if message.chat.type != "private":
        return
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    text = (message.text or "").strip().lower()
    if text not in {"/add", "/del"}:
        return

    if message.business_connection_id:
        db["connection_id"] = message.business_connection_id

    target_id = str(message.chat.id)
    target_name = message.chat.full_name or target_id

    # Удаляем команду из чата
    try:
        await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id],
        )
    except Exception as e:
        logging.warning(f"Не удалось удалить команду: {e}")

    if text == "/add":
        db["targets"][target_id] = {
            "name": target_name,
            "last_sent": "",
        }
        save_db(db)
        await bot.send_message(
            OWNER_ID,
            f"➕ Добавил **{target_name}** в утреннюю рассылку.",
            parse_mode="Markdown",
        )
    elif text == "/del":
        if target_id in db["targets"]:
            del db["targets"][target_id]
            save_db(db)
            await bot.send_message(
                OWNER_ID,
                f"➖ Убрал **{target_name}** из рассылки.",
                parse_mode="Markdown",
            )


# Управление в личке самого бота
@dp.message(Command("start", "list"))
async def cmd_list(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    conn_status = (
        "🟢 Подключён" if db.get("connection_id") else "🔴 Не подключён"
    )
    targets = db.get("targets", {})

    if not targets:
        lines = "Список пуст. Напиши `/add` в нужном личном чате."
    else:
        lines = "\n".join(
            f"• {info['name']} (последний раз: {info['last_sent'] or 'ещё не слал'})"
            for info in targets.values()
        )

    await message.answer(
        f"**Статус:** {conn_status}\n"
        f"**Время отправки:** с {START_HOUR:02d}:00 до {START_HOUR:02d}:{WINDOW_MINUTES - 1:02d}\n\n"
        f"**Список получателей:**\n{lines}\n\n"
        "Команды: `/list` — список, `/test` — проверить отправку прямо сейчас",
        parse_mode="Markdown",
    )


@dp.message(Command("test"))
async def cmd_test(message: Message):
    if message.from_user.id != OWNER_ID:
        return

    conn_id = db.get("connection_id")
    targets = db.get("targets", {})

    if not conn_id:
        await message.answer(
            "🔴 Бот не подключён в «Telegram для бизнеса -> Чат-боты»."
        )
        return
    if not targets:
        await message.answer(
            "Список пуст. Зайди в любую личку и напиши `/add`.",
            parse_mode="Markdown",
        )
        return

    for target_id, info in list(targets.items()):
        phrase = random.choice(PHRASES)
        try:
            await bot.send_message(
                chat_id=int(target_id),
                text=phrase,
                business_connection_id=conn_id,
            )
            await message.answer(
                f"✅ Тест отправлен **{info['name']}**: «{phrase}»",
                parse_mode="Markdown",
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка для {info['name']}: {e}")


def get_daily_minute(date_str: str, user_id: str) -> int:
    digest = hashlib.md5(f"{date_str}:{user_id}".encode()).hexdigest()
    return int(digest, 16) % WINDOW_MINUTES


# Планировщик утренней рассылки
async def morning_scheduler():
    while True:
        try:
            now = datetime.now(TIMEZONE)
            today_str = now.strftime("%Y-%m-%d")
            conn_id = db.get("connection_id")

            if conn_id and START_HOUR <= now.hour < START_HOUR + 2:
                current_minutes = (now.hour - START_HOUR) * 60 + now.minute

                for target_id, info in list(db.get("targets", {}).items()):
                    if info.get("last_sent") == today_str:
                        continue

                    target_minute = get_daily_minute(today_str, target_id)
                    if current_minutes >= target_minute:
                        phrase = random.choice(PHRASES)
                        try:
                            await bot.send_message(
                                chat_id=int(target_id),
                                text=phrase,
                                business_connection_id=conn_id,
                            )
                            db["targets"][target_id]["last_sent"] = today_str
                            save_db(db)
                            await bot.send_message(
                                OWNER_ID,
                                f"📤 Отправил **{info['name']}**: «{phrase}»",
                                parse_mode="Markdown",
                            )
                        except Exception as e:
                            logging.error(f"Ошибка отправки {target_id}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в планировщике: {e}")

        await asyncio.sleep(30)


async def handle(request):
    return web.Response(text="Morning bot is alive!")


async def web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()


async def main():
    asyncio.create_task(web_server())
    asyncio.create_task(morning_scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
