import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("RUNSTATE_DB_PATH", os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db"))


def _now() -> str:
    return datetime.utcnow().isoformat()


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_sessions (
            chat_id TEXT PRIMARY KEY,
            current_run_id TEXT,
            current_task_id TEXT,
            pending_pr_json TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def set_session(chat_id: int | str, current_run_id: str | None = None, current_task_id: str | None = None):
    chat_key = str(chat_id)
    with _conn() as conn:
        row = conn.execute("SELECT current_run_id, current_task_id, pending_pr_json FROM runtime_sessions WHERE chat_id=?", (chat_key,)).fetchone()
        run_id = current_run_id if current_run_id is not None else (row[0] if row else None)
        task_id = current_task_id if current_task_id is not None else (row[1] if row else None)
        pending = row[2] if row else None
        conn.execute(
            """
            INSERT INTO runtime_sessions(chat_id, current_run_id, current_task_id, pending_pr_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                current_run_id=excluded.current_run_id,
                current_task_id=excluded.current_task_id,
                pending_pr_json=excluded.pending_pr_json,
                updated_at=excluded.updated_at
            """,
            (chat_key, run_id, task_id, pending, _now()),
        )
        conn.commit()


def set_pending_pr(chat_id: int | str, data: dict):
    chat_key = str(chat_id)
    with _conn() as conn:
        row = conn.execute("SELECT current_run_id, current_task_id FROM runtime_sessions WHERE chat_id=?", (chat_key,)).fetchone()
        run_id = row[0] if row else None
        task_id = row[1] if row else None
        conn.execute(
            """
            INSERT INTO runtime_sessions(chat_id, current_run_id, current_task_id, pending_pr_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                current_run_id=excluded.current_run_id,
                current_task_id=excluded.current_task_id,
                pending_pr_json=excluded.pending_pr_json,
                updated_at=excluded.updated_at
            """,
            (chat_key, run_id, task_id, json.dumps(data, ensure_ascii=False), _now()),
        )
        conn.commit()


def get_pending_pr(chat_id: int | str) -> dict | None:
    chat_key = str(chat_id)
    with _conn() as conn:
        row = conn.execute("SELECT pending_pr_json FROM runtime_sessions WHERE chat_id=?", (chat_key,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def clear_pending_pr(chat_id: int | str):
    chat_key = str(chat_id)
    with _conn() as conn:
        conn.execute("UPDATE runtime_sessions SET pending_pr_json=NULL, updated_at=? WHERE chat_id=?", (_now(), chat_key))
        conn.commit()
