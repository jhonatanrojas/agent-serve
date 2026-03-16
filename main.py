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
from src.task_source_router import TaskSourceRouter
from src.task_store import TaskStore
from src.task_file_manager import TaskFileManager
from src.tools import git_diff_summary
from src.llm_registry import models_status_text, get_model, register_dynamic_model
from src.chat_preferences import get_preference, set_auto, set_manual
from src.llm_runner import stats_text
from src.work_item import WorkItem
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
    pref = get_preference(update.effective_chat.id)
    await update.message.reply_text(f"🤖 Procesando en `{ws['repo_path']}`...", **_no_preview_kwargs())

    loop = asyncio.get_event_loop()

    async def progress(msg):
        await update.message.reply_text(msg, **_no_preview_kwargs())

    def run_sync():
        return run_agent(user_text, lambda m: asyncio.run_coroutine_threadsafe(progress(m), loop),
                         mode=pref["mode"], manual_model_key=pref["model_key"])

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
    tasks = TaskSourceRouter(ws).list_tasks()
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
    router = TaskSourceRouter(ws)
    store = TaskStore(ws["repo_path"])
    manager = TaskFileManager(ws["repo_path"])
    tasks = router.list_tasks()
    task_id = context.args[0]
    task = next((t for t in tasks if t.id.startswith(task_id) or t.id == task_id), None)
    if not task:
        await update.message.reply_text("Task no encontrada.", **_no_preview_kwargs())
        return

    branch = RepoManager(ws["repo_path"]).ensure_task_branch(task.id[:8])
    WorkspaceManager().set_active_branch(update.effective_chat.id, branch)

    if task.source == "notion":
        NotionTaskProvider().update_task_status(task.page_id, "In progress")
    else:
        store.update_status(task.id, "in_progress")
        manager.update_task_file(WorkItem.from_dict({**task.to_dict(), "status": "in_progress"}), "Inicio de ejecución")
    await update.message.reply_text(f"🚀 Ejecutando tarea {task.id[:8]} en branch `{branch}`", **_no_preview_kwargs())
    loop = asyncio.get_event_loop()
    pref = get_preference(update.effective_chat.id)

    def run_sync():
        try:
            return run_supervisor(f"{task.title}\n\n{task.description}",
                                  mode=pref["mode"], manual_model_key=pref["model_key"])
        finally:
            pass

    _current_task = loop.run_in_executor(None, run_sync)

    async def finalize_and_watch():
        result = await _current_task
        status = "Done" if "Tarea completada" in result else "Blocked"
        if task.source == "notion":
            NotionTaskProvider().update_task_status(task.page_id, status)
        else:
            local_status = "done" if status == "Done" else "blocked"
            store.update_status(task.id, local_status)
            manager.update_task_file(WorkItem.from_dict({**task.to_dict(), "status": local_status}), f"Fin de ejecución: {local_status}")
        await update.message.reply_text(result, **_no_preview_kwargs())
        await update.message.reply_text("Esta tarea quedó finalizada. ¿Deseas que continúe con la siguiente?", **_no_preview_kwargs())

    context.application.create_task(finalize_and_watch())


async def handle_do_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _current_task
    if update.effective_user.id != ALLOWED_USER:
        return
    if _current_task and not _current_task.done():
        await update.message.reply_text("⏳ Ya hay una tarea en ejecución.", **_no_preview_kwargs())
        return
    ws = _set_workspace_context(update.effective_chat.id)
    next_task = TaskSourceRouter(ws).next_task()
    if not next_task:
        await update.message.reply_text("No hay siguiente tarea elegible.", **_no_preview_kwargs())
        return
    context.args[:] = [next_task.id]
    await handle_do_task(update, context)


def _parse_task_line(text: str) -> tuple[str, str, list[str]]:
    parts = [p.strip() for p in text.split("|")]
    title = parts[0] if parts else ""
    description = parts[1] if len(parts) > 1 else ""
    deps = [d.strip() for d in (parts[2].split(",") if len(parts) > 2 else []) if d.strip()]
    return title, description, deps


