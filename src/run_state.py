import json
import os
import sqlite3
import uuid
from typing import Any
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("RUNSTATE_DB_PATH", os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db"))
MAX_EVENT_HISTORY = int(os.getenv("RUNSTATE_MAX_EVENTS", "200"))
MAX_CHECKPOINT_HISTORY = int(os.getenv("RUNSTATE_MAX_CHECKPOINTS", "100"))
MAX_VALIDATION_HISTORY = int(os.getenv("RUNSTATE_MAX_VALIDATIONS", "50"))
MAX_ATTEMPT_HISTORY = int(os.getenv("RUNSTATE_MAX_ATTEMPTS", "200"))


def _ensure_parent(path: str):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str):
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _conn() -> sqlite3.Connection:
    _ensure_parent(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_states (
            run_id TEXT PRIMARY KEY,
            task_id TEXT,
            source_message TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL,
            next_action TEXT NOT NULL DEFAULT 'planning',
            current_subtask TEXT,
            current_subtask_index INTEGER NOT NULL DEFAULT 0,
            spec TEXT NOT NULL DEFAULT '{}',
            completed_subtasks TEXT NOT NULL DEFAULT '[]',
            modified_files TEXT NOT NULL DEFAULT '[]',
            validations TEXT NOT NULL DEFAULT '[]',
            events TEXT NOT NULL DEFAULT '[]',
            checkpoints TEXT NOT NULL DEFAULT '[]',
            attempts TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(conn, "run_states", "source_message", "source_message TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "run_states", "task_id", "task_id TEXT")
    _ensure_column(conn, "run_states", "next_action", "next_action TEXT NOT NULL DEFAULT 'planning'")
    _ensure_column(conn, "run_states", "checkpoints", "checkpoints TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "run_states", "attempts", "attempts TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "run_states", "current_subtask_index", "current_subtask_index INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "run_states", "spec", "spec TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "run_states", "completed_subtasks", "completed_subtasks TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            phase TEXT NOT NULL,
            decision_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}',
            cost_estimate REAL NOT NULL DEFAULT 0,
            risk_level TEXT NOT NULL DEFAULT 'low'
        )
        """
    )
    conn.commit()
    return conn


def _now() -> str:
    return datetime.utcnow().isoformat()


EVENT_TYPES = {
    "planning_started",
    "analysis_completed",
    "coding_failed",
    "review_rejected",
    "validation_passed",
    "guardrail_triggered",
    "run_paused",
    "run_resumed",
    "run_failed",
}

STALE_RUN_MINUTES = int(os.getenv("AGENT_STALE_RUN_MINUTES", "30"))


def cleanup_stale_runs() -> list[str]:
    """Marca como 'failed' runs activos sin actividad o con 0 eventos."""
    from datetime import datetime, timezone
    stale_ids = []
    for run in list_recent_runs(limit=100):
        if run.get("phase") in ("done", "failed", None):
            continue
        rid = run["run_id"]
        # Sin eventos: run zombie (nunca arrancó realmente)
        if len(run.get("events", [])) == 0:
            append_event(rid, "run_failed", "failed", {"reason": "stale: 0 events, zombie run"})
            update_run_state(rid, phase="failed")
            stale_ids.append(rid)
            continue
        # Con eventos pero sin actividad por más de STALE_RUN_MINUTES
        updated_at = run.get("updated_at", "")
        try:
            last = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_mins = (datetime.now(timezone.utc) - last).total_seconds() / 60
        except Exception:
            continue
        if age_mins > STALE_RUN_MINUTES:
            append_event(rid, "run_failed", "failed", {"reason": f"stale: no activity for {int(age_mins)}m"})
            update_run_state(rid, phase="failed")
            stale_ids.append(rid)
    return stale_ids


def _build_event(event_type: str, phase: str, details: dict[str, Any] | None = None) -> dict:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Tipo de evento no soportado: {event_type}")
    return {
        "type": event_type,
        "phase": phase,
        "timestamp": _now(),
        "details": details or {},
    }


def _loads(value: str, default):
    try:
        return json.loads(value) if value else default
    except Exception:
        return default


def _build_checkpoint(label: str, phase: str, data: dict[str, Any] | None = None) -> dict:
    return {
        "label": label,
        "phase": phase,
        "timestamp": _now(),
        "data": data or {},
    }


def create_run_state(initial_phase: str = "planning", source_message: str = "", task_id: str | None = None) -> str:
    run_id = str(uuid.uuid4())
    ts = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO run_states (
                run_id, task_id, source_message, phase, next_action, current_subtask, current_subtask_index,
                spec, completed_subtasks, modified_files, validations, events, checkpoints, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, task_id, source_message, initial_phase, initial_phase, "", 0, "{}", "[]", "[]", "[]", "[]", "[]", "[]", ts, ts),
        )
        conn.commit()
    return run_id


def get_run_state(run_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT run_id, phase, current_subtask, modified_files,
                   validations, events, checkpoints, attempts, source_message, created_at, updated_at,
                   next_action, current_subtask_index, spec, completed_subtasks
                   , task_id
            FROM run_states
            WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()

    if not row:
        return None

    return {
        "run_id": row[0],
        "phase": row[1],
        "current_subtask": row[2] or "",
        "modified_files": _loads(row[3], []),
        "validations": _loads(row[4], []),
        "events": _loads(row[5], []),
        "checkpoints": _loads(row[6], []),
        "attempts": _loads(row[7], []),
        "source_message": row[8] or "",
        "created_at": row[9],
        "updated_at": row[10],
        "next_action": row[11] or "planning",
        "current_subtask_index": int(row[12] or 0),
        "spec": _loads(row[13], {}),
        "completed_subtasks": _loads(row[14], []),
        "task_id": row[15] or "",
    }


def update_run_state(
    run_id: str,
    *,
    phase: str | None = None,
    source_message: str | None = None,
    task_id: str | None = None,
    next_action: str | None = None,
    current_subtask: str | None = None,
    current_subtask_index: int | None = None,
    spec: dict | None = None,
    completed_subtasks: list[str] | None = None,
    modified_files: list[str] | None = None,
    validations: list[dict] | None = None,
    events: list[dict] | None = None,
    checkpoints: list[dict] | None = None,
    attempts: list[dict] | None = None,
) -> bool:
    current = get_run_state(run_id)
    if not current:
        return False

    next_phase = phase if phase is not None else current["phase"]
    next_source_message = source_message if source_message is not None else current["source_message"]
    next_task_id = task_id if task_id is not None else current.get("task_id", "")
    next_action_value = next_action if next_action is not None else current.get("next_action", "planning")
    next_subtask = current_subtask if current_subtask is not None else current["current_subtask"]
    next_subtask_index = current_subtask_index if current_subtask_index is not None else current.get("current_subtask_index", 0)
    next_spec = spec if spec is not None else current.get("spec", {})
    next_completed_subtasks = completed_subtasks if completed_subtasks is not None else current.get("completed_subtasks", [])
    next_files = modified_files if modified_files is not None else current["modified_files"]
    next_validations = validations if validations is not None else current["validations"]
    next_events = events if events is not None else current["events"]
    next_checkpoints = checkpoints if checkpoints is not None else current["checkpoints"]
    next_attempts = attempts if attempts is not None else current.get("attempts", [])

    with _conn() as conn:
        conn.execute(
            """
            UPDATE run_states
            SET phase=?, task_id=?, source_message=?, next_action=?, current_subtask=?, current_subtask_index=?, spec=?,
                completed_subtasks=?, modified_files=?, validations=?, events=?, checkpoints=?, attempts=?, updated_at=?
            WHERE run_id=?
            """,
            (
                next_phase,
                next_task_id,
                next_source_message,
                next_action_value,
                next_subtask,
                next_subtask_index,
                json.dumps(next_spec, ensure_ascii=False),
                json.dumps(next_completed_subtasks, ensure_ascii=False),
                json.dumps(next_files, ensure_ascii=False),
                json.dumps(next_validations, ensure_ascii=False),
                json.dumps(next_events, ensure_ascii=False),
                json.dumps(next_checkpoints, ensure_ascii=False),
                json.dumps(next_attempts, ensure_ascii=False),
                _now(),
                run_id,
            ),
        )
        conn.commit()
    return True


def append_modified_files(run_id: str, files: list[str]) -> bool:
    current = get_run_state(run_id)
    if not current:
        return False

    merged = list(dict.fromkeys([*current["modified_files"], *files]))
    return update_run_state(run_id, modified_files=merged)


def append_validation(run_id: str, validation: dict) -> bool:
    current = get_run_state(run_id)
    if not current:
        return False

    validations = [*current["validations"], validation][-MAX_VALIDATION_HISTORY:]
    return update_run_state(run_id, validations=validations)


def append_event(run_id: str, event_type: str, phase: str, details: dict[str, Any] | None = None) -> bool:
    current = get_run_state(run_id)
    if not current:
        return False

    events = [*current["events"], _build_event(event_type=event_type, phase=phase, details=details)][-MAX_EVENT_HISTORY:]
    return update_run_state(run_id, events=events)


def append_checkpoint(run_id: str, label: str, phase: str, data: dict[str, Any] | None = None) -> bool:
    current = get_run_state(run_id)
    if not current:
        return False

    checkpoints = [*current["checkpoints"], _build_checkpoint(label=label, phase=phase, data=data)][-MAX_CHECKPOINT_HISTORY:]
    return update_run_state(run_id, checkpoints=checkpoints)


def get_latest_checkpoint(run_id: str) -> dict | None:
    current = get_run_state(run_id)
    if not current:
        return None
    cps = current.get("checkpoints", [])
    return cps[-1] if cps else None


def append_attempt(run_id: str, attempt: dict) -> bool:
    current = get_run_state(run_id)
    if not current:
        return False

    attempts = [*current.get("attempts", []), attempt][-MAX_ATTEMPT_HISTORY:]
    return update_run_state(run_id, attempts=attempts)


def list_recent_runs(limit: int = 10) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT run_id, phase, current_subtask, updated_at, source_message
                   , task_id
            FROM run_states
            ORDER BY datetime(updated_at) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "run_id": r[0],
            "phase": r[1],
            "current_subtask": r[2] or "",
            "updated_at": r[3],
            "source_message": r[4] or "",
            "task_id": r[5] or "",
        }
        for r in rows
    ]


def get_latest_run() -> dict | None:
    runs = list_recent_runs(1)
    return runs[0] if runs else None


def get_latest_active_run() -> dict | None:
    runs = list_recent_runs(50)
    for r in runs:
        if r.get("phase") not in ("done", "failed"):
            return r
    return None


def append_decision(run_id: str, phase: str, decision_type: str, actor: str,
                    details: dict[str, Any] | None = None,
                    cost_estimate: float = 0,
                    risk_level: str = "low") -> bool:
    if not get_run_state(run_id):
        return False
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO run_decisions(run_id, timestamp, phase, decision_type, actor, details, cost_estimate, risk_level)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _now(),
                phase,
                decision_type,
                actor,
                json.dumps(details or {}, ensure_ascii=False),
                float(cost_estimate or 0),
                risk_level or "low",
            ),
        )
        conn.commit()
    return True


def list_run_decisions(run_id: str, limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, phase, decision_type, actor, details, cost_estimate, risk_level
            FROM run_decisions
            WHERE run_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "timestamp": r[0],
            "phase": r[1],
            "decision_type": r[2],
            "actor": r[3],
            "details": _loads(r[4], {}),
            "cost_estimate": float(r[5] or 0),
            "risk_level": r[6] or "low",
        })
    return out
