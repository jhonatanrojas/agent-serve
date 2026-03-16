import os
import re
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

from src.agent import run_agent, cancel as cancel_agent
from src.scheduler import set_send_callback
from src.supervisor import resume_run
from src.run_state import get_latest_active_run, get_latest_run
from src.run_dashboard import build_run_dashboard, build_run_logs, build_run_plan
from src.tools import git_diff_summary
from src.llm_registry import models_status_text, get_model
from src.chat_preferences import get_preference, set_auto, set_manual

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))

_bot_app = None
_current_task = None
_current_run_id = None


def _no_preview_kwargs() -> dict:
    """Compatibilidad entre versiones de python-telegram-bot para desactivar previews."""
    try:
        from telegram import LinkPreviewOptions
        return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
    except Exception:
        return {"disable_web_page_preview": True}


def _extract_run_id(text: str) -> str | None:
    m = re.search(r"Run ID:\s*([a-f0-9\-]{8,})", text or "", flags=re.IGNORECASE)
    return m.group(1) if m else None


def _resolve_target_run_id(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit
    if _current_run_id:
        return _current_run_id
    active = get_latest_active_run()
    if active:
        return active.get("run_id")
    latest = get_latest_run()
    return latest.get("run_id") if latest else None


async def _watch_current_task(update: Update, task: asyncio.Future):
    """Espera una tarea en background y reporta resultado sin bloquear updates."""
    global _current_task, _current_run_id
    try:
        result = await task
        maybe_run = _extract_run_id(result)
        if maybe_run:
            _current_run_id = maybe_run
        await update.message.reply_text(result, **_no_preview_kwargs())
    except asyncio.CancelledError:
        await update.message.reply_text("⛔ Tarea cancelada.", **_no_preview_kwargs())
    finally:
        _current_task = None


async def _watch_current_task(update: Update, task: asyncio.Future):
    """Espera una tarea en background y reporta resultado sin bloquear updates."""
    global _current_task
    try:
        result = await task
        await update.message.reply_text(result)
    except asyncio.CancelledError:
        await update.message.reply_text("⛔ Tarea cancelada.")
    finally:
        _current_task = None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return

    if _current_task and not _current_task.done():
        await update.message.reply_text("⏳ Ya hay una tarea en ejecución. Usa /stop para cancelarla.", **_no_preview_kwargs())
        return

    user_text = update.message.text
    await update.message.reply_text("🤖 Procesando... (usa /stop para cancelar)", **_no_preview_kwargs())

    loop = asyncio.get_event_loop()

    async def progress(msg):
        await update.message.reply_text(msg, **_no_preview_kwargs())

    def run_sync():
        return run_agent(user_text, lambda m: asyncio.run_coroutine_threadsafe(progress(m), loop))

    _current_task = loop.run_in_executor(None, run_sync)
    context.application.create_task(_watch_current_task(update, _current_task))


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return

    cancel_agent()
    if _current_task and not _current_task.done():
        _current_task.cancel()
    await update.message.reply_text("⛔ Señal de cancelación enviada al agente.", **_no_preview_kwargs())


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import logging
    logging.getLogger(__name__).info(f"[CMD /status] user_id={update.effective_user.id} allowed={ALLOWED_USER}")
    if update.effective_user.id != ALLOWED_USER:
        await update.message.reply_text(f"⛔ No autorizado. Tu ID: {update.effective_user.id}", **_no_preview_kwargs())
        return
    run_id = _resolve_target_run_id(context.args[0] if context.args else None)
    if not run_id:
        await update.message.reply_text("ℹ️ No hay runs registrados.", **_no_preview_kwargs())
        return
    await update.message.reply_text(build_run_dashboard(run_id), **_no_preview_kwargs())


async def handle_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    run_id = _resolve_target_run_id(context.args[0] if context.args else None)
    if not run_id:
        await update.message.reply_text("ℹ️ No hay runs registrados.", **_no_preview_kwargs())
        return
    await update.message.reply_text(build_run_plan(run_id), **_no_preview_kwargs())


async def handle_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    run_id = _resolve_target_run_id(context.args[0] if context.args else None)
    if not run_id:
        await update.message.reply_text("ℹ️ No hay runs registrados.", **_no_preview_kwargs())
        return
    await update.message.reply_text(build_run_logs(run_id), **_no_preview_kwargs())


async def handle_diff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(git_diff_summary(30), **_no_preview_kwargs())


async def handle_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return

    if _current_task and not _current_task.done():
        await update.message.reply_text("⏳ Ya hay una tarea en ejecución. Usa /stop para cancelarla.", **_no_preview_kwargs())
        return

    run_id = context.args[0] if context.args else _resolve_target_run_id(None)
    if not run_id:
        await update.message.reply_text("Uso: /resume <run_id>", **_no_preview_kwargs())
        return

    await update.message.reply_text(f"🔄 Reanudando run `{run_id}`...", **_no_preview_kwargs())

    loop = asyncio.get_event_loop()

    def run_sync():
        return resume_run(run_id)

    _current_task = loop.run_in_executor(None, run_sync)
    context.application.create_task(_watch_current_task(update, _current_task))


async def handle_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    chat_id = update.effective_chat.id
    pref = get_preference(chat_id)
    mode_line = f"\n⚙️ Modo actual: *{pref['mode']}*"
    if pref["model_key"]:
        mode_line += f" → `{pref['model_key']}`"
    await update.message.reply_text(
        models_status_text() + mode_line,
        parse_mode="Markdown",
        **_no_preview_kwargs(),
    )


async def handle_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    chat_id = update.effective_chat.id
    arg = context.args[0] if context.args else None

    if not arg:
        pref = get_preference(chat_id)
        await update.message.reply_text(
            f"Uso: /model auto | /model <model_key>\nModo actual: {pref['mode']} {pref['model_key'] or ''}",
            **_no_preview_kwargs(),
        )
        return

    if arg == "auto":
        set_auto(chat_id)
        await update.message.reply_text("✅ Modo automático activado.", **_no_preview_kwargs())
        return

    entry = get_model(arg)
    if not entry:
        await update.message.reply_text(f"❌ Modelo `{arg}` no existe. Usa /models para ver la lista.", **_no_preview_kwargs())
        return
    if not entry.is_available:
        await update.message.reply_text(f"❌ Modelo `{arg}` no disponible (falta API key).", **_no_preview_kwargs())
        return

    set_manual(chat_id, arg)
    await update.message.reply_text(f"✅ Modelo fijado: `{arg}` ({entry.model})", **_no_preview_kwargs())


async def handle_runwith(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task, _current_run_id
    if update.effective_user.id != ALLOWED_USER:
        return

    if _current_task and not _current_task.done():
        await update.message.reply_text("⏳ Ya hay una tarea en ejecución. Usa /stop para cancelarla.", **_no_preview_kwargs())
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Uso: /runwith <model_key> <tarea>", **_no_preview_kwargs())
        return

    model_key = context.args[0]
    task_text = " ".join(context.args[1:])

    entry = get_model(model_key)
    if not entry:
        await update.message.reply_text(f"❌ Modelo `{model_key}` no existe. Usa /models.", **_no_preview_kwargs())
        return
    if not entry.is_available:
        await update.message.reply_text(f"❌ Modelo `{model_key}` no disponible (falta API key).", **_no_preview_kwargs())
        return

    await update.message.reply_text(
        f"🤖 Ejecutando con `{model_key}`: {task_text}\n(usa /stop para cancelar)",
        **_no_preview_kwargs(),
    )

    loop = asyncio.get_event_loop()

    def run_sync():
        return run_agent(task_text, mode="manual", manual_model_key=model_key)

    _current_task = loop.run_in_executor(None, run_sync)
    context.application.create_task(_watch_current_task(update, _current_task))


def main():
    global _bot_app
    _bot_app = ApplicationBuilder().token(TOKEN).build()
    _bot_app.add_handler(CommandHandler("stop", handle_stop))
    _bot_app.add_handler(CommandHandler("status", handle_status))
    _bot_app.add_handler(CommandHandler("plan", handle_plan))
    _bot_app.add_handler(CommandHandler("resume", handle_resume))
    _bot_app.add_handler(CommandHandler("logs", handle_logs))
    _bot_app.add_handler(CommandHandler("diff", handle_diff))
    _bot_app.add_handler(CommandHandler("models", handle_models))
    _bot_app.add_handler(CommandHandler("model", handle_model))
    _bot_app.add_handler(CommandHandler("runwith", handle_runwith))
    _bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def send_scheduled(msg: str):
        await _bot_app.bot.send_message(chat_id=ALLOWED_USER, text=msg, **_no_preview_kwargs())

    def sync_send(msg: str):
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(send_scheduled(msg), loop)

    set_send_callback(sync_send)

    print("🚀 Agent server corriendo... (/stop /status /plan /resume /logs /diff /models /model /runwith)")
    _bot_app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
