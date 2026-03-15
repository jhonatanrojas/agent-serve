import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("RUNSTATE_DB_PATH", os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db"))
REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve"))

_CODE_EXTS = {".py", ".js", ".ts", ".json", ".md", ".yaml", ".yml", ".toml"}
_IGNORE = {".git", "venv", "__pycache__", "node_modules", ".serena", ".mem0"}


def _now() -> str:
    return datetime.utcnow().isoformat()


def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS repo_map (
            repo_path TEXT PRIMARY KEY,
            map_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _iter_code_files(repo_path: Path):
    for p in repo_path.rglob("*"):
        if any(part in _IGNORE for part in p.parts):
            continue
        if p.is_file() and p.suffix in _CODE_EXTS:
            yield p


def _extract_py_symbols(content: str) -> list[str]:
    symbols = []
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("def "):
            name = s[4:].split("(")[0].strip()
            if name:
                symbols.append(name)
        elif s.startswith("class "):
            name = s[6:].split("(")[0].split(":")[0].strip()
            if name:
                symbols.append(name)
    return symbols[:30]


def _extract_deps(rel: str, content: str) -> list[str]:
    deps = []
    if rel.endswith(".py"):
        for line in content.splitlines()[:200]:
            s = line.strip()
            if s.startswith("import "):
                deps.append(s.replace("import ", "", 1).split()[0])
            elif s.startswith("from "):
                deps.append(s.replace("from ", "", 1).split()[0])
    elif rel.endswith((".json", ".toml")):
        for m in re.findall(r'"([a-zA-Z0-9_\-]+)"\s*:\s*"?[\^~]?[0-9]', content[:4000]):
            deps.append(m)
    return deps[:30]


def build_repo_map(repo_path: Path | None = None) -> dict:
    repo = (repo_path or REPO_PATH).resolve()
    modules = []
    dependencies = set()
    symbols = {}
    tests_related = []

    for p in _iter_code_files(repo):
        rel = str(p.relative_to(repo))
        try:
            content = p.read_text(errors="ignore")
        except Exception:
            continue

        modules.append(rel)
        for d in _extract_deps(rel, content):
            dependencies.add(d)

        if rel.endswith(".py"):
            syms = _extract_py_symbols(content)
            if syms:
                symbols[rel] = syms

        name = p.name.lower()
        if "test" in name or rel.startswith("tests/"):
            tests_related.append(rel)

    validation_commands = ["ruff check --select=E,F,W <changed_files>", "python -m py_compile <changed_files>"]

    return {
        "repo_path": str(repo),
        "modules": sorted(modules),
        "dependencies": sorted(dependencies),
        "symbols": symbols,
        "tests_related": sorted(set(tests_related)),
        "validation_commands": validation_commands,
        "updated_at": _now(),
    }


def save_repo_map(repo_map: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO repo_map (repo_path, map_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(repo_path) DO UPDATE SET map_json=excluded.map_json, updated_at=excluded.updated_at
            """,
            (repo_map.get("repo_path", str(REPO_PATH.resolve())), json.dumps(repo_map, ensure_ascii=False), _now()),
        )
        conn.commit()


def load_repo_map(repo_path: Path | None = None) -> dict | None:
    repo = str((repo_path or REPO_PATH).resolve())
    with _conn() as conn:
        row = conn.execute("SELECT map_json FROM repo_map WHERE repo_path=?", (repo,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def get_or_build_repo_map(repo_path: Path | None = None) -> dict:
    loaded = load_repo_map(repo_path)
    if loaded:
        return loaded
    built = build_repo_map(repo_path)
    save_repo_map(built)
    return built


def refresh_repo_map(changed_files: list[str] | None = None, repo_path: Path | None = None) -> dict:
    # Implementación incremental segura: reconstrucción completa (evita inconsistencia parcial)
    built = build_repo_map(repo_path)
    save_repo_map(built)
    return built
