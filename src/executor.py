"""Utilidades compartidas entre agent y coder para evitar importaciones circulares."""
import os
import json
import logging
import threading
from src.tools import TOOLS, TOOL_MAP
from src.shell_policy import run_with_policy

log = logging.getLogger("executor")

MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))

_cancel_event = threading.Event()


def cancel():
    _cancel_event.set()


def reset():
    _cancel_event.clear()


def is_cancelled():
    return _cancel_event.is_set()


def _safe_parse_args(tc) -> dict:
    try:
        return json.loads(tc.function.arguments)
    except Exception:
        return {}


def execute_tool_call(tc) -> tuple[str, dict, str]:
    """Ejecuta una tool call. Retorna (name, args, result). Nunca lanza excepción."""
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments)
    except json.JSONDecodeError as e:
        log.error("JSON inválido en tool %s: %s", name, e)
        return name, {}, f"Error: argumentos JSON inválidos en `{name}`"

    tool_def = next((t for t in TOOLS if t["function"]["name"] == name), None)
    if tool_def:
        required = tool_def["function"].get("parameters", {}).get("required", [])
        missing = [r for r in required if r not in args]
        if missing:
            log.warning("Args faltantes en %s: %s", name, missing)
            return name, args, f"Argumentos faltantes en `{name}`: {missing}"

    try:
        log.info("Ejecutando tool: %s args=%s", name, args)
        if name not in TOOL_MAP:
            return name, args, f"Tool no registrada: `{name}`"

        result = run_with_policy(name, lambda: TOOL_MAP[name](args))
        log.info("Tool %s OK: %s", name, str(result)[:120])
        return name, args, result
    except Exception as e:
        log.error("Error en tool %s: %s", name, e)
        return name, args, f"Error ejecutando `{name}`: {e}"
