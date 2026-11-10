import logging
import os

from pymongo import ASCENDING, MongoClient
from telegram import Update
from telegram.constants import ChatMemberStatus
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

# Set to True if you also want "left the group" messages deleted
DELETE_LEFT_MESSAGES = False

# ---------- Database ----------
mongo = MongoClient(MONGO_URI)
db = mongo[MONGO_DB]
settings = db["chat_settings"]
settings.create_index([("chat_id", ASCENDING)], unique=True)


def is_enabled(chat_id: int) -> bool:
    doc = settings.find_one({"chat_id": chat_id}, {"enabled": 1})
    return bool(doc and doc.get("enabled"))


def set_enabled(chat_id: int, value: bool) -> None:
    settings.update_one(
        {"chat_id": chat_id},
        {"$set": {"enabled": value}},
        upsert=True,
    )


# ---------- Helpers ----------
async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    member = await context.bot.get_chat_member(
        update.effective_chat.id, update.effective_user.id
    )
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


# ---------- Commands ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.effective_message.reply_text(
            "ဒီ command ကို admin တွေသာ သုံးနိုင်ပါတယ်။"
        )
        return

    set_enabled(update.effective_chat.id, True)
    await update.effective_message.reply_text(
        "bot အလုပ်လုပ်နေပါပြီ ✅\n"
        "group ထဲက joined group စာသားတွေကို အလိုအလျောက် ဖျက်ပေးပါမယ်။"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin(update, context):
        await update.effective_message.reply_text(
            "ဒီ command ကို admin တွေသာ သုံးနိုင်ပါတယ်။"
        )
        return

    set_enabled(update.effective_chat.id, False)
    await update.effective_message.reply_text("bot ရပ်နားလိုက်ပါပြီ ⏸")


# ---------- Service message cleaner ----------
async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_enabled(chat_id):
        return

    try:
        await update.effective_message.delete()
    except Exception as exc:  # missing rights, message too old, already deleted, etc.
        log.warning("Could not delete message %s in %s: %s",
                    update.effective_message.message_id, chat_id, exc)


service_filter = filters.StatusUpdate.NEW_CHAT_MEMBERS
if DELETE_LEFT_MESSAGES:
    service_filter = service_filter | filters.StatusUpdate.LEFT_CHAT_MEMBERS


# ---------- Main ----------
def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start, filters=filters.ChatType.GROUPS))
    app.add_handler(CommandHandler("stop", cmd_stop, filters=filters.ChatType.GROUPS))
    app.add_handler(MessageHandler(service_filter & filters.ChatType.GROUPS, on_service_message))

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
