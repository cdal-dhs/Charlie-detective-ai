"""Script de démarrage du bot Telegram seul."""
import asyncio
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import get_settings

settings = get_settings()

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bonjour ! Charlie est en ligne.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Agent opérationnel. Polling actif sur 3 boîtes.")

async def main():
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    print("Bot polling started...")
    import time
    while True:
        time.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
