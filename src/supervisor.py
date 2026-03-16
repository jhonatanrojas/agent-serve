import logging
from dataclasses import dataclass, field
from typing import Literal

from src.planner import plan_task, generate_spec
from src.analyst import analyze_codebase
from src.coder import run_coder
from src.reviewer import run_reviewer, format_review
from src.validator import run_validation, format_validation
from src.executor import is_cancelled
from src.run_state import (
    create_run_state, get_run_state, update_run_state,
    append_modified_files, append_validation, append_event, append_checkpoint, append_attempt,
)
from src.workspace_manager import WorkspaceManager, WorkspaceError
from src.git_gate import mark_validation_result
from src.repomap import refresh_repo_map
from src.recovery_agent import RecoveryAgent

log = logging.getLogger("supervisor")

Stage = Literal["planning", "analyzing", "coding", "reviewing", "done", "failed"]

MAX_AGENT_LOOPS = 2


@dataclass
class SupervisorState:
    message: str
    stage: Stage = "planning"
    spec: dict = field(default_factory=dict)
    spec_summary: str = ""
    analysis: str = ""
    modified_files: list = field(default_factory=list)
    review: dict = field(default_factory=dict)
    agent_call_counts: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)

    def record_agent_call(self, agent: str) -> bool:
        self.agent_call_counts[agent] = self.agent_call_counts.get(agent, 0) + 1
        return self.agent_call_counts[agent] <= MAX_AGENT_LOOPS


def _parse_subtask_index(next_action: str) -> int:
    if not next_action.startswith("code_subtask_"):
        return 1
    try:
        idx = int(next_action.split("code_subtask_")[1])
        return max(idx, 1)
    except Exception:
        return 1


