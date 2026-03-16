import asyncio
import os
import re
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from src.agent import run_agent, cancel as cancel_agent
from src.repo_manager import RepoManager
from src.run_dashboard import build_run_dashboard, build_run_logs, build_run_plan
from src.run_state import get_latest_active_run, get_latest_run
from src.scheduler import set_send_callback
from src.supervisor import resume_run, run_supervisor
from src.task_provider_notion import NotionTaskProvider
from src.tools import git_diff_summary
from src.llm_registry import models_status_text, get_model
from src.chat_preferences import get_preference, set_auto, set_manual
from src.llm_runner import stats_text
from src.workspace_context import set_active_repo_path
from src.workspace_manager import WorkspaceError, WorkspaceManager

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))

_bot_app = None
_current_task = None
_current_run_id = None


def _no_preview_kwargs() -> dict:
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


def _parse_kv_args(args: list[str]) -> dict:
    parsed = {}
    for token in args:
        if "=" in token:
            k, v = token.split("=", 1)
            parsed[k.strip()] = v.strip()
    return parsed


def _set_workspace_context(chat_id: int):
    ws = WorkspaceManager().get_active_workspace(chat_id)
    set_active_repo_path(ws["repo_path"])
    return ws


async def _watch_current_task(update: Update, task: asyncio.Future):
    global _current_task, _current_run_id
    try:
        result = await task
        maybe_run = _extract_run_id(result)
        if maybe_run:
            _current_run_id = maybe_run
        await update.message.reply_text(result, **_no_preview_kwargs())
        await update.message.reply_text("Esta tarea quedó finalizada. ¿Deseas que continúe con la siguiente?", **_no_preview_kwargs())
    except asyncio.CancelledError:
        await update.message.reply_text("⛔ Tarea cancelada.", **_no_preview_kwargs())
    finally:
        _current_task = None


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return
    if _current_task and not _current_task.done():
        await update.message.reply_text("⏳ Ya hay una tarea en ejecución. Usa /stop para cancelarla.", **_no_preview_kwargs())
        return
    ws = _set_workspace_context(update.effective_chat.id)
    user_text = update.message.text
    await update.message.reply_text(f"🤖 Procesando en `{ws['repo_path']}`...", **_no_preview_kwargs())

    loop = asyncio.get_event_loop()

    async def progress(msg):
        await update.message.reply_text(msg, **_no_preview_kwargs())

    def run_sync():
        return run_agent(user_text, lambda m: asyncio.run_coroutine_threadsafe(progress(m), loop))

    _current_task = loop.run_in_executor(None, run_sync)
    context.application.create_task(_watch_current_task(update, _current_task))


async def handle_workon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    params = _parse_kv_args(context.args)
    repo_url = params.get("repo", "")
    notion_id = params.get("notion", "")
    branch = params.get("branch", "")
    if not repo_url or not branch:
        await update.message.reply_text("Uso: /workon repo=<url> notion=<database_id> branch=<branch>", **_no_preview_kwargs())
        return
    try:
        ws = WorkspaceManager().set_active_workspace(update.effective_chat.id, repo_url, notion_id, branch)
        set_active_repo_path(ws["repo_path"])
        await update.message.reply_text(
            f"✅ Workspace activo\nrepo={ws['repo_path']}\nbranch={ws['active_branch']}\nnotion={ws['notion_database_id'] or '-'}",
            **_no_preview_kwargs(),
        )
    except WorkspaceError as e:
        await update.message.reply_text(f"❌ {e}", **_no_preview_kwargs())


async def handle_plan_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    provider = NotionTaskProvider()
    tasks = provider.list_tasks(ws.get("notion_database_id", ""))
    repo_hint = ws.get("repo_url", "")
    eligible = [t for t in tasks if not t.repo_hint or (repo_hint and t.repo_hint.lower() in repo_hint.lower())]
    if not eligible:
        await update.message.reply_text("No hay tareas elegibles para el repo activo.", **_no_preview_kwargs())
        return
    lines = ["🗂️ Tareas elegibles:"]
    for t in eligible[:20]:
        lines.append(f"- {t.id[:8]} | {t.title} | status={t.status or 'N/A'}")
    await update.message.reply_text("\n".join(lines), **_no_preview_kwargs())


