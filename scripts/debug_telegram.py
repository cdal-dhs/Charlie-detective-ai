import asyncio, sys, traceback
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

try:
    from app.config import get_settings
except Exception:
    traceback.print_exc()
    sys.exit(1)

settings = get_settings()
print(f"TOKEN: {settings.telegram_bot_token[:20]}...", file=sys.stderr)
print(f"CHAT_ID: {settings.telegram_chat_id}", file=sys.stderr)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bonjour ! Charlie est en ligne.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Agent opérationnel sur 3 boîtes.")

async def main():
    print("BUILDING APP", file=sys.stderr)
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    print("INITIALIZING", file=sys.stderr)
    await app.initialize()
    await app.start()
    print("STARTING POLLING", file=sys.stderr)
    await app.updater.start_polling(drop_pending_updates=True)
    print("POLLING ACTIVE", file=sys.stderr)
    await asyncio.sleep(30)
    print("STOPPING", file=sys.stderr)

try:
    asyncio.run(main())
except Exception:
    traceback.print_exc()
