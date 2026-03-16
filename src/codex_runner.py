"""
Runner para Codex CLI — ejecuta tareas de código usando `codex exec`
con la sesión OAuth activa (~/.codex/auth.json), sin necesitar OPENAI_API_KEY.
Solo se usa para agent_role in ("coder", "tests").
"""
from __future__ import annotations
import os
import subprocess
import tempfile
import logging

log = logging.getLogger("codex_runner")

AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
TIMEOUT = int(os.getenv("CODEX_RUNNER_TIMEOUT", "180"))


def is_codex_session_active() -> bool:
    return os.path.exists(AUTH_PATH)


def run_codex_task(prompt: str, repo_path: str) -> str:
    """
    Ejecuta `codex exec --full-auto -C <repo_path> <prompt>`.
    Retorna el output del agente. Lanza RuntimeError si falla o timeout.
    """
    if not is_codex_session_active():
        raise RuntimeError("No hay sesión activa de Codex CLI.")

    try:
        result = subprocess.run(
            ["codex", "exec", "--full-auto", "-C", repo_path, prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        log.info(f"[codex_runner] exit={result.returncode} repo={repo_path}")

        # Detectar error en stderr aunque returncode sea 0
        if result.returncode != 0 or (result.stderr and result.stderr.strip().startswith("error:")):
            raise RuntimeError(f"codex exec falló (exit {result.returncode}): {(result.stderr or result.stdout)[:300]}")

        return result.stdout.strip() or "(sin output)"

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"codex exec timeout ({TIMEOUT}s)")