async def handle_do_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return
    if _current_task and not _current_task.done():
        await update.message.reply_text("⏳ Ya hay una tarea en ejecución.", **_no_preview_kwargs())
        return
    if not context.args:
        await update.message.reply_text("Uso: /do_task <task_id>", **_no_preview_kwargs())
        return

    ws = _set_workspace_context(update.effective_chat.id)
    provider = NotionTaskProvider()
    tasks = provider.list_tasks(ws.get("notion_database_id", ""))
    task_id = context.args[0]
    task = next((t for t in tasks if t.id.startswith(task_id) or t.id == task_id), None)
    if not task:
        await update.message.reply_text("Task no encontrada en Notion.", **_no_preview_kwargs())
        return

    branch = RepoManager(ws["repo_path"]).ensure_task_branch(task.id[:8])
    WorkspaceManager().set_active_branch(update.effective_chat.id, branch)

    provider.update_task_status(task.page_id, "In progress")
    await update.message.reply_text(f"🚀 Ejecutando tarea {task.id[:8]} en branch `{branch}`", **_no_preview_kwargs())
    loop = asyncio.get_event_loop()

    def run_sync():
        try:
            return run_supervisor(f"{task.title}\n\n{task.description}")
        finally:
            pass

    _current_task = loop.run_in_executor(None, run_sync)

    async def finalize_and_watch():
        result = await _current_task
        status = "Done" if "Tarea completada" in result else "Blocked"
        provider.update_task_status(task.page_id, status)
        await update.message.reply_text(result, **_no_preview_kwargs())
        await update.message.reply_text("Esta tarea quedó finalizada. ¿Deseas que continúe con la siguiente?", **_no_preview_kwargs())

    context.application.create_task(finalize_and_watch())


async def handle_do_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    provider = NotionTaskProvider()
    tasks = provider.list_tasks(ws.get("notion_database_id", ""))
    repo_hint = ws.get("repo_url", "")
    eligible = [t for t in tasks if (t.status or "").lower() not in {"done", "completed"} and (not t.repo_hint or (repo_hint and t.repo_hint.lower() in repo_hint.lower()))]
    if not eligible:
        await update.message.reply_text("No hay siguiente tarea elegible.", **_no_preview_kwargs())
        return
    context.args[:] = [eligible[0].id]
    await handle_do_task(update, context)


async def handle_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return

    cancel_agent()
    if _current_task and not _current_task.done():
        _current_task.cancel()
    await update.message.reply_text("⛔ Señal de cancelación enviada al agente.", **_no_preview_kwargs())


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
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
    _set_workspace_context(update.effective_chat.id)
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
    _current_task = loop.run_in_executor(None, lambda: resume_run(run_id))
    context.application.create_task(_watch_current_task(update, _current_task))


async def handle_modelstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(stats_text(), parse_mode="Markdown", **_no_preview_kwargs())


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
    _bot_app.add_handler(CommandHandler("workon", handle_workon))
    _bot_app.add_handler(CommandHandler("plan_tasks", handle_plan_tasks))
    _bot_app.add_handler(CommandHandler("do_task", handle_do_task))
    _bot_app.add_handler(CommandHandler("do_next", handle_do_next))
    _bot_app.add_handler(CommandHandler("stop", handle_stop))
    _bot_app.add_handler(CommandHandler("status", handle_status))
    _bot_app.add_handler(CommandHandler("plan", handle_plan))
    _bot_app.add_handler(CommandHandler("resume", handle_resume))
    _bot_app.add_handler(CommandHandler("logs", handle_logs))
    _bot_app.add_handler(CommandHandler("diff", handle_diff))
    _bot_app.add_handler(CommandHandler("models", handle_models))
    _bot_app.add_handler(CommandHandler("model", handle_model))
    _bot_app.add_handler(CommandHandler("runwith", handle_runwith))
    _bot_app.add_handler(CommandHandler("modelstats", handle_modelstats))
    _bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    async def send_scheduled(msg: str):
        await _bot_app.bot.send_message(chat_id=ALLOWED_USER, text=msg, **_no_preview_kwargs())

    def sync_send(msg: str):
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(send_scheduled(msg), loop)

    set_send_callback(sync_send)

    print("🚀 Agent server corriendo... (/workon /plan_tasks /do_task /do_next /stop /status /plan /resume /logs /diff /models /model /runwith /modelstats)")
    _bot_app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
