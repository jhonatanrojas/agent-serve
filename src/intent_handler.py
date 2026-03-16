"""
Orquesta acciones según la intención clasificada.
Maneja el flujo conversacional: setup → crear tareas → confirmar → ejecutar.
"""
from __future__ import annotations
import logging
import os
from typing import Callable, Awaitable

from src.intent_classifier import classify_intent
from src.repo_resolver import resolve_repo_url, repo_name_from_url, default_branch
from src.workspace_manager import WorkspaceManager, WORKSPACE_ROOT, _safe_repo_dir
from src.workspace_context import set_active_repo_path
from src.task_store import TaskStore
from src.task_file_manager import TaskFileManager
from src.work_item import WorkItem

log = logging.getLogger("intent_handler")

# Estado conversacional en memoria por chat_id
# {"pending_tasks": [...], "pending_repo_path": str, "pending_ws": dict}
_chat_state: dict[int, dict] = {}


def get_chat_state(chat_id: int) -> dict:
    return _chat_state.setdefault(chat_id, {})


def clear_pending(chat_id: int):
    _chat_state.pop(chat_id, None)


def request_github_token(chat_id: int, pr_data: dict | None = None):
    """Marca el estado para que el próximo mensaje del usuario sea interpretado como GITHUB_TOKEN."""
    state = get_chat_state(chat_id)
    state["pending_github_token"] = True
    if pr_data:
        state["pending_pr"] = pr_data


