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
            source_message TEXT NOT NULL DEFAULT '',
            phase TEXT NOT NULL,
            current_subtask TEXT,
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
    _ensure_column(conn, "run_states", "checkpoints", "checkpoints TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "run_states", "attempts", "attempts TEXT NOT NULL DEFAULT '[]'")
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
}


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


def create_run_state(initial_phase: str = "planning", source_message: str = "") -> str:
    run_id = str(uuid.uuid4())
    ts = _now()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO run_states (
                run_id, source_message, phase, current_subtask,
                modified_files, validations, events, checkpoints, attempts,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, source_message, initial_phase, "", "[]", "[]", "[]", "[]", "[]", ts, ts),
        )
        conn.commit()
    return run_id


def get_run_state(run_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT run_id, phase, current_subtask, modified_files,
                   validations, events, checkpoints, attempts, source_message, created_at, updated_at
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
    }


def update_run_state(
    run_id: str,
    *,
    phase: str | None = None,
    source_message: str | None = None,
    current_subtask: str | None = None,
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
    next_subtask = current_subtask if current_subtask is not None else current["current_subtask"]
    next_files = modified_files if modified_files is not None else current["modified_files"]
    next_validations = validations if validations is not None else current["validations"]
    next_events = events if events is not None else current["events"]
    next_checkpoints = checkpoints if checkpoints is not None else current["checkpoints"]
    next_attempts = attempts if attempts is not None else current.get("attempts", [])

    with _conn() as conn:
        conn.execute(
            """
            UPDATE run_states
            SET phase=?, source_message=?, current_subtask=?, modified_files=?,
                validations=?, events=?, checkpoints=?, attempts=?, updated_at=?
            WHERE run_id=?
            """,
            (
                next_phase,
                next_source_message,
                next_subtask,
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
