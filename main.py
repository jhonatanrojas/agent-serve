import os
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from src.agent import run_agent

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return

    user_text = update.message.text
    await update.message.reply_text("🤖 Procesando...")

    def progress(msg):
        asyncio.create_task(update.message.reply_text(msg))

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: run_agent(user_text, progress))
    await update.message.reply_text(result)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("🚀 Agent server corriendo...")
    app.run_polling()


if __name__ == "__main__":
    main()
