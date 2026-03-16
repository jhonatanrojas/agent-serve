import os
from pathlib import Path

_ACTIVE_REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve")).resolve()


def set_active_repo_path(repo_path: str):
    global _ACTIVE_REPO_PATH
    _ACTIVE_REPO_PATH = Path(repo_path).resolve()


def get_active_repo_path() -> Path:
    return _ACTIVE_REPO_PATH