def run_supervisor(user_message: str, progress_callback=None, existing_run_id: str | None = None,
                   completed_subtasks: set[str] | None = None,
                   mode: str = "auto", manual_model_key: str | None = None) -> str:
    state = SupervisorState(message=user_message)
    completed_subtasks = completed_subtasks or set()
    recovery = RecoveryAgent()
    log.info("Supervisor iniciando: %s", user_message[:80])

    existing_run = get_run_state(existing_run_id) if existing_run_id else None
    if existing_run:
        state.spec = existing_run.get("spec", {})
        state.modified_files = existing_run.get("modified_files", [])
        completed_subtasks.update(existing_run.get("completed_subtasks", []))

    next_action = (existing_run or {}).get("next_action", "planning")

    def notify(msg: str):
        log.info("[supervisor] %s", msg[:100])
        if progress_callback:
            progress_callback(msg)

    def notify_agent(agent: str, msg: str):
        from src.llm_selector import select_candidates
        candidates = select_candidates(agent_role=agent, mode=mode, manual_model_key=manual_model_key)
        model_label = candidates[0].key if candidates else "?"
        notify(f"🤖 [{agent}/{model_label}] {msg}")

    run_id = existing_run_id or create_run_state(initial_phase="planning", source_message=user_message)

    try:
        workspace = WorkspaceManager().create_or_get_workspace(run_id, user_message)
        mark_validation_result(False, workspace["branch_name"])
        append_checkpoint(run_id, "workspace_ready", "planning", workspace)
        notify(f"🌿 Workspace listo en branch `{workspace['branch_name']}`")
    except WorkspaceError as e:
        append_event(run_id, "guardrail_triggered", "planning", {"agent": "workspace_manager", "reason": str(e)[:200]})
        return f"❌ No se pudo preparar workspace para la tarea: {e}"

    # planning
    if next_action == "planning":
        if not state.record_agent_call("planner"):
            return "🔁 Loop detectado en planner. Abortando."

        notify_agent("planner", "Planificando tarea...")
        is_complex, spec_summary = plan_task(user_message, mode=mode, manual_model_key=manual_model_key)
        if not is_complex:
            return "__SIMPLE__"

        if existing_run_id is None:
            append_event(run_id, "planning_started", "planning", {"message": user_message[:200]})
            append_checkpoint(run_id, "planning_ready", "planning", {"message": user_message[:200]})

        state.spec_summary = spec_summary
        state.spec = generate_spec(user_message, mode=mode, manual_model_key=manual_model_key)
        state.stage = "analyzing"
        update_run_state(run_id, phase="analyzing", next_action="analyze", spec=state.spec)
        append_checkpoint(run_id, "phase_analyzing", "analyzing", {"spec_summary": spec_summary[:300]})
        notify(spec_summary)
    else:
        if not state.spec:
            _, state.spec_summary = plan_task(user_message, mode=mode, manual_model_key=manual_model_key)
            state.spec = generate_spec(user_message, mode=mode, manual_model_key=manual_model_key)
            update_run_state(run_id, spec=state.spec)
        notify(f"🔄 Reanudando desde `{next_action}`")

    # analyzing
    if next_action in ("planning", "analyze"):
        if is_cancelled():
            append_event(run_id, "run_paused", "analyzing", {"reason": "user_cancelled"})
            append_checkpoint(run_id, "cancelled", "analyzing", {"reason": "user_cancelled"})
            return "⛔ Cancelado."

        if not state.record_agent_call("analyst"):
            state.errors.append("Loop en analyst")
            append_event(run_id, "guardrail_triggered", "analyzing", {"agent": "analyst", "reason": "loop"})
            state.stage = "coding"
        else:
            notify_agent("analyst", "Analizando codebase...")
            state.analysis = analyze_codebase(user_message)
            state.stage = "coding"
            update_run_state(run_id, phase="coding", next_action="code_subtask_1")
            append_event(run_id, "analysis_completed", "analyzing", {"summary": state.analysis[:300]})
            append_checkpoint(run_id, "phase_coding", "coding", {"analysis": state.analysis[:300]})
            notify(state.analysis)

    subtasks = state.spec.get("subtasks", [])
    if not subtasks:
        notify("⚠️ La spec no tiene subtareas definidas. Ejecutando como tarea simple.")
        state.stage = "done"
        update_run_state(run_id, phase="done", next_action="done")
        append_checkpoint(run_id, "done_no_subtasks", "done", {})
        return "__SIMPLE__"

    start_index = _parse_subtask_index(next_action) if next_action.startswith("code_subtask_") else 1
    context = f"Spec:\n{state.spec_summary}\n\nAnálisis:\n{state.analysis}"

    if next_action in ("planning", "analyze") or next_action.startswith("code_subtask_"):
        for i, subtask in enumerate(subtasks, 1):
            if i < start_index or subtask in completed_subtasks:
                append_checkpoint(run_id, "subtask_skipped_resume", "coding", {"subtask": subtask, "index": i})
                continue

            if is_cancelled():
                append_event(run_id, "run_paused", "coding", {"reason": "user_cancelled"})
                append_checkpoint(run_id, "cancelled", "coding", {"reason": "user_cancelled", "subtask": subtask})
                return "⛔ Cancelado durante codificación."

            if not state.record_agent_call(f"coder_{i}"):
                state.errors.append(f"Loop en coder subtarea {i}: {subtask}")
                append_event(run_id, "guardrail_triggered", "coding", {"agent": f"coder_{i}", "reason": "loop", "subtask": subtask})
                continue

            attempt_count = 0
            strategy_used = "default"
            while True:
                attempt_count += 1
                notify_agent("coder", f"Subtarea {i}/{len(subtasks)} intento {attempt_count}:\n`{subtask}`")
                update_run_state(run_id, current_subtask=subtask, current_subtask_index=i, next_action=f"code_subtask_{i}")
                append_checkpoint(run_id, "subtask_started", "coding", {"subtask": subtask, "index": i, "total": len(subtasks), "attempt": attempt_count})

                effective_context = context + f"\n\nRecovery strategy: {strategy_used}"
                result = run_coder(subtask, context=effective_context, progress_callback=progress_callback,
                                   mode=mode, manual_model_key=manual_model_key,
                                   repo_path=workspace.get("repo_path"))
                state.modified_files.extend(result.get("modified_files", []))
                append_modified_files(run_id, result.get("modified_files", []))
                status = result.get("status", "unknown")
                append_attempt(run_id, {
                    "subtask": subtask,
                    "attempt_count": attempt_count,
                    "strategy_used": strategy_used,
                    "resultado": status,
                })

                if status not in ("loop_detected", "error"):
                    completed_subtasks.add(subtask)
                    append_checkpoint(run_id, "subtask_completed", "coding", {"subtask": subtask, "modified_files": result.get("modified_files", []), "attempt": attempt_count})
                    update_run_state(
                        run_id,
                        completed_subtasks=sorted(completed_subtasks),
                        next_action=f"code_subtask_{i + 1}" if i < len(subtasks) else "review",
                    )
                    break

                state.errors.append(f"Subtarea {i} falló: {result.get('result', '')[:100]}")
                append_event(run_id, "coding_failed", "coding", {"subtask": subtask, "status": status, "result": result.get("result", "")[:300], "attempt": attempt_count})
                append_checkpoint(run_id, "subtask_failed", "coding", {"subtask": subtask, "status": status, "attempt": attempt_count})

                failure_type = recovery.classify_failure(status, result.get("result", ""))
                decision = recovery.decide(failure_type, attempt_count)
                strategy_used = decision.strategy
                if decision.action == "retry":
                    continue

                append_event(run_id, "run_paused", "coding", {"subtask": subtask, "reason": decision.reason, "attempt": attempt_count})
                append_checkpoint(run_id, "paused_by_recovery", "coding", {"subtask": subtask, "reason": decision.reason, "attempt": attempt_count})
                return f"⏸️ Ejecución pausada por RecoveryAgent en subtarea {i}: {decision.reason}"

        state.modified_files = list(set(state.modified_files))
        if state.modified_files:
            refresh_repo_map(state.modified_files)
        state.stage = "reviewing"
        update_run_state(run_id, phase="reviewing", next_action="review", modified_files=state.modified_files, current_subtask="")
        append_checkpoint(run_id, "phase_reviewing", "reviewing", {"modified_files": state.modified_files})

    if next_action in ("planning", "analyze") or next_action.startswith("code_subtask_") or next_action == "review":
        if is_cancelled():
            append_event(run_id, "run_paused", "reviewing", {"reason": "user_cancelled"})
            append_checkpoint(run_id, "cancelled", "reviewing", {"reason": "user_cancelled"})
            return "⛔ Cancelado antes del review."

        if state.record_agent_call("reviewer"):
            notify_agent("reviewer", "Revisando cambios...")
            criteria = state.spec.get("acceptance_criteria", [])
            state.review = run_reviewer(state.spec_summary, state.modified_files, criteria,
                                        mode=mode, manual_model_key=manual_model_key)
            if state.review.get("verdict") in ("RECHAZADO", "PARCIAL"):
                append_event(run_id, "review_rejected", "reviewing", {"verdict": state.review.get("verdict", "")})
            notify(format_review(state.review))

        state.stage = "done"
        update_run_state(run_id, phase="done", next_action="validate")
        append_checkpoint(run_id, "phase_done", "done", {})

    if state.modified_files and next_action in (
        "planning", "analyze", "review", "validate"
    ) or next_action.startswith("code_subtask_"):
        notify("🔬 [validator] Ejecutando validación técnica...")
        validation = run_validation(state.modified_files)
        append_validation(run_id, validation)
        mark_validation_result(bool(validation.get("passed")), workspace.get("branch_name", ""))
        if validation.get("passed"):
            append_event(run_id, "validation_passed", "done", {"checks": len(validation.get("checks", []))})
            append_checkpoint(run_id, "validation_passed", "done", {"checks": len(validation.get("checks", []))})
        notify(format_validation(validation))
    else:
        validation = {"passed": True}
        mark_validation_result(True, workspace.get("branch_name", ""))

    update_run_state(run_id, phase="done", next_action="done", current_subtask="", current_subtask_index=0)

    verdict = state.review.get("verdict", "SIN REVIEW")
    lines = [
        "🏁 **Tarea completada**",
        f"• Run ID: {run_id}",
        f"• Branch: {workspace.get('branch_name', 'n/a')}",
        f"• Subtareas ejecutadas: {len(subtasks)}",
        f"• Archivos modificados: {state.modified_files or 'ninguno'}",
        f"• Review: {verdict}",
        f"• Validación: {'✅ OK' if validation.get('passed') else '⚠️ Con errores'}",
    ]
    return "\n".join(lines)


