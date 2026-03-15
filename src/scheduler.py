from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
import os

_scheduler = BackgroundScheduler()
_scheduler.start()

# Callback global para enviar mensajes (se setea desde main.py)
_send_callback = None


def set_send_callback(fn):
    global _send_callback
    _send_callback = fn


def _run_task(task_id: str, command: str):
    if _send_callback:
        _send_callback(f"⏰ Tarea programada `{task_id}`: {command}")


def schedule_task(task_id: str, cron_expr: str, command: str) -> str:
    """
    cron_expr: expresión cron estándar "minuto hora día mes día_semana"
    Ejemplo: "0 9 * * 1" = lunes a las 9am
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return "cron_expr debe tener 5 campos: minuto hora día mes día_semana"
        trigger = CronTrigger(
            minute=parts[0], hour=parts[1],
            day=parts[2], month=parts[3], day_of_week=parts[4]
        )
        _scheduler.add_job(_run_task, trigger, id=task_id,
                           args=[task_id, command], replace_existing=True)
        return f"Tarea `{task_id}` programada: {cron_expr} → {command}"
    except Exception as e:
        return f"Error programando tarea: {e}"


def list_tasks() -> str:
    jobs = _scheduler.get_jobs()
    if not jobs:
        return "Sin tareas programadas"
    return "\n".join(f"- `{j.id}`: próxima ejecución {j.next_run_time}" for j in jobs)


def remove_task(task_id: str) -> str:
    try:
        _scheduler.remove_job(task_id)
        return f"Tarea `{task_id}` eliminada"
    except Exception as e:
        return f"Error: {e}"
