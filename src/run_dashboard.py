from src.run_state import get_run_state


def _next_step(phase: str) -> str:
    return {
        "planning": "analyzing",
        "analyzing": "coding",
        "coding": "reviewing",
        "reviewing": "validation",
        "done": "none",
    }.get(phase or "planning", "unknown")


def build_run_dashboard(run_id: str) -> str:
    run = get_run_state(run_id)
    if not run:
        return f"❌ Run no encontrado: {run_id}"

    validations = run.get("validations", [])
    last_validation = validations[-1] if validations else {}
    val_text = "sin validación"
    if last_validation:
        val_text = "✅ OK" if last_validation.get("passed") else "⚠️ con errores"

    lines = [
        "📊 **Run Dashboard**",
        f"• Run ID: {run.get('run_id')}",
        f"• Fase actual: {run.get('phase', 'unknown')}",
        f"• Subtarea: {run.get('current_subtask') or 'n/a'}",
        f"• Archivos modificados: {len(run.get('modified_files', []))}",
        f"• Validación: {val_text}",
        f"• Próximo paso: {_next_step(run.get('phase', 'planning'))}",
    ]
    return "\n".join(lines)


def build_run_logs(run_id: str, limit: int = 12) -> str:
    run = get_run_state(run_id)
    if not run:
        return f"❌ Run no encontrado: {run_id}"

    events = run.get("events", [])[-limit:]
    if not events:
        return "ℹ️ No hay eventos registrados"

    lines = ["🧾 **Run Logs**"]
    for ev in events:
        if not isinstance(ev, dict):
            continue
        lines.append(f"- [{ev.get('phase','?')}] {ev.get('type','?')} @ {ev.get('timestamp','')}")
    return "\n".join(lines)


def build_run_plan(run_id: str) -> str:
    run = get_run_state(run_id)
    if not run:
        return f"❌ Run no encontrado: {run_id}"

    checkpoints = run.get("checkpoints", [])
    subtasks = []
    for cp in checkpoints:
        if isinstance(cp, dict) and cp.get("label") == "subtask_started":
            st = cp.get("data", {}).get("subtask")
            if st and st not in subtasks:
                subtasks.append(st)

    lines = [
        "🗺️ **Plan del run**",
        f"• Mensaje origen: {run.get('source_message','')[:160] or 'n/a'}",
        f"• Fase actual: {run.get('phase','unknown')}",
    ]
    if subtasks:
        lines.append("• Subtareas detectadas:")
        lines.extend([f"  - {s}" for s in subtasks[:20]])
    else:
        lines.append("• Subtareas detectadas: n/a")
    return "\n".join(lines)
