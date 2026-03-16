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
    Ejecuta `codex exec --full-auto -C <repo_path> -o <tmpfile> <prompt>`.
    Retorna el texto de la última respuesta del agente.
    Lanza RuntimeError si falla o timeout.
    """
    if not is_codex_session_active():
        raise RuntimeError("No hay sesión activa de Codex CLI.")

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        out_file = f.name

    try:
        result = subprocess.run(
            [
                "codex", "exec",
                "--full-auto",
                "--dangerously-bypass-approvals-and-sandbox",
                "-C", repo_path,
                "-o", out_file,
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        log.info(f"[codex_runner] exit={result.returncode} repo={repo_path}")

        if result.returncode != 0:
            raise RuntimeError(f"codex exec falló (exit {result.returncode}): {result.stderr[:300]}")

        output = ""
        if os.path.exists(out_file):
            with open(out_file) as f:
                output = f.read().strip()

        return output or result.stdout.strip() or "(sin output)"

    except subprocess.TimeoutExpired:
        raise RuntimeError(f"codex exec timeout ({TIMEOUT}s)")
    finally:
        if os.path.exists(out_file):
            os.unlink(out_file)
