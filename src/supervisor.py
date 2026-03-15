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

MAX_AGENT_LOOPS = 2  # máximo de veces que un agente puede reinvocarse sin progreso


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
        """Registra llamada a un agente. Retorna False si hay loop."""
        self.agent_call_counts[agent] = self.agent_call_counts.get(agent, 0) + 1
        return self.agent_call_counts[agent] <= MAX_AGENT_LOOPS


def run_supervisor(user_message: str, progress_callback=None, existing_run_id: str | None = None, completed_subtasks: set[str] | None = None) -> str:
    """
    Orquesta el flujo completo para tareas complejas.
    Decide dinámicamente qué subagente invocar en cada paso.
    """
    state = SupervisorState(message=user_message)
    completed_subtasks = completed_subtasks or set()
    recovery = RecoveryAgent()
    log.info("Supervisor iniciando: %s", user_message[:80])

    def notify(msg: str):
        log.info("[supervisor] %s", msg[:100])
        if progress_callback:
            progress_callback(msg)

    # ── Stage: planning ──────────────────────────────────────────────────────
    if not state.record_agent_call("planner"):
        return "🔁 Loop detectado en planner. Abortando."

    notify("📐 Planificando tarea...")
    is_complex, spec_summary = plan_task(user_message)

    if not is_complex:
        # No es compleja — el supervisor no debe intervenir
        return "__SIMPLE__"

    run_id = existing_run_id or create_run_state(initial_phase="planning", source_message=user_message)
    if existing_run_id is None:
        append_event(run_id, "planning_started", "planning", {"message": user_message[:200]})
        append_checkpoint(run_id, "planning_ready", "planning", {"message": user_message[:200]})
    try:
        workspace = WorkspaceManager().create_or_get_workspace(run_id, user_message)
        mark_validation_result(False, workspace["branch_name"])
        append_checkpoint(run_id, "workspace_ready", "planning", workspace)
        notify(f"🌿 Workspace listo en branch `{workspace['branch_name']}`")
    except WorkspaceError as e:
        append_event(run_id, "guardrail_triggered", "planning", {"agent": "workspace_manager", "reason": str(e)[:200]})
        return f"❌ No se pudo preparar workspace para la tarea: {e}"

    state.spec_summary = spec_summary
    state.spec = generate_spec(user_message)
    state.stage = "analyzing"
    update_run_state(run_id, phase="analyzing")
    append_checkpoint(run_id, "phase_analyzing", "analyzing", {"spec_summary": spec_summary[:300]})
    notify(spec_summary)

    # ── Stage: analyzing ─────────────────────────────────────────────────────
    if is_cancelled():
        append_event(run_id, "run_paused", "analyzing", {"reason": "user_cancelled"})
        append_checkpoint(run_id, "cancelled", "analyzing", {"reason": "user_cancelled"})
        return "⛔ Cancelado."

    if not state.record_agent_call("analyst"):
        state.errors.append("Loop en analyst")
        append_event(run_id, "guardrail_triggered", "analyzing", {"agent": "analyst", "reason": "loop"})
        state.stage = "coding"  # saltar análisis si hay loop
    else:
        notify("🔍 Analizando codebase...")
        state.analysis = analyze_codebase(user_message)
        state.stage = "coding"
        update_run_state(run_id, phase="coding")
        append_event(run_id, "analysis_completed", "analyzing", {"summary": state.analysis[:300]})
        append_checkpoint(run_id, "phase_coding", "coding", {"analysis": state.analysis[:300]})
        notify(state.analysis)

    # ── Stage: coding ────────────────────────────────────────────────────────
    subtasks = state.spec.get("subtasks", [])
    if not subtasks:
        notify("⚠️ La spec no tiene subtareas definidas. Ejecutando como tarea simple.")
        state.stage = "done"
        update_run_state(run_id, phase="done")
        append_checkpoint(run_id, "done_no_subtasks", "done", {})
        return "__SIMPLE__"

    context = f"Spec:\n{state.spec_summary}\n\nAnálisis:\n{state.analysis}"

    for i, subtask in enumerate(subtasks, 1):
        if subtask in completed_subtasks:
            notify(f"⏭️ Subtarea {i} omitida (ya completada en run previo).")
            append_checkpoint(run_id, "subtask_skipped_resume", "coding", {"subtask": subtask, "index": i})
            continue

        if is_cancelled():
            append_event(run_id, "run_paused", "coding", {"reason": "user_cancelled"})
            append_checkpoint(run_id, "cancelled", "coding", {"reason": "user_cancelled", "subtask": subtask})
            return "⛔ Cancelado durante codificación."

        if not state.record_agent_call(f"coder_{i}"):
            notify(f"🔁 Loop detectado en coder subtarea {i}. Saltando.")
            state.errors.append(f"Loop en coder subtarea {i}: {subtask}")
            append_event(run_id, "guardrail_triggered", "coding", {"agent": f"coder_{i}", "reason": "loop", "subtask": subtask})
            continue

        attempt_count = 0
        strategy_used = "default"

        while True:
            attempt_count += 1
            notify(f"🔨 Subtarea {i}/{len(subtasks)} intento {attempt_count}: {subtask}")
            update_run_state(run_id, current_subtask=subtask)
            append_checkpoint(run_id, "subtask_started", "coding", {"subtask": subtask, "index": i, "total": len(subtasks), "attempt": attempt_count})

            effective_context = context + f"\n\nRecovery strategy: {strategy_used}"
            result = run_coder(subtask, context=effective_context, progress_callback=progress_callback)
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
                append_checkpoint(run_id, "subtask_completed", "coding", {"subtask": subtask, "modified_files": result.get("modified_files", []), "attempt": attempt_count})
                notify(f"✅ Subtarea {i} lista. Archivos: {result.get('modified_files', [])}")
                break

            state.errors.append(f"Subtarea {i} falló: {result.get('result', '')[:100]}")
            append_event(run_id, "coding_failed", "coding", {"subtask": subtask, "status": status, "result": result.get("result", "")[:300], "attempt": attempt_count})
            if status == "loop_detected":
                append_event(run_id, "guardrail_triggered", "coding", {"agent": f"coder_{i}", "reason": "loop_detected", "subtask": subtask, "attempt": attempt_count})
            append_checkpoint(run_id, "subtask_failed", "coding", {"subtask": subtask, "status": status, "attempt": attempt_count})

            failure_type = recovery.classify_failure(status, result.get("result", ""))
            decision = recovery.decide(failure_type, attempt_count)
            strategy_used = decision.strategy

            if decision.action == "retry":
                notify(f"♻️ RecoveryAgent reintenta subtarea {i}: {decision.strategy} ({decision.reason})")
                continue

            notify(f"⏸️ RecoveryAgent pausó subtarea {i}: {decision.reason}")
            append_event(run_id, "run_paused", "coding", {"subtask": subtask, "reason": decision.reason, "attempt": attempt_count})
            append_checkpoint(run_id, "paused_by_recovery", "coding", {"subtask": subtask, "reason": decision.reason, "attempt": attempt_count})
            return f"⏸️ Ejecución pausada por RecoveryAgent en subtarea {i}: {decision.reason}"

    state.modified_files = list(set(state.modified_files))
    if state.modified_files:
        refresh_repo_map(state.modified_files)
    state.stage = "reviewing"
    update_run_state(run_id, phase="reviewing", modified_files=state.modified_files, current_subtask="")
    append_checkpoint(run_id, "phase_reviewing", "reviewing", {"modified_files": state.modified_files})

    # ── Stage: reviewing ─────────────────────────────────────────────────────
    if is_cancelled():
        append_event(run_id, "run_paused", "reviewing", {"reason": "user_cancelled"})
        append_checkpoint(run_id, "cancelled", "reviewing", {"reason": "user_cancelled"})
        return "⛔ Cancelado antes del review."

    if not state.record_agent_call("reviewer"):
        notify("🔁 Loop en reviewer. Saltando revisión.")
        append_event(run_id, "guardrail_triggered", "reviewing", {"agent": "reviewer", "reason": "loop"})
    else:
        notify("🔍 Revisando cambios...")
        criteria = state.spec.get("acceptance_criteria", [])
        state.review = run_reviewer(state.spec_summary, state.modified_files, criteria)
        if state.review.get("verdict") in ("RECHAZADO", "PARCIAL"):
            append_event(run_id, "review_rejected", "reviewing", {"verdict": state.review.get("verdict", ""), "issues": state.review.get("issues", [])[:5], "required_fixes": state.review.get("required_fixes", [])[:5]})
        review_msg = format_review(state.review)
        notify(review_msg)

    state.stage = "done"
    update_run_state(run_id, phase="done")
    append_checkpoint(run_id, "phase_done", "done", {})

    # ── Validación técnica ───────────────────────────────────────────────────
    if state.modified_files:
        notify("🔬 Ejecutando validación técnica...")
        validation = run_validation(state.modified_files)
        append_validation(run_id, validation)
        mark_validation_result(bool(validation.get("passed")), workspace.get("branch_name", ""))
        if validation.get("passed"):
            append_event(run_id, "validation_passed", "done", {"checks": len(validation.get("checks", []))})
            append_checkpoint(run_id, "validation_passed", "done", {"checks": len(validation.get("checks", []))})
        validation_msg = format_validation(validation)
        notify(validation_msg)
    else:
        validation = {"passed": True}
        mark_validation_result(True, workspace.get("branch_name", ""))

    # ── Resumen final ────────────────────────────────────────────────────────
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
    if state.errors:
        lines.append(f"• ⚠️ Errores: {'; '.join(state.errors)}")

    return "\n".join(lines)