async def handle_addtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Uso: /addtask titulo | descripción | TASK-001,TASK-002", **_no_preview_kwargs())
        return
    raw = " ".join(context.args)
    title, description, deps = _parse_task_line(raw)
    store = TaskStore(ws["repo_path"])
    item = store.add_item(title=title, description=description, depends_on=deps)
    TaskFileManager(ws["repo_path"]).create_task_file(item)
    await update.message.reply_text(f"✅ Creada {item.id}: {item.title}", **_no_preview_kwargs())


async def handle_addtasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    raw = " ".join(context.args)
    if not raw:
        await update.message.reply_text("Uso: /addtasks task1 ; task2 ; task3", **_no_preview_kwargs())
        return
    created = []
    store = TaskStore(ws["repo_path"])
    manager = TaskFileManager(ws["repo_path"])
    for row in [x.strip() for x in raw.split(";") if x.strip()]:
        title, description, deps = _parse_task_line(row)
        item = store.add_item(title, description, deps)
        manager.create_task_file(item)
        created.append(item.id)
    await update.message.reply_text(f"✅ Creadas: {', '.join(created)}", **_no_preview_kwargs())


async def handle_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    items = TaskStore(ws["repo_path"]).list_items()
    if not items:
        await update.message.reply_text("No hay tareas locales.", **_no_preview_kwargs())
        return
    lines = ["🗂️ Backlog local:"]
    for it in items[:30]:
        lines.append(f"- {it.id} | {it.status} | {it.title}")
    await update.message.reply_text("\n".join(lines), **_no_preview_kwargs())


async def handle_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    if not context.args:
        await update.message.reply_text("Uso: /task <TASK-XXX>", **_no_preview_kwargs())
        return
    ws = _set_workspace_context(update.effective_chat.id)
    task = TaskStore(ws["repo_path"]).get_item(context.args[0])
    if not task:
        await update.message.reply_text("No existe la tarea solicitada.", **_no_preview_kwargs())
        return
    deps = ", ".join(task.depends_on) if task.depends_on else "-"
    await update.message.reply_text(f"{task.id} | {task.status}\n{task.title}\nDeps: {deps}\n\n{task.description}", **_no_preview_kwargs())


async def handle_taskmode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    manager = WorkspaceManager()
    if not context.args:
        ws = manager.get_active_workspace(update.effective_chat.id)
        await update.message.reply_text(f"Modo actual: {ws.get('task_mode', 'local')}", **_no_preview_kwargs())
        return
    try:
        ws = manager.set_task_mode(update.effective_chat.id, context.args[0])
        await update.message.reply_text(f"✅ task_mode={ws.get('task_mode')}", **_no_preview_kwargs())
    except WorkspaceError as e:
        await update.message.reply_text(f"❌ {e}", **_no_preview_kwargs())


async def handle_sync_notion_to_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    notion_items = NotionTaskProvider().list_tasks(ws.get("notion_database_id", ""))
    store = TaskStore(ws["repo_path"])
    manager = TaskFileManager(ws["repo_path"])
    imported = 0
    for n in notion_items:
        exists = store.get_item(n.id)
        if exists:
            continue
        local = WorkItem.from_dict({**n.to_dict(), "id": n.id, "source": "notion", "status": (n.status or 'todo').lower()})
        store.upsert_item(local)
        manager.create_task_file(local)
        imported += 1
    await update.message.reply_text(f"✅ Importadas {imported} tareas desde Notion.", **_no_preview_kwargs())


async def handle_export_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    ws = _set_workspace_context(update.effective_chat.id)
    path = TaskStore(ws["repo_path"]).export_json()
    await update.message.reply_text(f"📦 Export local: {path}", **_no_preview_kwargs())


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


