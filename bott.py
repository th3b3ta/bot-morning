import asyncio
from collections import OrderedDict
from datetime import datetime
import hashlib
import html
import json
import logging
import os
import random
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message
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

# Кэш входящих сообщений для SaveMod (хранит последние 2000 сообщений)
MAX_CACHE_SIZE = 2000
msg_cache: OrderedDict[tuple[int, int], dict] = OrderedDict()

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


def extract_media(message: Message) -> tuple[str | None, str | None]:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.video_note:
        return "video_note", message.video_note.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    if message.document:
        return "document", message.document.file_id
    if message.audio:
        return "audio", message.audio.file_id
    return None, None


async def send_saved_media(
    header: str, text: str, media_type: str | None, file_id: str | None
):
    if not media_type or not file_id:
        await bot.send_message(OWNER_ID, f"{header}\n{text}", parse_mode="HTML")
        return

    caption = f"{header}\n{text}" if text else header

    if media_type == "photo":
        await bot.send_photo(
            OWNER_ID, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "video":
        await bot.send_video(
            OWNER_ID, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "voice":
        await bot.send_voice(
            OWNER_ID, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "document":
        await bot.send_document(
            OWNER_ID, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "audio":
        await bot.send_audio(
            OWNER_ID, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "video_note":
        await bot.send_message(OWNER_ID, header, parse_mode="HTML")
        await bot.send_video_note(OWNER_ID, file_id)
    elif media_type == "sticker":
        await bot.send_message(OWNER_ID, header, parse_mode="HTML")
        await bot.send_sticker(OWNER_ID, file_id)


def cache_message(message: Message):
    if not message.from_user or message.from_user.id == OWNER_ID:
        return
    if message.chat.type != "private":
        return

    media_type, file_id = extract_media(message)
    text = message.text or message.caption or ""
    if not text and not media_type:
        return

    key = (message.chat.id, message.message_id)
    msg_cache[key] = {
        "sender_name": message.from_user.full_name or str(message.from_user.id),
        "text": text,
        "media_type": media_type,
        "file_id": file_id,
    }
    if len(msg_cache) > MAX_CACHE_SIZE:
        msg_cache.popitem(last=False)


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
            "✅ Бизнес-бот подключён (рассылка + SaveMod + реплай на одноразовые)!\n\n"
            "Команды в личных чатах:\n"
            "• `/add` / `/del` — управление утренней рассылкой\n"
            "• `.` или `/save` ответом на одноразовое медиа — сохранить без палева",
            parse_mode="Markdown",
        )
    else:
        db["connection_id"] = None
        save_db(db)
        await bot.send_message(
            OWNER_ID, "⚠️ Бизнес-бот отключён в настройках Telegram."
        )


# Обработка всех сообщений в личных чатах
@dp.business_message()
async def on_business_message(message: Message):
    if message.chat.type != "private":
        return

    # 1. Входящее сообщение от собеседника -> в кэш SaveMod
    if message.from_user and message.from_user.id != OWNER_ID:
        cache_message(message)
        return

    # 2. Твои исходящие сообщения
    if not message.from_user or message.from_user.id != OWNER_ID:
        return

    if message.business_connection_id:
        db["connection_id"] = message.business_connection_id

    raw_text = (message.text or "").strip()
    cmd = raw_text.lower()

    # 3. Перехват одноразовых/скрытых медиа при реплае
    if message.reply_to_message:
        rep = message.reply_to_message
        is_stealth_trigger = cmd in {".", "/save"}
        is_uncached = (message.chat.id, rep.message_id) not in msg_cache

        if is_stealth_trigger:
            try:
                await bot.delete_business_messages(
                    business_connection_id=message.business_connection_id,
                    message_ids=[message.message_id],
                )
            except Exception as e:
                logging.warning(f"Не удалось удалить триггер реплая: {e}")

        if is_uncached or is_stealth_trigger:
            media_type, file_id = extract_media(rep)
            sender_name = (
                rep.from_user.full_name
                if rep.from_user
                else (message.chat.full_name or str(message.chat.id))
            )
            sender = html.escape(sender_name)
            rep_text = html.escape(rep.text or rep.caption or "")

            if media_type and file_id:
                header = f"🔥 <b>Сохранено по реплаю</b> от <b>{sender}</b>:"
                try:
                    await send_saved_media(header, rep_text, media_type, file_id)
                except Exception as e:
                    logging.error(f"Ошибка сохранения реплая: {e}")
            elif is_stealth_trigger:
                await bot.send_message(
                    OWNER_ID,
                    f"⚠️ Telegram не передал файл из одноразового сообщения от <b>{sender}</b> "
                    "(сервер вырезал медиа из реплая).",
                    parse_mode="HTML",
                )
        return

    # 4. Команды /add и /del для утренней рассылки
    if cmd not in {"/add", "/del"}:
        return

    target_id = str(message.chat.id)
    target_name = message.chat.full_name or target_id

    try:
        await bot.delete_business_messages(
            business_connection_id=message.business_connection_id,
            message_ids=[message.message_id],
        )
    except Exception as e:
        logging.warning(f"Не удалось удалить команду: {e}")

    if cmd == "/add":
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
    elif cmd == "/del":
        if target_id in db["targets"]:
            del db["targets"][target_id]
            save_db(db)
            await bot.send_message(
                OWNER_ID,
                f"➖ Убрал **{target_name}** из рассылки.",
                parse_mode="Markdown",
            )


# Перехват изменённых сообщений (SaveMod)
@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    if not message.from_user or message.from_user.id == OWNER_ID:
        return
    if message.chat.type != "private":
        return

    key = (message.chat.id, message.message_id)
    old_data = msg_cache.get(key)
    new_text = message.text or message.caption or ""

    if old_data and old_data["text"] and old_data["text"] != new_text:
        sender = html.escape(old_data["sender_name"])
        old_t = html.escape(old_data["text"])
        new_t = html.escape(new_text)
        try:
            await bot.send_message(
                OWNER_ID,
                f"✏️ <b>Изменено сообщение</b> от <b>{sender}</b>:\n"
                f"<b>Было:</b> {old_t}\n"
                f"<b>Стало:</b> {new_t}",
                parse_mode="HTML",
            )
        except Exception as e:
            logging.error(f"Ошибка пересылки изменённого сообщения: {e}")

    cache_message(message)


# Перехват удалённых сообщений (SaveMod)
@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    for msg_id in event.message_ids:
        data = msg_cache.pop((event.chat.id, msg_id), None)
        if not data:
            continue

        sender = html.escape(data["sender_name"])
        header = f"🗑 <b>Удалено сообщение</b> от <b>{sender}</b>:"
        text = html.escape(data["text"]) if data["text"] else ""

        try:
            await send_saved_media(
                header, text, data["media_type"], data["file_id"]
            )
        except Exception as e:
            logging.error(f"Ошибка пересылки удалённого сообщения: {e}")


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
        f"**Время отправки:** с {START_HOUR:02d}:00 до {START_HOUR:02d}:{WINDOW_MINUTES - 1:02d}\n"
        f"**SaveMod в кэше:** {len(msg_cache)} сообщ.\n\n"
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
