import os
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from src.agent import run_agent

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))
WEBHOOK_PATH = f"/webhook/{TOKEN}"
WEBHOOK_URL = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return

    user_text = update.message.text
    await update.message.reply_text("🤖 Procesando...")

    async def progress(msg):
        await update.message.reply_text(msg)

    def run_sync():
        return run_agent(user_text, lambda m: asyncio.run_coroutine_threadsafe(progress(m), loop))

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, run_sync)
    await update.message.reply_text(result)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print(f"🚀 Agent server corriendo con webhook en {WEBHOOK_URL}")
    app.run_webhook(
        listen="0.0.0.0",
        port=WEBHOOK_PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=WEBHOOK_URL,
        cert="/root/agent-serve/webhook.crt",
        key="/root/agent-serve/webhook.key",
    )


if __name__ == "__main__":
    main()
