from pathlib import Path
from src.workspace_context import get_active_repo_path


class PathSandboxError(Exception):
    pass


def resolve_repo_path(path: str) -> Path:
    repo_root = get_active_repo_path().resolve()
    p = Path(path)
    if not p.is_absolute():
        p = (repo_root / p).resolve()
    else:
        p = p.resolve()

    if not str(p).startswith(str(repo_root)):
        raise PathSandboxError(f"Ruta fuera del repositorio activo: {p}")
    return p
