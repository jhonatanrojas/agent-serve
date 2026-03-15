import logging
from dataclasses import dataclass, field
from typing import Literal

from src.planner import plan_task, generate_spec
from src.analyst import analyze_codebase
from src.coder import run_coder
from src.reviewer import run_reviewer, format_review
from src.validator import run_validation, format_validation
from src.executor import is_cancelled

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


def run_supervisor(user_message: str, progress_callback=None) -> str:
    """
    Orquesta el flujo completo para tareas complejas.
    Decide dinámicamente qué subagente invocar en cada paso.
    """
    state = SupervisorState(message=user_message)
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

    state.spec_summary = spec_summary
    state.spec = generate_spec(user_message)
    state.stage = "analyzing"
    notify(spec_summary)

    # ── Stage: analyzing ─────────────────────────────────────────────────────
    if is_cancelled():
        return "⛔ Cancelado."

    if not state.record_agent_call("analyst"):
        state.errors.append("Loop en analyst")
        state.stage = "coding"  # saltar análisis si hay loop
    else:
        notify("🔍 Analizando codebase...")
        state.analysis = analyze_codebase(user_message)
        state.stage = "coding"
        notify(state.analysis)

    # ── Stage: coding ────────────────────────────────────────────────────────
    subtasks = state.spec.get("subtasks", [])
    if not subtasks:
        notify("⚠️ La spec no tiene subtareas definidas. Ejecutando como tarea simple.")
        state.stage = "done"
        return "__SIMPLE__"

    context = f"Spec:\n{state.spec_summary}\n\nAnálisis:\n{state.analysis}"

    for i, subtask in enumerate(subtasks, 1):
        if is_cancelled():
            return "⛔ Cancelado durante codificación."

        if not state.record_agent_call(f"coder_{i}"):
            notify(f"🔁 Loop detectado en coder subtarea {i}. Saltando.")
            state.errors.append(f"Loop en coder subtarea {i}: {subtask}")
            continue

        notify(f"🔨 Subtarea {i}/{len(subtasks)}: {subtask}")
        result = run_coder(subtask, context=context, progress_callback=progress_callback)
        state.modified_files.extend(result.get("modified_files", []))
        status = result.get("status", "unknown")

        if status in ("loop_detected", "error"):
            state.errors.append(f"Subtarea {i} falló: {result.get('result', '')[:100]}")
            notify(f"⚠️ Subtarea {i} con problemas: {status}")
        else:
            notify(f"✅ Subtarea {i} lista. Archivos: {result.get('modified_files', [])}")

    state.modified_files = list(set(state.modified_files))
    state.stage = "reviewing"

    # ── Stage: reviewing ─────────────────────────────────────────────────────
    if is_cancelled():
        return "⛔ Cancelado antes del review."

    if not state.record_agent_call("reviewer"):
        notify("🔁 Loop en reviewer. Saltando revisión.")
    else:
        notify("🔍 Revisando cambios...")
        criteria = state.spec.get("acceptance_criteria", [])
        state.review = run_reviewer(state.spec_summary, state.modified_files, criteria)
        review_msg = format_review(state.review)
        notify(review_msg)

    state.stage = "done"

    # ── Validación técnica ───────────────────────────────────────────────────
    if state.modified_files:
        notify("🔬 Ejecutando validación técnica...")
        validation = run_validation(state.modified_files)
        validation_msg = format_validation(validation)
        notify(validation_msg)
    else:
        validation = {"passed": True}

    # ── Resumen final ────────────────────────────────────────────────────────
    verdict = state.review.get("verdict", "SIN REVIEW")
    lines = [
        "🏁 **Tarea completada**",
        f"• Subtareas ejecutadas: {len(subtasks)}",
        f"• Archivos modificados: {state.modified_files or 'ninguno'}",
        f"• Review: {verdict}",
        f"• Validación: {'✅ OK' if validation.get('passed') else '⚠️ Con errores'}",
    ]
    if state.errors:
        lines.append(f"• ⚠️ Errores: {'; '.join(state.errors)}")

    return "\n".join(lines)
