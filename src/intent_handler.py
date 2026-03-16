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
from src.workspace_manager import WorkspaceManager
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
        return False  # delegar a run_agent normal

    # --- Resolver repo si viene en el mensaje ---
    ws = None
    if intent.get("repo"):
        try:
            repo_url = resolve_repo_url(intent["repo"])
            repo_name = repo_name_from_url(repo_url)
            branch = intent.get("branch") or "main"
            await notify(f"🔧 Configurando workspace: `{repo_name}` (branch: `{branch}`)...")
            ws = WorkspaceManager().set_active_workspace(chat_id, repo_url, "", branch)
            set_active_repo_path(ws["repo_path"])
            await notify(f"✅ Workspace listo: `{repo_name}` en `{ws['active_branch']}`")
        except Exception as e:
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
