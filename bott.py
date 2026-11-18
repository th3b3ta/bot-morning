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
from aiogram.filters import Command, CommandObject
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, Message
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# --- НАСТРОЙКИ ---
TOKEN = "9234797737:AAFzTG64sZX3KgkdMfIENlIazl-nFpzSjtE"
ADMIN_ID = 7450045362
FRIEND_ID = 7095861311
ALLOWED_USERS = {
    ADMIN_ID: "Ты",
    FRIEND_ID: "Кент",
}

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

# Кэш входящих сообщений для SaveMod (хранит последние 3000 сообщений на всех)
MAX_CACHE_SIZE = 3000
msg_cache: OrderedDict[tuple[int, int, int], dict] = OrderedDict()

bot = Bot(token=TOKEN)
dp = Dispatcher()


def default_db() -> dict:
    return {
        "users": {
            str(uid): {"name": label, "connection_id": None, "targets": {}}
            for uid, label in ALLOWED_USERS.items()
        }
    }


def load_db() -> dict:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "users" in data:
                    for uid, label in ALLOWED_USERS.items():
                        if str(uid) not in data["users"]:
                            data["users"][str(uid)] = {
                                "name": label,
                                "connection_id": None,
                                "targets": {},
                            }
                    return data
        except Exception:
            pass
    return default_db()


def save_db(data: dict):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка сохранения БД: {e}")


db = load_db()


def ensure_user(uid: int, full_name: str | None = None):
    uid_str = str(uid)
    if uid_str not in db["users"]:
        db["users"][uid_str] = {
            "name": ALLOWED_USERS.get(uid, str(uid)),
            "connection_id": None,
            "targets": {},
        }
    if full_name:
        prefix = "Ты" if uid == ADMIN_ID else "Кент"
        db["users"][uid_str]["name"] = f"{prefix} ({full_name})"


async def resolve_owner_by_connection(conn_id: str | None) -> int | None:
    if not conn_id:
        return None
    for uid_str, udata in db.get("users", {}).items():
        if udata.get("connection_id") == conn_id:
            return int(uid_str)

    # Если Render перезапустился и забыл connection_id — восстанавливаем через API
    try:
        conn = await bot.get_business_connection(conn_id)
        if (
            conn
            and conn.user
            and conn.user.id in ALLOWED_USERS
            and conn.is_enabled
        ):
            uid = conn.user.id
            ensure_user(uid, conn.user.full_name)
            db["users"][str(uid)]["connection_id"] = conn_id
            save_db(db)
            return uid
    except Exception as e:
        logging.warning(f"Не удалось восстановить connection {conn_id}: {e}")

    return None


