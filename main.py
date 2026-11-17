# Python version: 3.11.9 (see .python-version). Works on 3.11+.
import asyncio
import logging
import os
import sys
import threading
import time
import urllib.request

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
# Only this Telegram user id may use /start and /stop (get your id from @userinfobot)
ADMIN_ID = int(os.environ["ADMIN_ID"])

# Which service messages to delete
DELETE_JOIN_MESSAGES = True   # "X joined the group"
DELETE_LEFT_MESSAGES = True   # "X left the group"

# ---------- Database ----------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo[MONGO_DB]
settings = db["chat_settings"]
settings.create_index([("chat_id", ASCENDING)], unique=True)
# Users this bot promoted to "title-only" admin (only they may use /edittag)
promoted = db["promoted_users"]
promoted.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)

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


def _db_mark_promoted(chat_id: int, user_id: int) -> None:
    promoted.update_one(
        {"chat_id": chat_id, "user_id": user_id},
        {"$set": {"chat_id": chat_id, "user_id": user_id}},
        upsert=True,
    )


def _db_is_promoted(chat_id: int, user_id: int) -> bool:
    return promoted.find_one({"chat_id": chat_id, "user_id": user_id}, {"_id": 1}) is not None


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
def is_bot_owner(update: Update) -> bool:
    """Only the single user whose id is in the ADMIN_ID env var may control the bot."""
    user = update.effective_user
    return user is not None and user.id == ADMIN_ID


async def safe_reply(update: Update, text: str) -> None:
    try:
        await update.effective_message.reply_text(text)
    except TelegramError as exc:
        log.warning("Reply failed in chat %s: %s", update.effective_chat.id, exc)


# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, "ဒီ command ကို bot ပိုင်ရှင် admin တစ်ယောက်တည်းသာ သုံးနိုင်ပါတယ်။")
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
    if not is_bot_owner(update):
        await safe_reply(update, "ဒီ command ကို bot ပိုင်ရှင် admin တစ်ယောက်တည်းသာ သုံးနိုင်ပါတယ်။")
        return

    try:
        await set_enabled(update.effective_chat.id, False)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, "ခဏ ပြဿနာရှိနေပါတယ်။ ခဏနေ ပြန်ကြိုးစားပါ။")
        return

    await safe_reply(update, "bot ရပ်နားလိုက်ပါပြီ ⏸")


async def cmd_edittag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/edittag <text> : change your own tag (admin custom title). Only for users the bot promoted."""
    chat = update.effective_chat
    user = update.effective_user
    if user is None:
        return

    try:
        allowed = await asyncio.to_thread(_db_is_promoted, chat.id, user.id)
    except Exception:
        log.exception("DB read failed for chat %s", chat.id)
        await safe_reply(update, "ခဏ ပြဿနာရှိနေပါတယ်။ ခဏနေ ပြန်ကြိုးစားပါ။")
        return
    if not allowed:
        await safe_reply(update, "group ထဲကို member တစ်ယောက် ထည့်ပေးထားမှ /edittag ကို သုံးလို့ရပါမယ်။")
        return

    tag = " ".join(context.args).strip() if context.args else ""
    if not tag:
        await safe_reply(update, "အသုံးပြုပုံ - /edittag အရာရာစိတ်ကြိုက်")
        return
    if len(tag) > 16:
        await safe_reply(update, "Tag က စာလုံး ၁၆ လုံးထက် မကျော်ရပါဘူး။")
        return

    try:
        await context.bot.set_chat_administrator_custom_title(chat.id, user.id, tag)
    except BadRequest as exc:
        log.warning("Custom title failed in chat %s: %s", chat.id, exc)
        await safe_reply(update, "Tag ပြောင်းလို့မရပါ (emoji မသုံးရပါ၊ ၁၆ လုံးအတွင်းဖြစ်ရပါမယ်)။")
        return
    except TelegramError as exc:
        log.warning("Custom title failed in chat %s: %s", chat.id, exc)
        await safe_reply(update, "Tag ပြောင်းလို့မရပါ။ ခဏနေ ပြန်ကြိုးစားပါ။")
        return

    await safe_reply(update, f"Tag ကို \"{tag}\" သို့ ပြောင်းပေးလိုက်ပါပြီ ✅")


# ---------- Inviter reward: bare admin ----------
async def promote_inviter(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """If a user added someone else to the group, make the adder an admin with NO real rights.

    The Bot API treats "all rights False" as a demotion, so we pass can_manage_chat=True:
    it is the baseline right every admin has (it is implied by any other right) and gives
    nothing that matters here: no delete / ban / invite / pin / change info / add admins.
    """
    adder = message.from_user
    chat = message.chat
    if adder is None or adder.is_bot:
        return
    # Joining by yourself (e.g. via invite link) does not count; someone else must be added.
    if not any(m.id != adder.id and not m.is_bot for m in message.new_chat_members):
        return

    try:
        member = await context.bot.get_chat_member(chat.id, adder.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return  # already an admin / the owner: nothing to do
        await context.bot.promote_chat_member(
            chat.id,
            adder.id,
            is_anonymous=False,
            can_manage_chat=True,
        )
        await asyncio.to_thread(_db_mark_promoted, chat.id, adder.id)
        log.info("Promoted user %s to title-only admin in chat %s", adder.id, chat.id)
    except TelegramError:
        # Most likely the bot lacks the "Add New Admins" right, or this is not a supergroup.
        log.exception("Could not promote user %s in chat %s", adder.id, chat.id)
    except Exception:
        log.exception("Unexpected error promoting user %s in chat %s", adder.id, chat.id)


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


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if not await is_enabled(chat.id):
        return

    await promote_inviter(message, context)  # before the join message is deleted
    if DELETE_JOIN_MESSAGES:
        await delete_message(message)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    if DELETE_LEFT_MESSAGES and await is_enabled(chat.id):
        await delete_message(message)


# ---------- Keepalive (stops Render free instance from sleeping) ----------
KEEPALIVE_INTERVAL = 10 * 60  # seconds; Render sleeps after ~15 min without traffic


def keepalive_loop() -> None:
    """Ping our own public URL regularly so Render counts it as traffic."""
    url = PUBLIC_URL.rstrip("/") + "/"
    log.info("Keepalive started, pinging %s every %ds", url, KEEPALIVE_INTERVAL)
    while True:
        time.sleep(KEEPALIVE_INTERVAL)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                log.debug("Keepalive ping status %s", resp.status)
        except Exception as exc:  # 404 from webhook root is still a successful wake-up
            if hasattr(exc, "code"):
                log.debug("Keepalive ping got HTTP %s (service is awake)", exc.code)
            else:
                log.warning("Keepalive ping failed: %s", exc)


# ---------- Main ----------
def main() -> None:
    log.info("Python version: %s", sys.version.split()[0])

    threading.Thread(target=keepalive_loop, name="keepalive", daemon=True).start()

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
    app.add_handler(CommandHandler("edittag", cmd_edittag, filters=filters.ChatType.GROUPS))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, on_new_members)
    )
    app.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER & filters.ChatType.GROUPS, on_left_member)
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