_ONBOARDING_TEXT = """👋 *Bienvenido a Agent Server*

Antes de empezar, configura tu entorno:

*1. Workspace activo*
```
/workon repo=<url> branch=<branch>
```
Ejemplo: `/workon repo=git@github.com:user/repo.git branch=main`

*2. Modelo LLM*
El modelo por defecto está configurado en `.env` (`LLM_MODEL`).
Para ver los modelos disponibles: /models
Para fijar un modelo: `/model <key>`
Para agregar un modelo nuevo: `/addmodel`

*3. Fuente de tareas*
- Solo local: `/taskmode local`
- Solo Notion: `/taskmode notion` (requiere `NOTION_API_KEY` en `.env`)
- Ambas: `/taskmode hybrid`

*4. Crear tu primera tarea*
```
/addtask Mi primera tarea | descripción opcional
```

*5. Ejecutar*
```
/do_next
```

Escribe /help para ver todos los comandos disponibles."""


_ADDMODEL_USAGE = """➕ *Agregar modelo*

Formato:
```
/addmodel key=<key> model=<provider/model> env=<API_KEY_VAR> key_val=<valor> [uses=<roles>] [priority=<n>]
```

Ejemplos:
```
/addmodel key=gpt4 model=openai/gpt-4o env=OPENAI_API_KEY key_val=sk-xxx uses=coder,reviewer priority=3
/addmodel key=claude model=anthropic/claude-3-5-sonnet env=ANTHROPIC_API_KEY key_val=sk-ant-xxx uses=general priority=4
```

`uses` acepta: `general`, `coder`, `analyst`, `planner`, `reviewer`, `tests`"""


_SETKEY_USAGE = """🔑 *Configurar API key*

Formato:
```
/setkey <ENV_VAR> <valor>
```

Ejemplos:
```
/setkey OPENAI_API_KEY sk-xxx
/setkey GEMINI_API_KEY AIza-xxx
/setkey MISTRAL_API_KEY xxx
```

Activa el modelo correspondiente en el registry automáticamente."""


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(_ONBOARDING_TEXT, parse_mode="Markdown", **_no_preview_kwargs())


async def handle_addmodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    if not context.args:
        await update.message.reply_text(_ADDMODEL_USAGE, parse_mode="Markdown", **_no_preview_kwargs())
        return

    params = _parse_kv_args(context.args)
    key = params.get("key", "").strip()
    model = params.get("model", "").strip()
    env = params.get("env", "").strip()
    key_val = params.get("key_val", "").strip()
    uses = params.get("uses", "general").strip()
    priority = int(params.get("priority", "10"))

    if not key or not model:
        await update.message.reply_text("❌ `key` y `model` son obligatorios.\n\n" + _ADDMODEL_USAGE,
                                        parse_mode="Markdown", **_no_preview_kwargs())
        return

    entry = register_dynamic_model(key, model, env, key_val, priority, uses)
    await update.message.reply_text(
        f"✅ Modelo `{key}` registrado\n"
        f"• model: `{entry.model}`\n"
        f"• uses: `{', '.join(entry.use_cases)}`\n"
        f"• priority: `{entry.priority}`\n"
        f"• disponible: `{entry.is_available}`",
        parse_mode="Markdown", **_no_preview_kwargs(),
    )