def _infer_last_completed_phase(run_data: dict) -> str:
    """Infiere la última fase completada usando fase actual y checkpoints."""
    checkpoints = run_data.get("checkpoints", [])
    labels = {cp.get("label", "") for cp in checkpoints if isinstance(cp, dict)}
    current_phase = run_data.get("phase", "planning")

    if "validation_passed" in labels:
        return "done"
    if "phase_done" in labels:
        return "reviewing"
    if "phase_reviewing" in labels:
        return "coding"
    if "phase_coding" in labels:
        return "analyzing"
    if "phase_analyzing" in labels:
        return "planning"
    return current_phase


def resume_run(run_id: str, progress_callback=None) -> str:
    """
    Reanuda una corrida previa a partir de su contexto persistido.
    Implementación incremental: retoma por fase detectada y reejecuta pipeline.
    """
    run_data = get_run_state(run_id)
    if not run_data:
        return f"❌ No existe run_id: {run_id}"

    if run_data.get("phase") == "done":
        return f"✅ La corrida `{run_id}` ya está completada."

    source_message = run_data.get("source_message", "")
    if not source_message:
        source_message = ""
        for ev in run_data.get("events", []):
            details = ev.get("details", {}) if isinstance(ev, dict) else {}
            if isinstance(details, dict) and details.get("message"):
                source_message = details["message"]
                break

    if not source_message:
        return f"❌ No se pudo recuperar el mensaje original para reanudar `{run_id}`."

    phase = _infer_last_completed_phase(run_data)
    append_event(run_id, "run_resumed", phase, {"resume_run_id": run_id})
    append_checkpoint(run_id, "resumed", phase, {"resume_run_id": run_id})

    completed_subtasks = {
        cp.get("data", {}).get("subtask", "")
        for cp in run_data.get("checkpoints", [])
        if isinstance(cp, dict) and cp.get("label") == "subtask_completed"
    }
    completed_subtasks.discard("")

    resumed_result = run_supervisor(
        source_message,
        progress_callback,
        existing_run_id=run_id,
        completed_subtasks=completed_subtasks,
    )
    return (
        f"🔄 Reanudación iniciada desde fase `{phase}` para `{run_id}`.\n"
        "ℹ️ Se ejecutó una continuación segura del pipeline con el contexto persistido.\n\n"
        f"{resumed_result}"
    )
