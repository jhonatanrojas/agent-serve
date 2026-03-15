import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import git

DB_PATH = os.getenv("RUNSTATE_DB_PATH", os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db"))
REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve")).resolve()


class WorkspaceError(Exception):
    pass


def _now() -> str:
    return datetime.utcnow().isoformat()


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_metadata (
            run_id TEXT PRIMARY KEY,
            branch_name TEXT NOT NULL,
            workspace_path TEXT NOT NULL,
            base_branch TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned[:40] or "task"


def _validate_workspace_path(path: Path, root: Path):
    resolved = path.resolve()
    allowed_root = root.resolve()
    if not str(resolved).startswith(str(allowed_root)):
        raise WorkspaceError(f"Ruta de workspace inválida: {resolved}")


class WorkspaceManager:
    def __init__(self, repo_path: Path | None = None):
        configured_root = REPO_PATH.resolve()
        self.repo_path = (repo_path or configured_root).resolve()
        root = self.repo_path if repo_path is not None else configured_root
        _validate_workspace_path(self.repo_path, root)

    def get_metadata(self, run_id: str) -> dict | None:
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT run_id, branch_name, workspace_path, base_branch, created_at, updated_at
                FROM workspace_metadata WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "run_id": row[0],
            "branch_name": row[1],
            "workspace_path": row[2],
            "base_branch": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    def create_or_get_workspace(self, run_id: str, task_message: str) -> dict:
        existing = self.get_metadata(run_id)
        if existing:
            return existing

        repo = git.Repo(str(self.repo_path))
        if repo.is_dirty(untracked_files=True):
            raise WorkspaceError("Repositorio con cambios sin commitear; no se puede crear branch de tarea.")

        base_branch = repo.active_branch.name
        short_run = run_id.split("-")[0]
        branch_name = f"task/{short_run}-{_slug(task_message)}"

        if branch_name in [h.name for h in repo.heads]:
            branch_ref = repo.heads[branch_name]
        else:
            branch_ref = repo.create_head(branch_name)

        branch_ref.checkout()

        ts = _now()
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO workspace_metadata (run_id, branch_name, workspace_path, base_branch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, branch_name, str(self.repo_path), base_branch, ts, ts),
            )
            conn.commit()

        return {
            "run_id": run_id,
            "branch_name": branch_name,
            "workspace_path": str(self.repo_path),
            "base_branch": base_branch,
            "created_at": ts,
            "updated_at": ts,
        }