async def handle_natural_message(
    chat_id: int,
    user_id: int,
    message: str,
    notify: Callable[[str], Awaitable],
    run_task_fn: Callable[[str], Awaitable],  # ejecuta una tarea por id
) -> bool:
    """
    Procesa un mensaje libre. Retorna True si fue manejado, False si debe
    seguir el flujo normal de run_agent().
    """
    state = get_chat_state(chat_id)

    # --- Token pendiente: el usuario está respondiendo con el GITHUB_TOKEN ---
    if state.get("pending_github_token"):
        token = message.strip()
        if token and not token.startswith("/"):
            import re
            from pathlib import Path
            os.environ["GITHUB_TOKEN"] = token
            env_path = Path(__file__).parent.parent / ".env"
            if env_path.exists():
                text = env_path.read_text()
                if "GITHUB_TOKEN=" in text:
                    text = re.sub(r"GITHUB_TOKEN=.*", f"GITHUB_TOKEN={token}", text)
                else:
                    text += f"\nGITHUB_TOKEN={token}\n"
                env_path.write_text(text)
            state.pop("pending_github_token")
            await notify("✅ GitHub token registrado. Reintentando el PR...")
            # Reintentar PR si hay datos pendientes
            if state.get("pending_pr"):
                pr_data = state.pop("pending_pr")
                from src.tools import create_github_pr
                pr = create_github_pr(**pr_data)
                if "url" in pr:
                    await notify(f"🔀 PR creado: {pr['url']}")
                else:
                    await notify(f"⚠️ PR falló: {pr.get('error')}")
            return True
        else:
            state.pop("pending_github_token", None)
            await notify("❌ Token inválido, operación cancelada.")
            return True

    # --- Confirmación pendiente ---
    if state.get("pending_tasks"):
        intent = classify_intent(message)
        if intent["intent"] == "confirm":
            tasks = state.pop("pending_tasks")
            ws = state.pop("pending_ws", {})
            await notify(f"▶️ Ejecutando {len(tasks)} tarea(s)...")
            for task_id in tasks:
                await run_task_fn(task_id)
            return True
        elif intent["intent"] == "cancel":
            clear_pending(chat_id)
            await notify("❌ Cancelado. Las tareas quedaron en el backlog.")
            return True
        # Si no es confirm/cancel, procesar como nuevo mensaje

    # --- Clasificar intención ---
    intent = classify_intent(message)
    log.info(f"[intent_handler] chat={chat_id} intent={intent}")
    kind = intent.get("intent", "other")

    if kind == "other":
        return False  # delegar a run_agent normal

    if kind == "query":
        # Responder con estado real del backlog y run activo, sin crear workspace
        try:
            ws_data = WorkspaceManager().get_active_workspace(chat_id)
        except Exception:
            ws_data = None

        lines = []

        # Estado del run activo
        from src.run_state import get_run_state, list_recent_runs
        runs = list_recent_runs(limit=1)
        if runs:
            run = get_run_state(runs[0]["run_id"])
            if run and run.get("phase") not in ("done", None):
                lines.append(f"⚙️ **Tarea en ejecución**")
                lines.append(f"• Fase: `{run.get('phase')}`")
                if run.get("current_subtask"):
                    lines.append(f"• Subtarea actual: `{run.get('current_subtask')}`")
                idx = run.get("current_subtask_index", 0)
                total = len(run.get("spec", {}).get("subtasks", []))
                if total:
                    lines.append(f"• Progreso: {idx}/{total} subtareas")

        # Backlog de tareas
        if ws_data:
            from src.task_store import TaskStore
            store = TaskStore(ws_data["repo_path"])
            items = store.list_items()
            todo = [i for i in items if i.status == "todo"]
            done = [i for i in items if i.status == "done"]
            blocked = [i for i in items if i.status == "blocked"]
            lines.append(f"\n📋 **Backlog** (`{repo_name_from_url(ws_data.get('repo_url', ws_data['repo_path']))}`)")
            lines.append(f"• Pendientes: {len(todo)} | Completadas: {len(done)} | Bloqueadas: {len(blocked)}")
            for t in todo[:5]:
                lines.append(f"  - `{t.id}`: {t.title[:60]}")
            if len(todo) > 5:
                lines.append(f"  ... y {len(todo)-5} más")

        if lines:
            await notify("\n".join(lines))
        else:
            await notify("No hay workspace activo ni tareas en ejecución.")
        return True

    # --- do_next: ejecutar tareas pendientes del backlog activo ---
    if kind == "do_next":
        try:
            ws_data = WorkspaceManager().get_active_workspace(chat_id)
        except Exception:
            ws_data = None
        if not ws_data:
            await notify("⚠️ No hay workspace activo.")
            return True
        set_active_repo_path(ws_data["repo_path"])
        store = TaskStore(ws_data["repo_path"])
        # Desbloquear tareas bloqueadas por error previo para reintentarlas
        for item in store.list_items():
            if item.status == "blocked":
                store.update_status(item.id, "todo")
        pending = [i for i in store.list_items() if i.status == "todo"]
        if not pending:
            await notify("✅ No hay tareas pendientes en el backlog.")
            return True
        await notify(f"▶️ Ejecutando {len(pending)} tarea(s) pendiente(s) secuencialmente...")
        for item in pending:
            await run_task_fn(item.id)
        return True

    # --- Resolver repo si viene en el mensaje ---
    ws = None
    if intent.get("repo"):
        try:
            repo_url = resolve_repo_url(intent["repo"])
            repo_name = repo_name_from_url(repo_url)
            # Nunca usar main/master directamente — usar branch de trabajo
            raw_branch = intent.get("branch") or ""
            if not raw_branch or raw_branch.strip() in ("main", "master"):
                work_branch = "agent/work"
            else:
                work_branch = raw_branch

            await notify(f"🔧 Configurando workspace: `{repo_name}` (branch: `{work_branch}`)...")

            import asyncio
            loop = asyncio.get_event_loop()

            def _setup():
                wm = WorkspaceManager()
                # Clonar/actualizar desde main, luego crear branch de trabajo
                WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
                import git as _git
                from pathlib import Path
                repo_dir = WORKSPACE_ROOT / _safe_repo_dir(repo_url)
                if (repo_dir / ".git").exists():
                    repo = _git.Repo(str(repo_dir))
                    repo.remotes.origin.fetch()
                else:
                    repo = _git.Repo.clone_from(repo_url, str(repo_dir))
                # Crear branch de trabajo desde HEAD si no existe
                if work_branch not in [b.name for b in repo.branches]:
                    repo.create_head(work_branch)
                repo.heads[work_branch].checkout()
                return wm.set_active_workspace(chat_id, repo_url, "", work_branch)

            ws = await loop.run_in_executor(None, _setup)
            set_active_repo_path(ws["repo_path"])
            await notify(f"✅ Workspace listo: `{repo_name}` en `{ws['active_branch']}`")
        except Exception as e:
            log.exception(f"[intent_handler] error setup repo: {e}")
            await notify(f"❌ No pude configurar el repo: {e}")
            return True
    else:
        # Usar workspace activo
        try:
            ws_data = WorkspaceManager().get_active_workspace(chat_id)
            if ws_data:
                ws = ws_data
        except Exception:
            pass

    if not ws:
        await notify("⚠️ No hay workspace activo. Dime el nombre del repo, por ejemplo: _trabaja en mi-repo_")
        return True

    # --- Crear tareas ---
    tasks_text = intent.get("tasks", [])
    if not tasks_text:
        return True

    repo_path = ws["repo_path"]
    store = TaskStore(repo_path)
    manager = TaskFileManager(repo_path)
    created_ids = []

    for title in tasks_text:
        item = store.add_item(title=title, description="")
        manager.create_task_file(item)
        created_ids.append(item.id)

    lines = [f"📋 Tareas creadas en `{repo_name_from_url(ws.get('repo_url', repo_path))}`:"]
    for tid in created_ids:
        task = store.get_item(tid)
        lines.append(f"  • `{tid[:8]}`: {task.title if task else tid}")
    lines.append("\n¿Ejecuto ahora? Responde *sí* o *no*")

    state["pending_tasks"] = created_ids
    state["pending_ws"] = ws
    await notify("\n".join(lines))
    return True