def resume_run(run_id: str, progress_callback=None) -> str:
    run_data = get_run_state(run_id)
    if not run_data:
        return f"❌ No existe run_id: {run_id}"

    if run_data.get("phase") == "done" and run_data.get("next_action") in ("done", ""):
        return f"✅ La corrida `{run_id}` ya está completada."

    source_message = run_data.get("source_message", "")
    if not source_message:
        for ev in run_data.get("events", []):
            details = ev.get("details", {}) if isinstance(ev, dict) else {}
            if isinstance(details, dict) and details.get("message"):
                source_message = details["message"]
                break

    if not source_message:
        return f"❌ No se pudo recuperar el mensaje original para reanudar `{run_id}`."

    next_action = run_data.get("next_action", "planning")
    append_event(run_id, "run_resumed", run_data.get("phase", "planning"), {"resume_run_id": run_id, "next_action": next_action})
    append_checkpoint(run_id, "resumed", run_data.get("phase", "planning"), {"resume_run_id": run_id, "next_action": next_action})

    resumed_result = run_supervisor(
        source_message,
        progress_callback,
        existing_run_id=run_id,
        completed_subtasks=set(run_data.get("completed_subtasks", [])),
    )
    return (
        f"🔄 Reanudación iniciada desde acción `{next_action}` para `{run_id}`.\n"
        "ℹ️ Se continuó la corrida usando estado persistido por fase/subtarea.\n\n"
        f"{resumed_result}"
    )
