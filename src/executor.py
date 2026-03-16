"""Utilidades compartidas entre agent y coder para evitar importaciones circulares."""
import os
import json
import logging
import threading
from src.tools import TOOLS, TOOL_MAP
from src.shell_policy import run_with_policy

log = logging.getLogger("executor")

# Callback opcional para live updates (seteado desde main.py)
_live_callback = None

def set_live_callback(fn):
    global _live_callback
    _live_callback = fn

def _emit_live(msg: str):
    if _live_callback:
        try:
            _live_callback(msg)
        except Exception:
            pass

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
        # Intentar reparar JSON malformado (frecuente en contenido CSS/HTML con caracteres especiales)
        try:
            from json_repair import repair_json
            repaired = repair_json(tc.function.arguments)
            args = json.loads(repaired)
            log.warning("JSON reparado en tool %s (original inválido: %s)", name, e)
        except Exception:
            log.error("JSON inválido en tool %s: %s", name, e)
            return name, {}, f"Error: argumentos JSON inválidos en `{name}`"

    # Normalizar alias comunes de argumentos
    _ARG_ALIASES = {"relative_path": "path", "file_path": "path", "filename": "path"}
    tool_def = next((t for t in TOOLS if t["function"]["name"] == name), None)
    if tool_def:
        required = tool_def["function"].get("parameters", {}).get("required", [])
        for req in required:
            if req not in args:
                # buscar alias
                for alias, canonical in _ARG_ALIASES.items():
                    if req == alias and canonical in args:
                        args[req] = args[canonical]
                        break
                    if req == canonical and alias in args:
                        args[req] = args[alias]
                        break
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
        _emit_live(f"🔧 {name}: {str(result)[:150]}")
        # Notificar si la policy bloqueó la tool
        if isinstance(result, str) and result.startswith("Tool no permitida por policy"):
            try:
                from src.notifier import notify_error
                notify_error(result, context=name)
            except Exception:
                pass
        return name, args, result
    except Exception as e:
        log.error("Error en tool %s: %s", name, e)
        try:
            from src.notifier import notify_error
            notify_error(str(e), context=name)
        except Exception:
            pass
        return name, args, f"Error ejecutando `{name}`: {e}"
