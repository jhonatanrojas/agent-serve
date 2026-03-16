import logging
from dataclasses import dataclass, field
from typing import Literal

from src.planner import plan_task, generate_spec, enrich_task_plan
from src.analyst import analyze_codebase
from src.coder import run_coder
from src.reviewer import run_reviewer, format_review, run_self_review
from src.validator import run_validation, format_validation
from src.executor import is_cancelled
from src.run_state import (
    create_run_state, get_run_state, update_run_state,
    append_modified_files, append_validation, append_event, append_checkpoint, append_attempt, append_decision,
)
from src.workspace_manager import WorkspaceManager, WorkspaceError
from src.git_gate import mark_validation_result
from src.repomap import refresh_repo_map
from src.recovery_agent import RecoveryAgent

log = logging.getLogger("supervisor")

Stage = Literal["planning", "analyzing", "coding", "reviewing", "done", "failed"]

MAX_AGENT_LOOPS = 2
CIRCUIT_BREAKER_THRESHOLD = int(__import__("os").getenv("TASK_CIRCUIT_BREAKER_THRESHOLD", "3"))


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




def _is_analysis_subtask(text: str) -> bool:
    t = (text or "").strip().lower()
    analysis_signals = ("analizar", "investigar", "revisar", "documentar", "diagnosticar", "explorar",
                        "mockup", "wireframe", "planificar", "definir", "evaluar", "proponer", "diseñar")
    implementation_signals = ("implementar", "cambiar", "modificar", "crear", "agregar", "fix", "corregir",
                               "actualizar", "escribir", "editar", "refactorizar", "añadir", "insertar")
    if any(sig in t for sig in implementation_signals):
        return False
    return any(sig in t for sig in analysis_signals)




def _trace_decision(run_id: str, phase: str, decision: str, details: dict | None = None,
                    actor: str = "supervisor", cost_estimate: float = 0, risk_level: str = "low"):
    payload = {"decision": decision, **(details or {})}
    append_checkpoint(run_id, f"decision:{decision}", phase, payload)
    append_decision(run_id, phase, decision, actor=actor, details=payload, cost_estimate=cost_estimate, risk_level=risk_level)

