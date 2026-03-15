import os
import json
import logging
import threading
import litellm
from src.tools import TOOLS, TOOL_MAP
from src.memory import search_memory
from src.loop_guard import LoopGuard
from src.task_context import TaskContext
from src.planner import plan_task
from src.analyst import analyze_codebase

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("agent")

_SYSTEM_BASE = """Eres un agente de desarrollo autónomo. Puedes:
- Hacer git pull y git push
- Crear specs de cambios
- Leer y escribir archivos
- Buscar en internet con DuckDuckGo
- Guardar y recuperar memorias persistentes
- Ejecutar queries SQL en la base de datos local
- Programar tareas recurrentes con cron
- Interactuar con Notion y analizar código con Serena

Responde siempre en español. Sé conciso y reporta cada acción que realizas."""

_cancel_event = threading.Event()


def cancel():
    _cancel_event.set()


def reset():
    _cancel_event.clear()


def is_cancelled():
    return _cancel_event.is_set()


# ── Responsabilidades separadas ──────────────────────────────────────────────

def load_memory(query: str) -> str:
    """Recupera memorias relevantes para el mensaje del usuario."""
    return search_memory(query)


def build_system_prompt(memories: str) -> str:
    """Construye el system prompt inyectando memorias si las hay."""
    if memories and "Sin memorias" not in memories:
        return _SYSTEM_BASE + f"\n\nMemorias relevantes:\n{memories}"
    return _SYSTEM_BASE


def call_model(messages: list) -> object:
    """Llama al LLM y retorna el mensaje de respuesta. Lanza excepción si falla."""
    response = litellm.completion(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    return response.choices[0].message


def execute_tool_call(tc) -> tuple[str, dict, str]:
    """
    Ejecuta una tool call. Retorna (name, args, result).
    Nunca lanza excepción — los errores se retornan como string.
    """
    name = tc.function.name
    try:
        args = json.loads(tc.function.arguments)
    except json.JSONDecodeError as e:
        log.error("JSON inválido en tool %s: %s", name, e)
        return name, {}, f"Error: argumentos JSON inválidos en `{name}`"

    # Validar argumentos requeridos
    tool_def = next((t for t in TOOLS if t["function"]["name"] == name), None)
    if tool_def:
        required = tool_def["function"].get("parameters", {}).get("required", [])
        missing = [r for r in required if r not in args]
        if missing:
            log.warning("Args faltantes en %s: %s", name, missing)
            return name, args, f"Argumentos faltantes en `{name}`: {missing}"

    try:
        log.info("Ejecutando tool: %s args=%s", name, args)
        result = TOOL_MAP[name](args)
        log.info("Tool %s OK: %s", name, str(result)[:120])
        return name, args, result
    except Exception as e:
        log.error("Error en tool %s: %s", name, e)
        return name, args, f"Error ejecutando `{name}`: {e}"


def run_agent_loop(messages: list, ctx: TaskContext, guard: LoopGuard,
                   progress_callback=None) -> str:
    """Loop principal del agente. Retorna la respuesta final."""
    for iteration in range(MAX_ITERATIONS):
        ctx.iterations = iteration + 1

        if is_cancelled():
            log.info("Cancelado en iteración %d", iteration)
            ctx.finish("cancelled")
            return "⛔ Tarea cancelada por el usuario.\n\n" + ctx.summary()

        log.info("Iteración %d/%d", iteration + 1, MAX_ITERATIONS)

        try:
            msg = call_model(messages)
        except Exception as e:
            log.error("Error LLM: %s", e)
            ctx.finish("error", str(e))
            return f"Error al llamar al modelo: {e}"

        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            log.info("Completado en iteración %d", iteration + 1)
            ctx.finish("completed")
            return msg.content

        for tc in msg.tool_calls:
            if is_cancelled():
                ctx.finish("cancelled")
                return "⛔ Tarea cancelada por el usuario.\n\n" + ctx.summary()

            name = tc.function.name

            # Guardrail pre-ejecución
            loop_err = guard.record_call(name, _safe_parse_args(tc))
            if loop_err:
                ctx.finish("loop_detected", loop_err)
                if progress_callback:
                    progress_callback(loop_err)
                return loop_err + "\n\n" + ctx.summary()

            if progress_callback:
                progress_callback(f"⚙️ Ejecutando: `{name}`")

            name, args, result = execute_tool_call(tc)
            ctx.record_tool(name, args, result, iteration + 1)

            # Guardrail post-ejecución
            loop_err = guard.record_result(name, result)
            if loop_err:
                ctx.finish("loop_detected", loop_err)
                if progress_callback:
                    progress_callback(loop_err)
                return loop_err + "\n\n" + ctx.summary()

            if progress_callback:
                progress_callback(f"✅ `{name}`: {result[:200]}")

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    log.warning("Límite de iteraciones alcanzado (%d)", MAX_ITERATIONS)
    ctx.finish("limit_reached")
    return f"⚠️ Límite de {MAX_ITERATIONS} iteraciones alcanzado.\n\n" + ctx.summary()


def _safe_parse_args(tc) -> dict:
    try:
        return json.loads(tc.function.arguments)
    except Exception:
        return {}


# ── Punto de entrada público ─────────────────────────────────────────────────

def run_agent(user_message: str, progress_callback=None) -> str:
    reset()
    ctx = TaskContext(message=user_message)
    guard = LoopGuard()
    log.info("Iniciando agente: %s", user_message[:100])

    # Planner: evaluar complejidad y generar spec si aplica
    is_complex, spec_summary = plan_task(user_message)
    if is_complex and spec_summary:
        log.info("Tarea compleja — spec generada")
        if progress_callback:
            progress_callback(spec_summary)

    # Analyst: analizar codebase si la tarea es compleja
    analysis = ""
    if is_complex:
        analysis = analyze_codebase(user_message)
        log.info("Análisis completado")
        if progress_callback:
            progress_callback(analysis)

    memories = load_memory(user_message)
    system = build_system_prompt(memories)

    # Inyectar spec y análisis en el contexto si existen
    if is_complex:
        if spec_summary:
            system += f"\n\nSpec generada:\n{spec_summary}\nSigue las subtareas en orden."
        if analysis:
            system += f"\n\nAnálisis del codebase:\n{analysis}"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]

    return run_agent_loop(messages, ctx, guard, progress_callback)
