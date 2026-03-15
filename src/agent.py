import os
import logging
import litellm
from src.tools import TOOLS
from src.memory import search_memory
from src.loop_guard import LoopGuard
from src.task_context import TaskContext
from src.analyst import analyze_codebase  # noqa: F401 - usado via supervisor
from src.executor import (
    execute_tool_call, _safe_parse_args,
    cancel, reset, is_cancelled, MAX_ITERATIONS  # noqa: F401
)

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")

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


def load_memory(query: str) -> str:
    return search_memory(query)


def build_system_prompt(memories: str) -> str:
    if memories and "Sin memorias" not in memories:
        return _SYSTEM_BASE + f"\n\nMemorias relevantes:\n{memories}"
    return _SYSTEM_BASE


def call_model(messages: list) -> object:
    response = litellm.completion(
        model=MODEL,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
    )
    return response.choices[0].message


def run_agent_loop(messages: list, ctx: TaskContext, guard: LoopGuard,
                   progress_callback=None) -> str:
    for iteration in range(MAX_ITERATIONS):
        ctx.iterations = iteration + 1

        if is_cancelled():
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
            ctx.finish("completed")
            return msg.content

        for tc in msg.tool_calls:
            if is_cancelled():
                ctx.finish("cancelled")
                return "⛔ Tarea cancelada por el usuario.\n\n" + ctx.summary()

            name = tc.function.name
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

            loop_err = guard.record_result(name, result)
            if loop_err:
                ctx.finish("loop_detected", loop_err)
                if progress_callback:
                    progress_callback(loop_err)
                return loop_err + "\n\n" + ctx.summary()

            if progress_callback:
                progress_callback(f"✅ `{name}`: {result[:200]}")

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    ctx.finish("limit_reached")
    return f"⚠️ Límite de {MAX_ITERATIONS} iteraciones alcanzado.\n\n" + ctx.summary()


def run_agent(user_message: str, progress_callback=None) -> str:
    from src.supervisor import run_supervisor

    reset()
    log.info("Iniciando agente: %s", user_message[:100])

    # Intentar flujo complejo via supervisor
    result = run_supervisor(user_message, progress_callback)

    # Si el supervisor indica que es tarea simple, usar loop directo
    if result != "__SIMPLE__":
        return result

    # Tarea simple: loop directo
    ctx = TaskContext(message=user_message)
    guard = LoopGuard()
    memories = load_memory(user_message)
    system = build_system_prompt(memories)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]
    return run_agent_loop(messages, ctx, guard, progress_callback)
