"""
Resuelve nombre corto de repo → URL SSH de GitHub.
"""
from __future__ import annotations
import os
import subprocess

GITHUB_USER = os.getenv("GITHUB_USER", "")


def resolve_repo_url(name_or_url: str) -> str:
    """
    - URL completa (https:// o git@) → retorna tal cual
    - Nombre corto (ej. 'agent-serve') → git@github.com:<GITHUB_USER>/<name>.git
    """
    s = name_or_url.strip()
    if s.startswith("https://") or s.startswith("git@"):
        return s
    if not GITHUB_USER:
        raise ValueError("GITHUB_USER no configurado en .env")
    return f"git@github.com:{GITHUB_USER}/{s}.git"


def repo_name_from_url(url: str) -> str:
    """Extrae el nombre del repo desde la URL."""
    return url.rstrip("/").rstrip(".git").split("/")[-1]


def default_branch(repo_path: str) -> str:
    """Detecta la branch principal del repo (main o master)."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True, text=True, cwd=repo_path
        )
        if result.returncode == 0:
            return result.stdout.strip().split("/")[-1]
    except Exception:
        pass
    return "main"
