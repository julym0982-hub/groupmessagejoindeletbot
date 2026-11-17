# Python version: 3.11.9 (see .python-version). Works on 3.11+.
import asyncio
import logging
import os
import sys
import threading
import time
import urllib.request

from pymongo import ASCENDING, MongoClient
from telegram import ChatMemberUpdated, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import mention_html

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
# Only this Telegram user id may control the bot (get your id from @userinfobot)
ADMIN_ID = int(os.environ["ADMIN_ID"])

DEFAULT_WELCOME = "{name} ကို group မှ ကြိုဆိုပါတယ် 🎉"

# ---------- Database ----------
mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo[MONGO_DB]
settings = db["chat_settings"]
settings.create_index([("chat_id", ASCENDING)], unique=True)
# Users this bot promoted to "title-only" admin (only they may use /edittag)
promoted = db["promoted_users"]
promoted.create_index([("chat_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
# Bot-wide config (welcome text)
config = db["bot_config"]

# In-memory caches: avoid a DB round-trip for every service message.
_chat_cache: dict[int, dict] = {}
_welcome_cache: dict[str, str | None] = {}  # {"html": ...} once loaded
# Owner's pending input in private chat: user_id -> "formatted" | "html"
_awaiting: dict[int, str] = {}


def _state_from_doc(doc: dict | None) -> dict:
    doc = doc or {}
    enabled = bool(doc.get("enabled"))
    # Older versions had one flag that meant "delete"; keep that behaviour until it is changed.
    delete = bool(doc.get("delete", enabled))
    return {"enabled": enabled, "delete": delete}


def _db_get_chat(chat_id: int) -> dict:
    return _state_from_doc(settings.find_one({"chat_id": chat_id}))


def _db_save_chat(chat_id: int, state: dict) -> None:
    settings.update_one(
        {"chat_id": chat_id},
        {"$set": {"enabled": state["enabled"], "delete": state["delete"]}},
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


def _db_get_welcome() -> str | None:
    doc = config.find_one({"_id": "welcome"})
    return doc.get("html") if doc else None


def _db_set_welcome(html_text: str | None) -> None:
    if html_text is None:
        config.delete_one({"_id": "welcome"})
    else:
        config.update_one({"_id": "welcome"}, {"$set": {"html": html_text}}, upsert=True)


async def get_chat(chat_id: int) -> dict:
    if chat_id in _chat_cache:
        return _chat_cache[chat_id]
    try:
        state = await asyncio.to_thread(_db_get_chat, chat_id)
    except Exception:
        log.exception("DB read failed for chat %s", chat_id)
        return {"enabled": False, "delete": False}  # fail safe: do nothing if the DB is down
    _chat_cache[chat_id] = state
    return state


async def update_chat(chat_id: int, **changes) -> None:
    state = dict(await get_chat(chat_id))
    state.update(changes)
    await asyncio.to_thread(_db_save_chat, chat_id, state)
    _chat_cache[chat_id] = state


async def get_welcome() -> str:
    if "html" not in _welcome_cache:
        try:
            _welcome_cache["html"] = await asyncio.to_thread(_db_get_welcome)
        except Exception:
            log.exception("DB read failed for welcome text")
            return DEFAULT_WELCOME
    return _welcome_cache["html"] or DEFAULT_WELCOME


async def set_welcome(html_text: str | None) -> None:
    await asyncio.to_thread(_db_set_welcome, html_text)
    _welcome_cache["html"] = html_text


def render_welcome(template_html: str, mentions_html: str) -> str:
    """Put the mention(s) in the welcome text. {name} marks the place; without it, mention goes first."""
    if "{name}" in template_html:
        return template_html.replace("{name}", mentions_html)
    return f"{mentions_html}\n{template_html}"


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


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """DM the bot owner (works once the owner has pressed /start in the bot's private chat)."""
    try:
        await context.bot.send_message(ADMIN_ID, text)
    except TelegramError as exc:
        log.warning("Could not notify owner: %s", exc)


NOT_OWNER_TEXT ="ဒီ command ကို bot ပိုင်ရှင် admin တစ်ယောက်တည်းသာ သုံးနိုင်ပါတယ်။"
DB_ERROR_TEXT = "ခဏ ပြဿနာရှိနေပါတယ်။ ခဏနေ ပြန်ကြိုးစားပါ။"

HELP_TEXT = """👋 ဒီ bot ကို ပိုင်ရှင် admin တစ်ယောက်တည်းသာ ထိန်းချုပ်နိုင်ပါတယ်။

━━ Group ထဲမှာ သုံးရမယ့် command ━━
/start – bot ကို group မှာ စတင်အလုပ်လုပ်ခိုင်းမယ် (join ဝင်သူကို ကြိုဆိုမယ်၊ member ထည့်ပေးသူကို admin tag ပေးမယ်)
/stop – bot ကို အကုန်ရပ်မယ်
/startdelete – "joined / left" notification စာတွေကို ဖျက်မယ် (ဒီအချိန်မှာ ကြိုဆိုစကား မပြောပါ)
/stopdelete – notification ဖျက်တာကို ရပ်မယ် (ဒီအချိန်မှာမှ ကြိုဆိုစကား ပြောမယ်)
/edittag စာသား – member ထည့်ပေးထားသူက ကိုယ့် tag ကို ပြောင်းမယ်
   ဥပမာ - /edittag အရာရာစိတ်ကြိုက်

━━ ဒီ chat (bot ဆီ) မှာ သုံးရမယ့် command ━━
/setwelcome – ကြိုဆိုစကား ပြင်မယ်။ ပြီးရင် စာကို ဆက်ပို့ပါ။ Telegram ထဲမှာ bold / italic / spoiler / emoji နဲ့ ရေးထားတဲ့အတိုင်း ပို့တဲ့အတိုင်း အတိအကျ သိမ်းပေးမယ်။
/sethtml – HTML tag နဲ့ ကိုယ်တိုင်ရေးမယ်။ ပြီးရင် tag တွေပါတဲ့ စာကို ဆက်ပို့ပါ။
/showwelcome – လက်ရှိ ကြိုဆိုစကားကို ကြည့်မယ်
/resetwelcome – မူလ ကြိုဆိုစကားကို ပြန်ထားမယ်
/cancel – ပြင်နေတာကို ပယ်ဖျက်မယ်

━━ ကြိုဆိုစကား ရေးနည်း ━━
၁) {name} လို့ ရေးထားတဲ့နေရာမှာ ဝင်လာသူကို mention လုပ်ပေးမယ်။ {name} မပါရင် စာရဲ့ အပေါ်ဆုံးမှာ mention ထည့်ပေးမယ်။
၂) /setwelcome သုံးရင် Telegram ရဲ့ ပုံမှန် formatting (bold, italic, spoiler, link, emoji) ကို အတိုင်းသုံးပါ။
၃) <b> လို tag နဲ့ ရေးချင်ရင် /sethtml ကိုသုံးပါ။
   <b>bold</b>   <i>italic</i>   <u>underline</u>   <s>strike</s>
   <tg-spoiler>spoiler</tg-spoiler>   <code>code</code>
   <a href="https://example.com">link</a>
   <blockquote>quote</blockquote>
   ဥပမာ - {name} ကို <b>ကြိုဆိုပါတယ်</b> 🎉 <tg-spoiler>စည်းကမ်းတွေ ဖတ်ပါ</tg-spoiler>
၄) ကြိုဆိုစကားက စာသားချည်းပဲ (ပုံ/ဗီဒီယို မရပါ)။"""


# ---------- Group commands (bot owner only) ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    try:
        await update_chat(update.effective_chat.id, enabled=True)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, DB_ERROR_TEXT)
        return

    state = await get_chat(update.effective_chat.id)
    if state["delete"]:
        mode = "joined / left စာတွေကို ဖျက်နေပါမယ် (ကြိုဆိုစကား မပြောပါ)။"
    else:
        mode = "ဝင်လာသူကို ကြိုဆိုစကား ပြောပေးပါမယ်။ (notification ဖျက်ချင်ရင် /startdelete)"
    await safe_reply(update, "bot အလုပ်လုပ်နေပါပြီ ✅\n" + mode)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    try:
        await update_chat(update.effective_chat.id, enabled=False)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, DB_ERROR_TEXT)
        return
    await safe_reply(update, "bot ရပ်နားလိုက်ပါပြီ ⏸")


async def cmd_startdelete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    try:
        await update_chat(update.effective_chat.id, enabled=True, delete=True)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, DB_ERROR_TEXT)
        return
    await safe_reply(
        update,
        "joined / left စာတွေကို အလိုအလျောက် ဖျက်ပေးပါမယ် ✅\n"
        "(ဒီအချိန်မှာ ကြိုဆိုစကား မပြောပါ။ ပြောစေချင်ရင် /stopdelete)",
    )


async def cmd_stopdelete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    try:
        await update_chat(update.effective_chat.id, delete=False)
    except Exception:
        log.exception("Could not save settings for chat %s", update.effective_chat.id)
        await safe_reply(update, DB_ERROR_TEXT)
        return
    state = await get_chat(update.effective_chat.id)
    if state["enabled"]:
        await safe_reply(update, "notification ဖျက်တာ ရပ်လိုက်ပါပြီ ⏸\nအခု ဝင်လာသူကို ကြိုဆိုစကား ပြောပေးပါမယ် ✅")
    else:
        await safe_reply(update, "notification ဖျက်တာ ရပ်လိုက်ပါပြီ ⏸\n(ကြိုဆိုစကားပြောဖို့ /start လည်း လိုပါတယ်)")


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
        await safe_reply(update, DB_ERROR_TEXT)
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


# ---------- Private chat: welcome text editor (bot owner only) ----------
async def pm_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    _awaiting.pop(update.effective_user.id, None)
    await safe_reply(update, HELP_TEXT)


async def pm_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    _awaiting[update.effective_user.id] = "formatted"
    await safe_reply(
        update,
        "ကြိုဆိုစကားကို အခု ပို့ပါ ✍️\n"
        "bold / italic / spoiler / emoji တွေ ပါရင် ပို့တဲ့အတိုင်း သိမ်းပေးမယ်။\n"
        "{name} နေရာမှာ ဝင်လာသူကို mention လုပ်ပေးမယ်။\n"
        "မလုပ်တော့ရင် /cancel",
    )


async def pm_sethtml(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    _awaiting[update.effective_user.id] = "html"
    await safe_reply(
        update,
        "HTML tag တွေပါတဲ့ ကြိုဆိုစကားကို အခု ပို့ပါ ✍️\n"
        "ဥပမာ - {name} ကို <b>ကြိုဆိုပါတယ်</b> 🎉\n"
        "မလုပ်တော့ရင် /cancel",
    )


async def pm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    _awaiting.pop(update.effective_user.id, None)
    await safe_reply(update, "ပယ်ဖျက်လိုက်ပါပြီ။")


async def pm_showwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    user = update.effective_user
    text = render_welcome(await get_welcome(), mention_html(user.id, user.full_name))
    try:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.warning("Preview failed: %s", exc)
        await safe_reply(update, "Preview ပြလို့မရပါ။ /setwelcome နဲ့ ပြန်ပြင်ပါ။")


async def pm_resetwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(update):
        await safe_reply(update, NOT_OWNER_TEXT)
        return
    try:
        await set_welcome(None)
    except Exception:
        log.exception("Could not reset welcome text")
        await safe_reply(update, DB_ERROR_TEXT)
        return
    await safe_reply(update, "မူလ ကြိုဆိုစကားကို ပြန်ထားလိုက်ပါပြီ ✅")


async def pm_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner's plain message in private chat: saved as the new welcome text if /setwelcome or /sethtml was used."""
    if not is_bot_owner(update):
        return
    message = update.effective_message
    user = update.effective_user
    mode = _awaiting.get(user.id)
    if mode is None:
        await safe_reply(update, "Command တွေ ကြည့်ဖို့ /start ကို နှိပ်ပါ။")
        return

    # "formatted": keep exactly what the admin sent (bold, spoiler, emoji...). "html": admin typed tags.
    new_html = message.text_html if mode == "formatted" else message.text

    preview = render_welcome(new_html, mention_html(user.id, user.full_name))
    try:
        await message.reply_text(preview, parse_mode=ParseMode.HTML)  # also validates the HTML
    except BadRequest as exc:
        log.info("Welcome text rejected: %s", exc)
        await safe_reply(
            update,
            f"ဒီစာကို သိမ်းလို့မရပါ - tag မှားနေပါတယ် ({exc})\nပြင်ပြီး ပြန်ပို့ပါ၊ ဒါမှမဟုတ် /cancel",
        )
        return
    except TelegramError:
        log.exception("Welcome preview failed")
        await safe_reply(update, DB_ERROR_TEXT)
        return

    try:
        await set_welcome(new_html)
    except Exception:
        log.exception("Could not save welcome text")
        await safe_reply(update, DB_ERROR_TEXT)
        return
    _awaiting.pop(user.id, None)
    await safe_reply(update, "ကြိုဆိုစကား သိမ်းပြီးပါပြီ ✅ (အပေါ်က preview အတိုင်း ပြောပါမယ်)")


# ---------- Inviter reward: bare admin ----------
async def promote_inviter(chat, adder, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Make the user who added someone else an admin with NO real rights.

    The Bot API treats "all rights False" as a demotion, so we pass can_manage_chat=True:
    it is the baseline right every admin has (it is implied by any other right) and gives
    nothing that matters here: no delete / ban / invite / pin / change info / add admins.
    """
    try:
        member = await context.bot.get_chat_member(chat.id, adder.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            # Admins that someone else promoted can't be given a title by the bot.
            log.info("Promote skipped in chat %s: user %s is already %s", chat.id, adder.id, member.status)
            return
        await context.bot.promote_chat_member(
            chat.id,
            adder.id,
            is_anonymous=False,
            can_manage_chat=True,
        )
        await asyncio.to_thread(_db_mark_promoted, chat.id, adder.id)
        log.info("Promoted user %s to title-only admin in chat %s", adder.id, chat.id)
    except Exception as exc:
        # Most likely the bot lacks the "Add New Admins" right, or this is not a supergroup.
        log.exception("Could not promote user %s in chat %s", adder.id, chat.id)
        await notify_owner(
            context,
            f"⚠️ {adder.full_name} ({adder.id}) ကို admin ခန့်လို့မရပါ\n"
            f"Group: {chat.title} ({chat.id})\n"
            f"အကြောင်းရင်း: {type(exc).__name__}: {exc}",
        )


# ---------- Join / leave handling ----------
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


async def send_welcome(chat_id: int, users, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = [u for u in users if not u.is_bot]
    if not users:
        return
    mentions = ", ".join(mention_html(u.id, u.full_name) for u in users)
    text = render_welcome(await get_welcome(), mentions)
    try:
        await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        log.warning("Welcome failed in chat %s: %s", chat_id, exc)


def _is_in_chat(member) -> bool:
    if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return True
    return member.status == ChatMemberStatus.RESTRICTED and bool(getattr(member, "is_member", False))


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Someone joined (or was added). Telegram sends these updates even in big groups,
    where the "X joined" service message is not sent at all. Needs the bot to be an admin."""
    cmu: ChatMemberUpdated = update.chat_member
    if cmu is None:
        return
    chat = cmu.chat
    user = cmu.new_chat_member.user
    actor = cmu.from_user
    joined = (not _is_in_chat(cmu.old_chat_member)) and _is_in_chat(cmu.new_chat_member)
    if not joined or user.is_bot:
        return

    state = await get_chat(chat.id)
    log.info(
        "Join event in chat %s: enabled=%s delete=%s user=%s added_by=%s",
        chat.id, state["enabled"], state["delete"], user.id, actor.id if actor else None,
    )
    if not state["enabled"]:
        return

    # Added by another person (joining by yourself / via link does not count)
    if actor is not None and not actor.is_bot and actor.id != user.id:
        await promote_inviter(chat, actor, context)

    if not state["delete"]:  # /stopdelete mode: say welcome
        await send_welcome(chat.id, [user], context)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The "X joined" service message (small groups only): delete it in /startdelete mode."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    state = await get_chat(chat.id)
    if state["enabled"] and state["delete"]:
        await delete_message(message)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    state = await get_chat(chat.id)
    if state["enabled"] and state["delete"]:
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

    # Preload settings so restarts don't cause DB lookups per message
    try:
        for doc in settings.find({}):
            _chat_cache[doc["chat_id"]] = _state_from_doc(doc)
        log.info("Loaded settings for %d chat(s)", len(_chat_cache))
    except Exception:
        log.exception("Could not preload settings (will load on demand)")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # handle many join/leave events at the same time
        .build()
    )

    groups = filters.ChatType.GROUPS
    private = filters.ChatType.PRIVATE

    # Group commands
    app.add_handler(CommandHandler("start", cmd_start, filters=groups))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=groups))
    app.add_handler(CommandHandler("startdelete", cmd_startdelete, filters=groups))
    app.add_handler(CommandHandler("stopdelete", cmd_stopdelete, filters=groups))
    app.add_handler(CommandHandler("edittag", cmd_edittag, filters=groups))

    # Private chat (welcome text editor)
    app.add_handler(CommandHandler("start", pm_start, filters=private))
    app.add_handler(CommandHandler("help", pm_start, filters=private))
    app.add_handler(CommandHandler("setwelcome", pm_setwelcome, filters=private))
    app.add_handler(CommandHandler("sethtml", pm_sethtml, filters=private))
    app.add_handler(CommandHandler("showwelcome", pm_showwelcome, filters=private))
    app.add_handler(CommandHandler("resetwelcome", pm_resetwelcome, filters=private))
    app.add_handler(CommandHandler("cancel", pm_cancel, filters=private))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, pm_text))

    # Join events (promote + welcome); works in big groups too
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Join / leave service messages (delete only)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & groups, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER & groups, on_left_member))

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
