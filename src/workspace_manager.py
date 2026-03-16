import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import git

DB_PATH = os.getenv("RUNSTATE_DB_PATH", os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db"))
REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve")).resolve()
WORKSPACE_ROOT = Path(os.getenv("WORKSPACE_ROOT", "/tmp/agent-workspaces")).resolve()


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_sessions (
            chat_id TEXT PRIMARY KEY,
            repo_url TEXT,
            notion_database_id TEXT,
            task_mode TEXT DEFAULT "local",
            repo_path TEXT NOT NULL,
            active_branch TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    try:
        conn.execute("ALTER TABLE workspace_sessions ADD COLUMN task_mode TEXT DEFAULT 'local'")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return cleaned[:40] or "task"


def _safe_repo_dir(repo_url: str) -> str:
    base = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    return _slug(base) or "repo"


def _validate_no_main(branch: str):
    if (branch or "").strip() in {"main", "master"}:
        raise WorkspaceError("No se permite trabajar directamente sobre main/master.")


class WorkspaceManager:
    def __init__(self, repo_path: Path | None = None):
        self.repo_path = (repo_path or REPO_PATH).resolve()

    def get_active_workspace(self, chat_id: str | int | None = None) -> dict:
        chat_key = str(chat_id or "legacy")
        with _conn() as conn:
            row = conn.execute(
                """
                SELECT chat_id, repo_url, notion_database_id, task_mode, repo_path, active_branch, created_at, updated_at
                FROM workspace_sessions WHERE chat_id=?
                """,
                (chat_key,),
            ).fetchone()
        if row:
            return {
                "chat_id": row[0],
                "repo_url": row[1] or "",
                "notion_database_id": row[2] or "",
                "task_mode": row[3] or "local",
                "repo_path": row[4],
                "active_branch": row[5],
                "created_at": row[6],
                "updated_at": row[7],
            }
        # fallback legacy
        return {
            "chat_id": chat_key,
            "repo_url": "",
            "notion_database_id": "",
            "task_mode": "local",
            "repo_path": str(self.repo_path),
            "active_branch": self._current_branch(self.repo_path),
            "created_at": _now(),
            "updated_at": _now(),
        }

    def set_active_workspace(self, chat_id: str | int, repo_url: str, notion_database_id: str, branch: str) -> dict:
        _validate_no_main(branch)
        repo_url = (repo_url or "").strip()
        if not repo_url:
            raise WorkspaceError("repo_url es obligatorio")

        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        repo_dir = WORKSPACE_ROOT / _safe_repo_dir(repo_url)
        if (repo_dir / ".git").exists():
            repo = git.Repo(str(repo_dir))
            repo.remotes.origin.fetch()
        else:
            repo = git.Repo.clone_from(repo_url, str(repo_dir))

        self._checkout_branch(repo, branch)

        chat_key = str(chat_id)
        ts = _now()
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO workspace_sessions(chat_id, repo_url, notion_database_id, task_mode, repo_path, active_branch, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    repo_url=excluded.repo_url,
                    notion_database_id=excluded.notion_database_id,
                    task_mode=excluded.task_mode,
                    repo_path=excluded.repo_path,
                    active_branch=excluded.active_branch,
                    updated_at=excluded.updated_at
                """,
                (chat_key, repo_url, notion_database_id, "local", str(repo_dir), branch, ts, ts),
            )
            conn.commit()
        return self.get_active_workspace(chat_key)

    def set_active_branch(self, chat_id: str | int, branch: str) -> dict:
        _validate_no_main(branch)
        ws = self.get_active_workspace(chat_id)
        repo = git.Repo(ws["repo_path"])
        self._checkout_branch(repo, branch)
        with _conn() as conn:
            conn.execute(
                "UPDATE workspace_sessions SET active_branch=?, updated_at=? WHERE chat_id=?",
                (branch, _now(), str(chat_id)),
            )
            conn.commit()
        return self.get_active_workspace(chat_id)

    def set_task_mode(self, chat_id: str | int, mode: str) -> dict:
        mode = (mode or "local").lower()
        if mode not in {"local", "notion", "hybrid"}:
            raise WorkspaceError("Modo inválido. Usa local|notion|hybrid")
        with _conn() as conn:
            conn.execute(
                "UPDATE workspace_sessions SET task_mode=?, updated_at=? WHERE chat_id=?",
                (mode, _now(), str(chat_id)),
            )
            conn.commit()
        return self.get_active_workspace(chat_id)

    def _checkout_branch(self, repo: git.Repo, branch: str):
        if branch in [h.name for h in repo.heads]:
            repo.heads[branch].checkout()
            return
        origin_branch = f"origin/{branch}"
        if origin_branch in [r.name for r in repo.refs]:
            repo.git.checkout("-b", branch, origin_branch)
        else:
            repo.create_head(branch).checkout()

    @staticmethod
    def _current_branch(repo_path: Path) -> str:
        try:
            return git.Repo(str(repo_path)).active_branch.name
        except Exception:
            return "unknown"

    # Compatibilidad con supervisor actual
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
        if repo.is_dirty(untracked_files=False):
            raise WorkspaceError("Repositorio con cambios sin commitear; no se puede crear branch de tarea.")

        # Si estamos en una branch task/* anterior, volver a la base del workspace activo
        current = repo.active_branch.name
        if current.startswith("task/"):
            # Buscar la base guardada en workspace_sessions
            with _conn() as conn:
                row = conn.execute(
                    "SELECT active_branch FROM workspace_sessions WHERE repo_path=? LIMIT 1",
                    (str(self.repo_path),),
                ).fetchone()
            base = (row[0] if row else None) or "agent/work"
            if base in [h.name for h in repo.heads]:
                repo.heads[base].checkout()
            elif "agent/work" in [h.name for h in repo.heads]:
                repo.heads["agent/work"].checkout()

        base_branch = repo.active_branch.name
        short_run = run_id.split("-")[0]
        branch_name = f"task/{short_run}-{_slug(task_message)}"

        _validate_no_main(branch_name)
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
