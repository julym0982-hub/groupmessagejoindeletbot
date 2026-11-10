# Python version: 3.11.9 (see .python-version). Works on 3.11+.
import asyncio
import logging
import os
import sys

from pymongo import ASCENDING, MongoClient
from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("joinbot")

# ---------- Config (Render Environment Variables) ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGODB_URI"]
MONGO_DB = os.environ.get("MONGODB_DB", "joinbot")
PUBLIC_URL = os.environ["RENDER_EXTERNAL_URL"]  # Render sets this automatically
PORT = int(os.environ.get("PORT", "10000"))

# Which service messages to delete
DELETE_JOIN_MESSAGES = True   # "X joined the group"
DELETE_LEFT_MESSAGES = True   # "X left the group"

# ---------- Database ----------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo[MONGO_DB]
settings = db["chat_settings"]
settings.create_index([("chat_id", ASCENDING)], unique=True)

# In-memory cache: chat_id -> enabled. Avoids a DB round-trip for every service message.
_enabled_cache: dict[int, bool] = {}


def _db_get_enabled(chat_id: int) -> bool:
    doc = settings.find_one({"chat_id": chat_id}, {"enabled": 1})
    return bool(doc and doc.get("enabled"))


def _db_set_enabled(chat_id: int, value: bool) -> None:
    settings.update_one(
        {"chat_id": chat_id},
        {"$set": {"enabled": value}},
        upsert=True,
    )


async def is_enabled(chat_id: int) -> bool:
    if chat_id in _enabled_cache:
        return _enabled_cache[chat_id]
    try:
        value = await asyncio.to_thread(_db_get_enabled, chat_id)
    except Exception:
        log.exception("DB read failed for chat %s", chat_id)
        return False  # fail safe: do nothing if the DB is unreachable
    _enabled_cache[chat_id] = value
    return value


async def set_enabled(chat_id: int, value: bool) -> None:
    await asyncio.to_thread(_db_set_enabled, chat_id, value)
    _enabled_cache[chat_id] = value


# ---------- Helpers ----------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.effective_message

    # Anonymous admin: the message is sent "as the group" (sender_chat == chat)
    if message and message.sender_chat and message.sender_chat.id == chat.id:
        return True

    user = update.effective_user
    if user is None:
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError:
        log.exception("get_chat_member failed in chat %s", chat.id)
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def safe_reply(update: Update, text: str) -> None:
    try:
        await update.effective_message.reply_text(text)
    except TelegramError as exc:
        log.warning("Reply failed in chat %s: %s", update.effective_chat.id, exc)


# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await safe_reply(update, "ဒီ command ကို admin တွေသာ သုံးနိုင်ပါတယ်။")
        return

    try:
        await set_enabled(update.effective_chat.id, True)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, "ခဏ ပြဿနာရှိနေပါတယ်။ ခဏနေ ပြန်ကြိုးစားပါ။")
        return

    await safe_reply(
        update,
        "bot အလုပ်လုပ်နေပါပြီ ✅\n"
        "group ထဲက joined / left စာသားတွေကို အလိုအလျောက် ဖျက်ပေးပါမယ်။",
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await safe_reply(update, "ဒီ command ကို admin တွေသာ သုံးနိုင်ပါတယ်။")
        return

    try:
        await set_enabled(update.effective_chat.id, False)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, "ခဏ ပြဿနာရှိနေပါတယ်။ ခဏနေ ပြန်ကြိုးစားပါ။")
        return

    await safe_reply(update, "bot ရပ်နားလိုက်ပါပြီ ⏸")


# ---------- Service message cleaner ----------
async def delete_message(message) -> None:
    """Delete one message. Handles flood limits and expected errors quietly."""
    for _ in range(3):
        try:
            await message.delete()
            return
        except RetryAfter as exc:
            wait = exc.retry_after
            seconds = wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
            log.info("Flood limit in chat %s, waiting %.1fs", message.chat_id, seconds)
            await asyncio.sleep(min(seconds, 30))
        except BadRequest as exc:
            # Already deleted, too old (48h+), or not deletable: nothing more to do
            log.info("Skip delete in chat %s: %s", message.chat_id, exc)
            return
        except Forbidden as exc:
            log.warning("No delete permission in chat %s: %s", message.chat_id, exc)
            return
        except TelegramError as exc:
            log.warning("Delete failed in chat %s: %s", message.chat_id, exc)
            return


async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if not await is_enabled(chat.id):
        return

    await delete_message(message)


service_filter = None
if DELETE_JOIN_MESSAGES:
    service_filter = filters.StatusUpdate.NEW_CHAT_MEMBERS
if DELETE_LEFT_MESSAGES:
    left = filters.StatusUpdate.LEFT_CHAT_MEMBERS
    service_filter = left if service_filter is None else service_filter | left


# ---------- Main ----------
def main() -> None:
    log.info("Python version: %s", sys.version.split()[0])

    # Newer Python versions (e.g. 3.14) need an explicit event loop for PTB 21.x
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Preload enabled groups so restarts don't cause DB lookups per message
    try:
        for doc in settings.find({"enabled": True}, {"chat_id": 1}):
            _enabled_cache[doc["chat_id"]] = True
        log.info("Loaded %d enabled chat(s)", len(_enabled_cache))
    except Exception:
        log.exception("Could not preload settings (will load on demand)")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # handle many join/leave events at the same time
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=filters.ChatType.GROUPS))
    if service_filter is not None:
        app.add_handler(
            MessageHandler(service_filter & filters.ChatType.GROUPS, on_service_message)
        )

    # Webhook mode works on Render's web service (it needs an open port)
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{PUBLIC_URL}/{BOT_TOKEN}",
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
