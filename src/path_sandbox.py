import os
from pathlib import Path

REPO_ROOT = Path(os.getenv("REPO_PATH", "/root/agent-serve")).resolve()


class PathSandboxError(Exception):
    pass


def resolve_repo_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = (REPO_ROOT / p).resolve()
    else:
        p = p.resolve()

    if not str(p).startswith(str(REPO_ROOT)):
        raise PathSandboxError(f"Ruta fuera del repositorio: {p}")
    return p
