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
        CREATE TABLE IF NOT EXISTS git_gate_state (
            branch_name TEXT PRIMARY KEY,
            last_validation_ok INTEGER NOT NULL DEFAULT 0,
            approved_push INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _norm_branch(branch_name: str) -> str:
    return (branch_name or "").strip()


def _ensure_branch(branch_name: str):
    branch = _norm_branch(branch_name)
    with _conn() as conn:
        row = conn.execute("SELECT branch_name FROM git_gate_state WHERE branch_name=?", (branch,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO git_gate_state (branch_name, last_validation_ok, approved_push, updated_at) VALUES (?, 0, 0, ?)",
                (branch, _now()),
            )
        conn.commit()


def _set(branch_name: str, last_validation_ok: int | None = None, approved_push: int | None = None):
    branch = _norm_branch(branch_name)
    _ensure_branch(branch)
    with _conn() as conn:
        cur = conn.execute(
            "SELECT last_validation_ok, approved_push FROM git_gate_state WHERE branch_name=?",
            (branch,),
        ).fetchone()
        next_validation = int(last_validation_ok if last_validation_ok is not None else cur[0])
        next_approved = int(approved_push if approved_push is not None else cur[1])
        conn.execute(
            "UPDATE git_gate_state SET last_validation_ok=?, approved_push=?, updated_at=? WHERE branch_name=?",
            (next_validation, next_approved, _now(), branch),
        )
        conn.commit()


def mark_validation_result(passed: bool, branch_name: str):
    _set(branch_name, last_validation_ok=1 if passed else 0)


def approve_push(branch_name: str) -> str:
    branch = _norm_branch(branch_name)
    if branch in {"main", "master"}:
        return "Aprobación denegada: push a main/master está bloqueado"
    _set(branch, approved_push=1)
    return f"Push aprobado explícitamente para branch `{branch}`"


def clear_push_approval(branch_name: str):
    _set(branch_name, approved_push=0)


def can_commit(branch_name: str) -> tuple[bool, str]:
    branch = _norm_branch(branch_name)
    _ensure_branch(branch)
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_validation_ok FROM git_gate_state WHERE branch_name=?",
            (branch,),
        ).fetchone()
    ok = bool(row and row[0] == 1)
    if ok:
        return True, "OK"
    return False, f"Commit bloqueado en `{branch}`: la última validación no está en estado OK"


def can_push(branch_name: str) -> tuple[bool, str]:
    branch = _norm_branch(branch_name)
    if branch in {"main", "master"}:
        return False, "Push bloqueado: no se permite push directo a main/master"

    _ensure_branch(branch)
    with _conn() as conn:
        row = conn.execute(
            "SELECT approved_push FROM git_gate_state WHERE branch_name=?",
            (branch,),
        ).fetchone()
    approved = bool(row and row[0] == 1)
    if not approved:
        return False, f"Push bloqueado: falta aprobación explícita para `{branch}`"
    return True, "OK"