def run_supervisor(user_message: str, progress_callback=None, existing_run_id: str | None = None,
                   completed_subtasks: set[str] | None = None,
                   mode: str = "auto", manual_model_key: str | None = None,
                   task_id: str | None = None,
                   max_llm_calls: int | None = None,
                   max_tool_calls: int | None = None) -> str:
    state = SupervisorState(message=user_message)
    completed_subtasks = completed_subtasks or set()
    recovery = RecoveryAgent()
    run_llm_calls = 0
    run_tool_calls = 0
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

    run_id = existing_run_id or create_run_state(initial_phase="planning", source_message=user_message, task_id=task_id)

    try:
        from src.workspace_context import get_active_repo_path
        active_repo = get_active_repo_path()
        workspace = WorkspaceManager(repo_path=active_repo).create_or_get_workspace(run_id, user_message, task_id=task_id)
        _trace_decision(run_id, "planning", "workspace_prepared", {"branch": workspace.get("branch_name", ""), "task_id": task_id or ""})
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
        is_complex, spec_summary, planner_model = plan_task(user_message, mode=mode, manual_model_key=manual_model_key)
        if not is_complex:
            return "__SIMPLE__"

        if existing_run_id is None:
            append_event(run_id, "planning_started", "planning", {"message": user_message[:200]})
            append_checkpoint(run_id, "planning_ready", "planning", {"message": user_message[:200]})

        state.spec_summary = spec_summary
        spec, _ = generate_spec(user_message, mode=mode, manual_model_key=manual_model_key)
        state.spec = spec
        state.stage = "analyzing"
        update_run_state(run_id, phase="analyzing", next_action="analyze", spec=state.spec)
        append_checkpoint(run_id, "phase_analyzing", "analyzing", {"spec_summary": spec_summary[:300]})
        notify(spec_summary)
    else:
        if not state.spec:
            _, state.spec_summary, _ = plan_task(user_message, mode=mode, manual_model_key=manual_model_key)
            state.spec, _ = generate_spec(user_message, mode=mode, manual_model_key=manual_model_key)
            update_run_state(run_id, spec=state.spec)
        notify(f"🔄 Reanudando desde `{next_action}`")

    plan_meta = enrich_task_plan(state.spec)
    append_checkpoint(run_id, "plan_enriched", "planning", plan_meta)
    _trace_decision(run_id, "planning", "plan_enriched", {"risk": plan_meta.get("risk", "low"), "phases": plan_meta.get("ordered_phases", [])})

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
            state.analysis, analyst_model = analyze_codebase(user_message)
            notify(f"🤖 [analyst/{analyst_model}] análisis completado")
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

    coding_subtasks = [s for s in subtasks if not _is_analysis_subtask(s)]
    skipped_analysis = [s for s in subtasks if _is_analysis_subtask(s)]
    if skipped_analysis:
        append_checkpoint(run_id, "analysis_subtasks_skipped", "coding", {"count": len(skipped_analysis), "subtasks": skipped_analysis[:10]})
        _trace_decision(run_id, "coding", "skip_analysis_subtasks", {"count": len(skipped_analysis)})
        notify(f"ℹ️ Subtareas de análisis derivadas al analyst (omitidas en coder): {len(skipped_analysis)}")

    if not coding_subtasks:
        notify("⚠️ Solo había subtareas de análisis. Nada para implementar en coder.")
        state.stage = "done"
        update_run_state(run_id, phase="done", next_action="done")
        append_checkpoint(run_id, "done_analysis_only", "done", {"subtasks": skipped_analysis[:10]})
        return "__SIMPLE__"

    start_index = _parse_subtask_index(next_action) if next_action.startswith("code_subtask_") else 1
    context = f"Spec:\n{state.spec_summary}\n\nAnálisis:\n{state.analysis}"

    # Al reanudar desde una subtarea intermedia, incluir el diff actual para
    # que el coder sepa qué ya fue implementado y no repita trabajo.
    if start_index > 1:
        try:
            import subprocess
            repo_path = workspace.get("repo_path", ".")
            diff = subprocess.check_output(
                ["git", "diff", "HEAD"], cwd=repo_path, text=True, timeout=10
            )
            status_out = subprocess.check_output(
                ["git", "status", "--short"], cwd=repo_path, text=True, timeout=10
            )
            if diff or status_out:
                context += f"\n\n⚠️ RESUME: cambios ya implementados en el repo (NO repetir):\n```\n{status_out}{diff[:1500]}\n```"
        except Exception:
            pass

    if next_action in ("planning", "analyze") or next_action.startswith("code_subtask_"):
        failure_causes: dict[str, int] = {}
        consecutive_no_change = 0
        milestone_step = max(1, len(coding_subtasks) // 3)
        for i, subtask in enumerate(coding_subtasks, 1):
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
                notify_agent("coder", f"Subtarea {i}/{len(coding_subtasks)} intento {attempt_count}:\n`{subtask}`")
                update_run_state(run_id, current_subtask=subtask, current_subtask_index=i, next_action=f"code_subtask_{i}")
                append_checkpoint(run_id, "subtask_started", "coding", {"subtask": subtask, "index": i, "total": len(coding_subtasks), "attempt": attempt_count})

                effective_context = context + f"\n\nRecovery strategy: {strategy_used}"
                remaining_llm = None if max_llm_calls is None else max(max_llm_calls - run_llm_calls, 0)
                remaining_tools = None if max_tool_calls is None else max(max_tool_calls - run_tool_calls, 0)
                result = run_coder(subtask, context=effective_context, progress_callback=progress_callback,
                                   mode=mode, manual_model_key=manual_model_key,
                                   repo_path=workspace.get("repo_path"),
                                   max_llm_calls=remaining_llm,
                                   max_tool_calls=remaining_tools)
                run_llm_calls += int(result.get("llm_calls", 0) or 0)
                run_tool_calls += int(result.get("tool_calls", 0) or 0)
                changed_now = result.get("modified_files", [])
                state.modified_files.extend(changed_now)
                append_modified_files(run_id, changed_now)
                if changed_now:
                    consecutive_no_change = 0
                else:
                    consecutive_no_change += 1
                status = result.get("status", "unknown")
                if status == "error" and "Presupuesto" in str(result.get("result", "")):
                    _trace_decision(run_id, "coding", "budget_exhausted", {
                        "subtask": subtask,
                        "llm_calls": int(result.get("llm_calls", 0) or 0),
                        "tool_calls": int(result.get("tool_calls", 0) or 0),
                    }, risk_level="medium")
                append_attempt(run_id, {
                    "subtask": subtask,
                    "attempt_count": attempt_count,
                    "strategy_used": strategy_used,
                    "resultado": status,
                })

                if status not in ("loop_detected", "error"):
                    completed_subtasks.add(subtask)
                    if i % milestone_step == 0 or i == len(coding_subtasks):
                        append_checkpoint(run_id, "milestone_reached", "coding", {"completed": i, "total": len(coding_subtasks)})
                        notify(f"🏁 Milestone {i}/{len(coding_subtasks)} alcanzado")
                    
                    # Commits incrementales
                    try:
                        from src.tools import git_commit
                        commit_msg = f"subtask {i}: {subtask[:60]}"
                        git_commit(commit_msg)
                        notify(f"📝 Commit incremental: `{commit_msg}`")
                    except Exception as e:
                        log.warning("No se pudo realizar commit incremental: %s", e)

                    append_checkpoint(run_id, "subtask_completed", "coding", {"subtask": subtask, "modified_files": result.get("modified_files", []), "attempt": attempt_count})
                    update_run_state(
                        run_id,
                        completed_subtasks=sorted(completed_subtasks),
                        next_action=f"code_subtask_{i + 1}" if i < len(coding_subtasks) else "review",
                    )
                    break

                state.errors.append(f"Subtarea {i} falló: {result.get('result', '')[:100]}")
                if consecutive_no_change >= 2:
                    reason = "Bloqueo detectado: múltiples intentos sin cambios de archivos"
                    _trace_decision(run_id, "coding", "blocked_no_progress", {"subtask": subtask, "count": consecutive_no_change}, risk_level="medium")
                    append_event(run_id, "run_paused", "coding", {"subtask": subtask, "reason": reason, "attempt": attempt_count})
                    append_checkpoint(run_id, "paused_by_no_progress", "coding", {"subtask": subtask, "reason": reason, "attempt": attempt_count})
                    return f"⏸️ {reason}. Solicita ayuda o ajusta la subtarea."
                append_event(run_id, "coding_failed", "coding", {"subtask": subtask, "status": status, "result": result.get("result", "")[:300], "attempt": attempt_count})
                _trace_decision(run_id, "coding", "subtask_failed", {"subtask": subtask, "status": status, "attempt": attempt_count})
                append_checkpoint(run_id, "subtask_failed", "coding", {"subtask": subtask, "status": status, "attempt": attempt_count})

                failure_type = recovery.classify_failure(status, result.get("result", ""))
                cause_key = f"{subtask}|{failure_type}"
                failure_causes[cause_key] = failure_causes.get(cause_key, 0) + 1
                if failure_causes[cause_key] >= CIRCUIT_BREAKER_THRESHOLD:
                    reason = f"Circuit-breaker: misma causa '{failure_type}' repetida {failure_causes[cause_key]} veces"
                    _trace_decision(run_id, "coding", "circuit_breaker_triggered", {"subtask": subtask, "failure_type": failure_type, "count": failure_causes[cause_key]}, risk_level="high")
                    append_event(run_id, "run_paused", "coding", {"subtask": subtask, "reason": reason, "attempt": attempt_count})
                    append_checkpoint(run_id, "paused_by_circuit_breaker", "coding", {"subtask": subtask, "reason": reason, "attempt": attempt_count})
                    return f"⏸️ {reason}. Se requiere intervención manual."
                decision = recovery.decide(failure_type, attempt_count)
                strategy_used = decision.strategy
                if decision.action == "retry":
                    continue
                
                if decision.action == "replan":
                    reason = f"Re-planning dinámico iniciado por fallo en subtarea {i}: {failure_type}"
                    _trace_decision(run_id, "coding", "replan_requested", {"subtask": subtask, "reason": reason})
                    notify(f"🔄 {reason}")
                    
                    # Generar nueva spec basada en lo que queda
                    remaining_user_msg = f"Continuar tarea: {user_message}. Subtareas completadas: {list(completed_subtasks)}. Error en actual: {result.get('result', '')}"
                    new_spec, _ = generate_spec(remaining_user_msg, mode=mode, manual_model_key=manual_model_key)
                    
                    if new_spec.get("subtasks"):
                        state.spec["subtasks"] = new_spec["subtasks"]
                        update_run_state(run_id, spec=state.spec)
                        notify("✅ Nueva Spec generada. Reiniciando ejecución de subtareas restantes.")
                        # Reiniciar el bucle de subtareas con la nueva lista
                        return run_supervisor(user_message, progress_callback, run_id, completed_subtasks, mode, manual_model_key, task_id)

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

        self_review = run_self_review(state.modified_files, state.review)
        append_checkpoint(run_id, "self_review", "reviewing", self_review)
        _trace_decision(run_id, "reviewing", "self_review", {"debt_level": self_review.get("debt_level", "unknown")}, risk_level=self_review.get("debt_level", "low"))

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
        f"• Subtareas ejecutadas: {len(coding_subtasks)}",
        f"• Archivos modificados: {state.modified_files or 'ninguno'}",
        f"• Review: {verdict}",
        f"• Riesgo del plan: {plan_meta.get('risk', 'n/a')}",
        f"• Validación: {'✅ OK' if validation.get('passed') else '⚠️ Con errores'}",
        f"• Budget usado: llm_calls={run_llm_calls}, tool_calls={run_tool_calls}",
    ]
    return "\n".join(lines)


def resume_run(run_id: str, progress_callback=None) -> str:
    run_data = get_run_state(run_id)
    if not run_data:
        return f"❌ No existe run_id: {run_id}"

    if run_data.get("phase") == "done" and run_data.get("next_action") in ("done", ""):
        return f"✅ La corrida `{run_id}` ya está completada."

    if run_data.get("phase") == "failed":
        append_checkpoint(run_id, "resume_from_failed", "failed", {"next_action": run_data.get("next_action", "planning")})

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
        task_id=run_data.get("task_id") or None,
    )
    return (
        f"🔄 Reanudación iniciada desde acción `{next_action}` para `{run_id}`.\n"
        "ℹ️ Se continuó la corrida usando estado persistido por fase/subtarea.\n\n"
        f"{resumed_result}"
    )