def format_user_link(
    user_id: int | None, name: str, username: str | None = None
) -> str:
    escaped_name = html.escape(name)
    if username:
        return f'<b><a href="https://t.me/{username}">{escaped_name}</a></b>'
    if user_id:
        return f'<b><a href="tg://user?id={user_id}">{escaped_name}</a></b>'
    return f"<b>{escaped_name}</b>"


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
    recipient_id: int,
    header: str,
    text: str,
    media_type: str | None,
    file_id: str | None,
):
    if not media_type or not file_id:
        await bot.send_message(
            recipient_id,
            f"{header}\n{text}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    caption = f"{header}\n{text}" if text else header

    if media_type == "photo":
        await bot.send_photo(
            recipient_id, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "video":
        await bot.send_video(
            recipient_id, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "voice":
        await bot.send_voice(
            recipient_id, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "document":
        await bot.send_document(
            recipient_id, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "audio":
        await bot.send_audio(
            recipient_id, file_id, caption=caption, parse_mode="HTML"
        )
    elif media_type == "video_note":
        await bot.send_message(
            recipient_id,
            header,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await bot.send_video_note(recipient_id, file_id)
    elif media_type == "sticker":
        await bot.send_message(
            recipient_id,
            header,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        await bot.send_sticker(recipient_id, file_id)


def cache_message(account_owner_id: int, message: Message):
    if not message.from_user or message.from_user.id == account_owner_id:
        return
    if message.chat.type != "private":
        return

    media_type, file_id = extract_media(message)
    text = message.text or message.caption or ""
    if not text and not media_type:
        return

    key = (account_owner_id, message.chat.id, message.message_id)
    msg_cache[key] = {
        "sender_id": message.from_user.id,
        "sender_name": message.from_user.full_name or str(message.from_user.id),
        "sender_username": message.from_user.username,
        "text": text,
        "media_type": media_type,
        "file_id": file_id,
    }
    if len(msg_cache) > MAX_CACHE_SIZE:
        msg_cache.popitem(last=False)


# Подключение / отключение бизнес-бота
@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    uid = connection.user.id
    if uid not in ALLOWED_USERS:
        return

    ensure_user(uid, connection.user.full_name)
    uid_str = str(uid)

    if connection.is_enabled:
        db["users"][uid_str]["connection_id"] = connection.id
        save_db(db)
        await bot.send_message(
            uid,
            "✅ <b>Бизнес-бот подключён</b> (утренняя рассылка + SaveMod)!\n\n"
            "Команды в личных чатах:\n"
            "• <code>/add</code> — добавить чат в утреннюю рассылку\n"
            "• <code>/del</code> — убрать из рассылки\n"
            "• <code>.</code> или <code>/save</code> реплаем на одноразовое медиа — сохранить без палева",
            parse_mode="HTML",
        )
        if uid != ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"🔗 Кент (<b>{html.escape(connection.user.full_name)}</b>) подключил свой аккаунт к боту!",
                parse_mode="HTML",
            )
    else:
        db["users"][uid_str]["connection_id"] = None
        save_db(db)
        await bot.send_message(
            uid, "⚠️ Бизнес-бот отключён в настройках Telegram."
        )
        if uid != ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ Кент (<b>{html.escape(connection.user.full_name)}</b>) отключил бизнес-бота.",
                parse_mode="HTML",
            )


# Обработка всех сообщений в личных чатах
@dp.business_message()
async def on_business_message(message: Message):
    if message.chat.type != "private":
        return

    conn_id = message.business_connection_id
    account_owner_id = await resolve_owner_by_connection(conn_id)

    if (
        not account_owner_id
        and message.from_user
        and message.from_user.id in ALLOWED_USERS
    ):
        account_owner_id = message.from_user.id
        if conn_id:
            ensure_user(account_owner_id, message.from_user.full_name)
            db["users"][str(account_owner_id)]["connection_id"] = conn_id
            save_db(db)

    if not account_owner_id or account_owner_id not in ALLOWED_USERS:
        return

    # 1. Входящее сообщение от собеседника -> в кэш SaveMod владельца аккаунта
    if message.from_user and message.from_user.id != account_owner_id:
        cache_message(account_owner_id, message)
        return

    # 2. Исходящие сообщения от владельца аккаунта (тебя или кента)
    if not message.from_user or message.from_user.id != account_owner_id:
        return

    raw_text = (message.text or "").strip()
    cmd = raw_text.lower()

    # 3. Перехват одноразовых/скрытых медиа при реплае
    if message.reply_to_message:
        rep = message.reply_to_message
        is_stealth_trigger = cmd in {".", "/save"}
        is_uncached = (
            account_owner_id,
            message.chat.id,
            rep.message_id,
        ) not in msg_cache

        if is_stealth_trigger and conn_id:
            try:
                await bot.delete_business_messages(
                    business_connection_id=conn_id,
                    message_ids=[message.message_id],
                )
            except Exception as e:
                logging.warning(f"Не удалось удалить триггер реплая: {e}")

        if is_uncached or is_stealth_trigger:
            media_type, file_id = extract_media(rep)
            sender_id = rep.from_user.id if rep.from_user else message.chat.id
            sender_name = (
                rep.from_user.full_name
                if rep.from_user
                else (message.chat.full_name or str(message.chat.id))
            )
            sender_username = (
                rep.from_user.username
                if rep.from_user
                else message.chat.username
            )
            sender_link = format_user_link(
                sender_id, sender_name, sender_username
            )
            rep_text = html.escape(rep.text or rep.caption or "")

            if media_type and file_id:
                header = f"🔥 <b>Сохранено по реплаю</b> от {sender_link}:"
                try:
                    await send_saved_media(
                        account_owner_id, header, rep_text, media_type, file_id
                    )
                except Exception as e:
                    logging.error(f"Ошибка сохранения реплая: {e}")
            elif is_stealth_trigger:
                await bot.send_message(
                    account_owner_id,
                    f"⚠️ Telegram не передал файл из одноразового сообщения от {sender_link} "
                    "(сервер вырезал медиа из реплая).",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
        return

    # 4. Команда .spam <количество> <текст>
    if cmd.startswith(".spam "):
        parts = raw_text.split(" ", 2)
        if len(parts) < 3:
            await bot.send_message(account_owner_id, "⚠️ Формат: <code>.spam 5 текст</code>", parse_mode="HTML")
            return
        try:
            count = int(parts[1])
        except ValueError:
            await bot.send_message(account_owner_id, "⚠️ Количество должно быть числом. Пример: <code>.spam 5 привет</code>", parse_mode="HTML")
            return
        count = max(1, min(count, 100))
        spam_text = parts[2]
        if conn_id:
            try:
                await bot.delete_business_messages(
                    business_connection_id=conn_id,
                    message_ids=[message.message_id],
                )
            except Exception as e:
                logging.warning(f"Не удалось удалить .spam команду: {e}")
        sent = 0
        for _ in range(count):
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=spam_text,
                    business_connection_id=conn_id,
                )
                sent += 1
                await asyncio.sleep(0.3)
            except Exception as e:
                logging.error(f"Ошибка спама: {e}")
                break
        await bot.send_message(
            account_owner_id,
            f"✅ Спам выполнен: <b>{sent}/{count}</b> сообщений отправлено.",
            parse_mode="HTML",
        )
        return

    # 5. Команды /add и /del для утренней рассылки
    if cmd not in {"/add", "/del"}:
        return

    target_id = str(message.chat.id)
    target_name = message.chat.full_name or target_id
    uid_str = str(account_owner_id)

    if conn_id:
        try:
            await bot.delete_business_messages(
                business_connection_id=conn_id,
                message_ids=[message.message_id],
            )
        except Exception as e:
            logging.warning(f"Не удалось удалить команду: {e}")

    safe_target = html.escape(target_name)

    if cmd == "/add":
        db["users"][uid_str]["targets"][target_id] = {
            "name": target_name,
            "last_sent": "",
        }
        save_db(db)
        await bot.send_message(
            account_owner_id,
            f"➕ Добавил <b>{safe_target}</b> в утреннюю рассылку.",
            parse_mode="HTML",
        )
        if account_owner_id != ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"👤 Кент добавил <b>{safe_target}</b> (<code>{target_id}</code>) в свою рассылку.",
                parse_mode="HTML",
            )
    elif cmd == "/del":
        if target_id in db["users"][uid_str]["targets"]:
            del db["users"][uid_str]["targets"][target_id]
            save_db(db)
            await bot.send_message(
                account_owner_id,
                f"➖ Убрал <b>{safe_target}</b> из рассылки.",
                parse_mode="HTML",
            )
            if account_owner_id != ADMIN_ID:
                await bot.send_message(
                    ADMIN_ID,
                    f"👤 Кент убрал <b>{safe_target}</b> (<code>{target_id}</code>) из своей рассылки.",
                    parse_mode="HTML",
                )


# Перехват изменённых сообщений (SaveMod)
@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    if message.chat.type != "private":
        return

    account_owner_id = await resolve_owner_by_connection(
        message.business_connection_id
    )
    if (
        not account_owner_id
        or not message.from_user
        or message.from_user.id == account_owner_id
    ):
        return

    key = (account_owner_id, message.chat.id, message.message_id)
    old_data = msg_cache.get(key)
    new_text = message.text or message.caption or ""

    if old_data and old_data["text"] and old_data["text"] != new_text:
        sender_link = format_user_link(
            old_data.get("sender_id"),
            old_data["sender_name"],
            old_data.get("sender_username"),
        )
        old_t = html.escape(old_data["text"])
        new_t = html.escape(new_text)
        try:
            await bot.send_message(
                account_owner_id,
                f"✏️ <b>Изменено сообщение</b> от {sender_link}:\n"
                f"<b>Было:</b> {old_t}\n"
                f"<b>Стало:</b> {new_t}",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logging.error(f"Ошибка пересылки изменённого сообщения: {e}")

    cache_message(account_owner_id, message)


# Перехват удалённых сообщений (SaveMod)
@dp.deleted_business_messages()
async def on_deleted_business_messages(event: BusinessMessagesDeleted):
    account_owner_id = await resolve_owner_by_connection(
        event.business_connection_id
    )
    if not account_owner_id:
        return

    for msg_id in event.message_ids:
        data = msg_cache.pop((account_owner_id, event.chat.id, msg_id), None)
        if not data:
            continue

        sender_link = format_user_link(
            data.get("sender_id"),
            data["sender_name"],
            data.get("sender_username"),
        )
        header = f"🗑 <b>Удалено сообщение</b> от {sender_link}:"
        text = html.escape(data["text"]) if data["text"] else ""

        try:
            await send_saved_media(
                account_owner_id, header, text, data["media_type"], data["file_id"]
            )
        except Exception as e:
            logging.error(f"Ошибка пересылки удалённого сообщения: {e}")


def format_targets_block(targets: dict, show_ids: bool = False) -> str:
    if not targets:
        return "<i>Список пуст</i>"
    lines = []
    for tid, info in targets.items():
        safe_name = html.escape(info["name"])
        last = info["last_sent"] or "ещё не слал"
        if show_ids:
            lines.append(f"• {safe_name} (<code>{tid}</code>) — {last}")
        else:
            lines.append(f"• {safe_name} ({last})")
    return "\n".join(lines)


# Управление в личке самого бота
@dp.message(Command("start", "list"))
async def cmd_list(message: Message):
    uid = message.from_user.id
    if uid not in ALLOWED_USERS:
        return

    if uid == ADMIN_ID:
        my_data = db["users"][str(ADMIN_ID)]
        fr_data = db["users"][str(FRIEND_ID)]

        my_status = (
            "🟢 Подключён" if my_data.get("connection_id") else "🔴 Не подключён"
        )
        fr_status = (
            "🟢 Подключён" if fr_data.get("connection_id") else "🔴 Не подключён"
        )

        my_cache = sum(1 for k in msg_cache if k[0] == ADMIN_ID)
        fr_cache = sum(1 for k in msg_cache if k[0] == FRIEND_ID)

        text = (
            f"⏰ <b>Окно отправки:</b> с {START_HOUR:02d}:00 до {START_HOUR:02d}:{WINDOW_MINUTES - 1:02d}\n\n"
            f"👤 <b>{html.escape(my_data['name'])}</b> — {my_status} (SaveMod: {my_cache})\n"
            f"{format_targets_block(my_data['targets'], show_ids=True)}\n\n"
            f"🤝 <b>{html.escape(fr_data['name'])}</b> — {fr_status} (SaveMod: {fr_cache})\n"
            f"{format_targets_block(fr_data['targets'], show_ids=True)}\n\n"
            "<b>Управление кентом:</b>\n"
            "• <code>/fdel ID</code> — убрать чат у кента\n"
            "• <code>/fadd ID Имя</code> — добавить чат кенту\n"
            "• <code>/ftest</code> — тест рассылки кента\n"
            "• <code>/test</code> — тест твоей рассылки"
        )
        await message.answer(text, parse_mode="HTML")
    else:
        udata = db["users"][str(uid)]
        status = (
            "🟢 Подключён" if udata.get("connection_id") else "🔴 Не подключён"
        )
        my_cache = sum(1 for k in msg_cache if k[0] == uid)
        text = (
            f"<b>Статус:</b> {status}\n"
            f"<b>Время отправки:</b> с {START_HOUR:02d}:00 до {START_HOUR:02d}:{WINDOW_MINUTES - 1:02d}\n"
            f"<b>SaveMod в кэше:</b> {my_cache} сообщ.\n\n"
            f"<b>Твой список получателей:</b>\n"
            f"{format_targets_block(udata['targets'], show_ids=False)}\n\n"
            "В любой личке пиши <code>/add</code> или <code>/del</code>. Проверка: <code>/test</code>"
        )
        await message.answer(text, parse_mode="HTML")


# Админские команды для управления чатами кента
@dp.message(Command("fdel"))
async def cmd_friend_del(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Укажи ID чата из списка, например: <code>/fdel 123456789</code>",
            parse_mode="HTML",
        )
        return

    target_id = command.args.strip().split()[0]
    fr_targets = db["users"][str(FRIEND_ID)]["targets"]
    if target_id in fr_targets:
        name = html.escape(fr_targets[target_id]["name"])
        del fr_targets[target_id]
        save_db(db)
        await message.answer(
            f"➖ Убрал <b>{name}</b> (<code>{target_id}</code>) из списка кента.",
            parse_mode="HTML",
        )
    else:
        await message.answer("❌ Такого ID нет в списке кента.")


@dp.message(Command("fadd"))
async def cmd_friend_add(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await message.answer(
            "Формат: <code>/fadd 123456789 Имя</code>", parse_mode="HTML"
        )
        return

    parts = command.args.strip().split(maxsplit=1)
    target_id = parts[0]
    if not target_id.lstrip("-").isdigit():
        await message.answer("❌ ID чата должен состоять из цифр.")
        return

    target_name = parts[1] if len(parts) > 1 else target_id
    db["users"][str(FRIEND_ID)]["targets"][target_id] = {
        "name": target_name,
        "last_sent": "",
    }
    save_db(db)
    await message.answer(
        f"➕ Добавил <b>{html.escape(target_name)}</b> (<code>{target_id}</code>) в список кента.",
        parse_mode="HTML",
    )


async def run_test_for_user(target_uid: int, reply_to_uid: int):
    udata = db["users"][str(target_uid)]
    conn_id = udata.get("connection_id")
    targets = udata.get("targets", {})

    if not conn_id:
        await bot.send_message(
            reply_to_uid,
            "🔴 Аккаунт не подключён в «Telegram для бизнеса -> Чат-боты».",
        )
        return
    if not targets:
        await bot.send_message(
            reply_to_uid,
            "Список пуст. Сначала добавь чаты через <code>/add</code>.",
            parse_mode="HTML",
        )
        return

    for target_id, info in list(targets.items()):
        phrase = random.choice(PHRASES)
        safe_name = html.escape(info["name"])
        try:
            await bot.send_message(
                chat_id=int(target_id),
                text=phrase,
                business_connection_id=conn_id,
            )
            await bot.send_message(
                reply_to_uid,
                f"✅ Тест отправлен <b>{safe_name}</b>: «{html.escape(phrase)}»",
                parse_mode="HTML",
            )
        except Exception as e:
            await bot.send_message(
                reply_to_uid,
                f"❌ Ошибка для <b>{safe_name}</b>: {html.escape(str(e))}",
                parse_mode="HTML",
            )


@dp.message(Command("test"))
async def cmd_test(message: Message):
    uid = message.from_user.id
    if uid not in ALLOWED_USERS:
        return
    await run_test_for_user(uid, uid)


@dp.message(Command("ftest"))
async def cmd_friend_test(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await run_test_for_user(FRIEND_ID, ADMIN_ID)


def get_daily_minute(date_str: str, seed_key: str) -> int:
    digest = hashlib.md5(f"{date_str}:{seed_key}".encode()).hexdigest()
    return int(digest, 16) % WINDOW_MINUTES


# Планировщик утренней рассылки для всех подключённых пользователей
async def morning_scheduler():
    while True:
        try:
            now = datetime.now(TIMEZONE)
            today_str = now.strftime("%Y-%m-%d")

            if START_HOUR <= now.hour < START_HOUR + 2:
                current_minutes = (now.hour - START_HOUR) * 60 + now.minute

                for uid_str, udata in list(db.get("users", {}).items()):
                    conn_id = udata.get("connection_id")
                    if not conn_id:
                        continue

                    owner_uid = int(uid_str)
                    for target_id, info in list(
                        udata.get("targets", {}).items()
                    ):
                        if info.get("last_sent") == today_str:
                            continue

                        target_minute = get_daily_minute(
                            today_str, f"{uid_str}:{target_id}"
                        )
                        if current_minutes >= target_minute:
                            phrase = random.choice(PHRASES)
                            safe_name = html.escape(info["name"])
                            try:
                                await bot.send_message(
                                    chat_id=int(target_id),
                                    text=phrase,
                                    business_connection_id=conn_id,
                                )
                                db["users"][uid_str]["targets"][target_id][
                                    "last_sent"
                                ] = today_str
                                save_db(db)

                                await bot.send_message(
                                    owner_uid,
                                    f"📤 Отправил <b>{safe_name}</b>: «{html.escape(phrase)}»",
                                    parse_mode="HTML",
                                )
                                if owner_uid != ADMIN_ID:
                                    await bot.send_message(
                                        ADMIN_ID,
                                        f"📤 [Кент] Отправил <b>{safe_name}</b>: «{html.escape(phrase)}»",
                                        parse_mode="HTML",
                                    )
                            except Exception as e:
                                logging.error(
                                    f"Ошибка отправки ({uid_str} -> {target_id}): {e}"
                                )
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