async def handle_setkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(_SETKEY_USAGE, parse_mode="Markdown", **_no_preview_kwargs())
        return

    env_var = context.args[0].strip()
    value = context.args[1].strip()

    # Solo permitir variables de API key conocidas
    allowed_envs = {
        "OPENAI_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY",
        "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    }
    if env_var not in allowed_envs:
        await update.message.reply_text(
            f"❌ Variable no permitida. Permitidas: {', '.join(sorted(allowed_envs))}",
            **_no_preview_kwargs()
        )
        return

    import os as _os
    _os.environ[env_var] = value

    # Recargar registry para reflejar la nueva key
    from src.llm_registry import load_dynamic_models, MODELS_REGISTRY
    load_dynamic_models()

    # Ver qué modelos se activaron
    activated = [m.key for m in MODELS_REGISTRY.values() if m.api_key_env == env_var and m.is_available]
    msg = f"✅ `{env_var}` configurada en memoria."
    if activated:
        msg += f"\nModelos activados: {', '.join(f'`{k}`' for k in activated)}"
    else:
        msg += "\n⚠️ Ningún modelo del registry usa esta variable aún. Usa /addmodel para registrar uno."
    await update.message.reply_text(msg, parse_mode="Markdown", **_no_preview_kwargs())




_HELP_TEXT = """🤖 *Comandos disponibles*

*Inicio*
/start — onboarding y configuración inicial
/help — muestra este mensaje

*Workspace*
/workon repo=<url> branch=<b> [notion=<id>] — configura repo activo
/taskmode [local|notion|hybrid] — consulta o cambia fuente de tareas

*Backlog local*
/addtask <titulo | descripcion | deps> — crea tarea local
/addtasks <t1 ; t2 ; ...> — crea varias tareas en lote
/tasks — lista backlog local
/task <id> — detalle de una tarea local
/export\_tasks — ruta del tasks.json local
/sync\_notion\_to\_tasks — importa tareas de Notion al backlog local

*Ejecución*
/plan\_tasks — lista tareas elegibles según task\_mode
/do\_task <task\_id> — ejecuta tarea concreta en branch task/<id>
/do\_next — ejecuta la siguiente tarea elegible
/stop — cancela la tarea en curso
/resume [run\_id] — reanuda una corrida pausada

*Observabilidad*
/status [run\_id] — dashboard del run activo/último
/plan [run\_id] — subtareas del run
/logs [run\_id] — eventos recientes del run
/diff — diff local actual

*LLM Routing*
/models — lista modelos disponibles y modo actual
/model auto — vuelve a selección automática
/model <key> — fija modelo para este chat
/runwith <key> <tarea> — ejecuta tarea puntual con modelo específico
/modelstats — métricas de uso por modelo
/addmodel key=<k> model=<p/m> env=<VAR> key\_val=<val> — registra modelo nuevo
/setkey <ENV\_VAR> <valor> — configura API key en memoria
/codexkey <api\_key> — autentica Codex CLI con API key
/codexlogin — inicia device flow de Codex CLI (sin browser en servidor)"""


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER:
        return
    await update.message.reply_text(_HELP_TEXT, parse_mode="Markdown", **_no_preview_kwargs())


async def handle_codexlogin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia device flow de Codex CLI y envía URL+código por Telegram."""
    if update.effective_user.id != ALLOWED_USER:
        return

    import subprocess, threading, re, time, os as _os

    def strip_ansi(text):
        return re.sub(r'\x1B\[[0-9;]*[mK]', '', text)

    auth_path = _os.path.expanduser("~/.codex/auth.json")
    mtime_before = _os.path.getmtime(auth_path) if _os.path.exists(auth_path) else 0

    loop = asyncio.get_running_loop()

    proc = subprocess.Popen(
        ["codex", "login", "--device-auth"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    output_lines = []
    for line in proc.stdout:
        clean = strip_ansi(line).strip()
        if clean:
            output_lines.append(clean)
        if len(output_lines) >= 12:
            break

    msg = "🔐 Codex Device Login\n\n"
    msg += "\n".join(output_lines) if output_lines else "Sin output del CLI."
    msg += "\n\nAbre la URL en tu navegador, ingresa el código y autoriza."

    await update.message.reply_text(msg, **_no_preview_kwargs())

    def wait_and_notify():
        # Polling: esperar hasta 15 min a que auth.json sea creado/actualizado
        for _ in range(900):
            mtime_now = _os.path.getmtime(auth_path) if _os.path.exists(auth_path) else 0
            if mtime_now > mtime_before:
                break
            time.sleep(1)

        status = strip_ansi(
            subprocess.run(["codex", "login", "status"], capture_output=True, text=True).stdout
        ).strip()

        mtime_now = _os.path.getmtime(auth_path) if _os.path.exists(auth_path) else 0
        if mtime_now > mtime_before:
            msg_done = f"✅ Codex autenticado exitosamente!\n{status}"
        else:
            msg_done = f"⚠️ Codex login no completado o expiró.\n{status}"

        asyncio.run_coroutine_threadsafe(
            update.message.reply_text(msg_done, **_no_preview_kwargs()),
            loop,
        )

    threading.Thread(target=wait_and_notify, daemon=True).start()


async def handle_codexkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configura Codex CLI con API key directamente via --with-api-key."""
    if update.effective_user.id != ALLOWED_USER:
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: `/codexkey <OPENAI_API_KEY>`\nEjemplo: `/codexkey sk-proj-xxx`",
            parse_mode="Markdown", **_no_preview_kwargs()
        )
        return

    import subprocess, os as _os
    api_key = context.args[0].strip()

    result = subprocess.run(
        ["codex", "login", "--with-api-key"],
        input=api_key, capture_output=True, text=True,
    )
    if result.returncode == 0:
        _os.environ["OPENAI_API_KEY"] = api_key
        from src.llm_registry import load_dynamic_models
        load_dynamic_models()
        status = subprocess.run(["codex", "login", "status"], capture_output=True, text=True).stdout.strip()
        await update.message.reply_text(
            f"✅ Codex CLI autenticado\n`{status}`\n\nModelo `codex_mini` activado en el registry.",
            parse_mode="Markdown", **_no_preview_kwargs()
        )
    else:
        await update.message.reply_text(
            f"❌ Error: {result.stdout or result.stderr}", **_no_preview_kwargs()
        )


