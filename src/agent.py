import os
import json
import logging
import threading
import litellm
from src.tools import TOOLS, TOOL_MAP
from src.memory import search_memory
from src.loop_guard import LoopGuard
from src.task_context import TaskContext

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("agent")

SYSTEM_PROMPT = """Eres un agente de desarrollo autónomo. Puedes:
- Hacer git pull y git push
- Crear specs de cambios
- Leer y escribir archivos
- Buscar en internet con DuckDuckGo
- Guardar y recuperar memorias persistentes
- Ejecutar queries SQL en la base de datos local
- Programar tareas recurrentes con cron
- Interactuar con Notion y analizar código con Serena

Responde siempre en español. Sé conciso y reporta cada acción que realizas.

{memories}"""

_cancel_event = threading.Event()


def cancel():
    _cancel_event.set()


def reset():
    _cancel_event.clear()


def is_cancelled():
    return _cancel_event.is_set()


def _validate_args(tool_name: str, args: dict) -> str | None:
    """Retorna mensaje de error si los args son inválidos, None si son válidos."""
    tool_def = next((t for t in TOOLS if t["function"]["name"] == tool_name), None)
    if not tool_def:
        return f"Tool desconocida: {tool_name}"
    required = tool_def["function"].get("parameters", {}).get("required", [])
    missing = [r for r in required if r not in args]
    if missing:
        return f"Argumentos faltantes en `{tool_name}`: {missing}"
    return None


def _execute_tool(tc) -> str:
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments)
    except json.JSONDecodeError as e:
        log.error("JSON inválido en tool %s: %s", name, e)
        return f"Error: argumentos JSON inválidos en `{name}`"

    validation_error = _validate_args(name, args)
    if validation_error:
        log.warning("Validación fallida: %s", validation_error)
        return validation_error

    try:
        log.info("Ejecutando tool: %s args=%s", name, args)
        result = TOOL_MAP[name](args)
        log.info("Tool %s completada: %s", name, str(result)[:120])
        return result
    except Exception as e:
        log.error("Error en tool %s: %s", name, e)
        return f"Error ejecutando `{name}`: {e}"


def run_agent(user_message: str, progress_callback=None) -> str:
    reset()
    guard = LoopGuard()
    ctx = TaskContext(message=user_message)
    log.info("Iniciando agente. Mensaje: %s", user_message[:100])

    memories = search_memory(user_message)
    system = SYSTEM_PROMPT.format(
        memories=f"\nMemorias relevantes:\n{memories}" if "Sin memorias" not in memories else ""
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]

    for iteration in range(MAX_ITERATIONS):
        ctx.iterations = iteration + 1

        if is_cancelled():
            log.info("Agente cancelado en iteración %d", iteration)
            ctx.finish("cancelled")
            return "⛔ Tarea cancelada por el usuario.\n\n" + ctx.summary()

        log.info("Iteración %d/%d", iteration + 1, MAX_ITERATIONS)

        try:
            response = litellm.completion(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
        except Exception as e:
            log.error("Error llamando al LLM: %s", e)
            ctx.finish("error", str(e))
            return f"Error al llamar al modelo: {e}"

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            log.info("Agente completó en iteración %d", iteration + 1)
            ctx.finish("completed")
            return msg.content

        for tc in msg.tool_calls:
            if is_cancelled():
                log.info("Agente cancelado durante tool calls")
                ctx.finish("cancelled")
                return "⛔ Tarea cancelada por el usuario.\n\n" + ctx.summary()

            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments)
            except json.JSONDecodeError as e:
                log.error("JSON inválido en tool %s: %s", name, e)
                args = {}

            loop_error = guard.record_call(name, args)
            if loop_error:
                log.warning("Loop detectado en tool %s", name)
                ctx.finish("loop_detected", loop_error)
                if progress_callback:
                    progress_callback(loop_error)
                return loop_error + "\n\n" + ctx.summary()

            if progress_callback:
                progress_callback(f"⚙️ Ejecutando: `{name}`")

            result = _execute_tool(tc)
            ctx.record_tool(name, args, result, iteration + 1)

            loop_error = guard.record_result(name, result)
            if loop_error:
                log.warning("Resultado repetido en tool %s", name)
                ctx.finish("loop_detected", loop_error)
                if progress_callback:
                    progress_callback(loop_error)
                return loop_error + "\n\n" + ctx.summary()

            if progress_callback:
                progress_callback(f"✅ `{name}`: {result[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    log.warning("Límite de iteraciones alcanzado (%d)", MAX_ITERATIONS)
    ctx.finish("limit_reached")
    return f"⚠️ Se alcanzó el límite de {MAX_ITERATIONS} iteraciones.\n\n" + ctx.summary()
