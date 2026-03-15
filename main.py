import os
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from src.agent import run_agent, cancel as cancel_agent
from src.scheduler import set_send_callback

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))

_bot_app = None
_current_task = None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return

    user_text = update.message.text
    await update.message.reply_text("🤖 Procesando... (usa /stop para cancelar)")

    loop = asyncio.get_event_loop()

    async def progress(msg):
        await update.message.reply_text(msg)

    def run_sync():
        return run_agent(user_text, lambda m: asyncio.run_coroutine_threadsafe(progress(m), loop))

    _current_task = loop.run_in_executor(None, run_sync)
    try:
        result = await _current_task
        await update.message.reply_text(result)
    except asyncio.CancelledError:
        await update.message.reply_text("⛔ Tarea cancelada.")
    finally:
        _current_task = None


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    cancel_agent()
    await update.message.reply_text("⛔ Señal de cancelación enviada al agente.")


def main():
    global _bot_app
    _bot_app = ApplicationBuilder().token(TOKEN).build()
    _bot_app.add_handler(CommandHandler("stop", handle_stop))
    _bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def send_scheduled(msg: str):
        await _bot_app.bot.send_message(chat_id=ALLOWED_USER, text=msg)

    def sync_send(msg: str):
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(send_scheduled(msg), loop)

    set_send_callback(sync_send)

    print("🚀 Agent server corriendo... (/stop para cancelar tareas)")
    _bot_app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