async def handle_codexstatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estado de sesión Codex CLI y si codex_mini está activo como runner."""
    if update.effective_user.id != ALLOWED_USER:
        return
    import subprocess, re, os as _os
    strip_ansi = lambda t: re.sub(r'\x1B\[[0-9;]*[mK]', '', t)

    session = strip_ansi(
        subprocess.run(["codex", "login", "status"], capture_output=True, text=True).stdout
    ).strip()

    auth_path = _os.path.expanduser("~/.codex/auth.json")
    cli_active = _os.path.exists(auth_path)

    from src.llm_registry import MODELS_REGISTRY
    from src.chat_preferences import get_preference
    pref = get_preference(update.effective_chat.id)

    lines = [
        f"🔐 Sesión Codex CLI: {'✅ ' + session if cli_active else '❌ No autenticado'}",
        f"🤖 codex_mini runner: {'✅ activo (usa CLI)' if cli_active else '❌ inactivo'}",
        f"⚙️ Modelo actual del chat: {pref or 'auto'}",
    ]
    await update.message.reply_text("\n".join(lines), **_no_preview_kwargs())


def main():
    global _bot_app
    _bot_app = ApplicationBuilder().token(TOKEN).build()
    _bot_app.add_handler(CommandHandler("start", handle_start))
    _bot_app.add_handler(CommandHandler("help", handle_help))
    _bot_app.add_handler(CommandHandler("addmodel", handle_addmodel))
    _bot_app.add_handler(CommandHandler("setkey", handle_setkey))
    _bot_app.add_handler(CommandHandler("codexlogin", handle_codexlogin))
    _bot_app.add_handler(CommandHandler("codexkey", handle_codexkey))
    _bot_app.add_handler(CommandHandler("codexstatus", handle_codexstatus))
    _bot_app.add_handler(CommandHandler("workon", handle_workon))
    _bot_app.add_handler(CommandHandler("plan_tasks", handle_plan_tasks))
    _bot_app.add_handler(CommandHandler("addtask", handle_addtask))
    _bot_app.add_handler(CommandHandler("addtasks", handle_addtasks))
    _bot_app.add_handler(CommandHandler("do_task", handle_do_task))
    _bot_app.add_handler(CommandHandler("do_next", handle_do_next))
    _bot_app.add_handler(CommandHandler("tasks", handle_tasks))
    _bot_app.add_handler(CommandHandler("task", handle_task))
    _bot_app.add_handler(CommandHandler("taskmode", handle_taskmode))
    _bot_app.add_handler(CommandHandler("sync_notion_to_tasks", handle_sync_notion_to_tasks))
    _bot_app.add_handler(CommandHandler("export_tasks", handle_export_tasks))
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

    print("🚀 Agent server corriendo... (/workon /plan_tasks /addtask /addtasks /do_task /do_next /tasks /task /taskmode /sync_notion_to_tasks /export_tasks /stop /status /plan /resume /logs /diff /models /model /runwith /modelstats)")
    _bot_app.run_polling(drop_pending_updates=True, allowed_updates=["message"])


if __name__ == "__main__":
    main()
