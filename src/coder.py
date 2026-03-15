import os
import logging
import litellm
from src.loop_guard import LoopGuard
from src.task_context import TaskContext
from src.executor import execute_tool_call, _safe_parse_args, is_cancelled, MAX_ITERATIONS
from src.tools import TOOLS

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
log = logging.getLogger("coder")

# Tools permitidas para el coder — solo lectura/escritura de código y git
_CODER_ALLOWED_TOOLS = {
    "read_file", "write_file", "create_spec",
    "git_pull", "git_push",
    # Serena tools para edición semántica
    "read_file", "create_text_file", "replace_content",
    "find_file", "list_dir", "find_symbol",
    "insert_after_symbol", "replace_symbol_body",
}

_CODER_TOOLS = [t for t in TOOLS if t["function"]["name"] in _CODER_ALLOWED_TOOLS]

_CODER_PROMPT = """Eres un agente coder especializado. Tu única responsabilidad es implementar la siguiente subtarea.

Contexto del proyecto:
{context}

Subtarea a implementar:
{subtask}

Reglas estrictas:
- Implementa SOLO lo que dice la subtarea, nada más.
- No refactorices código fuera del alcance.
- Reporta cada archivo que modifiques.
- Si algo es ambiguo, elige la opción más conservadora.
- Responde en español."""


def run_coder(subtask: str, context: str = "", progress_callback=None) -> dict:
    """
    Ejecuta una subtarea de codificación con scope acotado.
    Retorna {"result": str, "modified_files": list, "status": str}
    """
    log.info("Coder iniciando subtarea: %s", subtask[:80])
    ctx = TaskContext(message=subtask)
    guard = LoopGuard()

    system = _CODER_PROMPT.format(context=context or "Sin contexto adicional.", subtask=subtask)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": subtask},
    ]

    for iteration in range(MAX_ITERATIONS):
        ctx.iterations = iteration + 1

        if is_cancelled():
            ctx.finish("cancelled")
            return {"result": "⛔ Cancelado.", "modified_files": ctx.modified_files, "status": "cancelled"}

        try:
            response = litellm.completion(
                model=MODEL,
                messages=messages,
                tools=_CODER_TOOLS,
                tool_choice="auto",
            )
        except Exception as e:
            log.error("Error LLM coder: %s", e)
            ctx.finish("error", str(e))
            return {"result": f"Error: {e}", "modified_files": ctx.modified_files, "status": "error"}

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            ctx.finish("completed")
            log.info("Coder completó subtarea en %d iteraciones", iteration + 1)
            return {
                "result": msg.content,
                "modified_files": ctx.modified_files,
                "status": "completed",
            }

        for tc in msg.tool_calls:
            if is_cancelled():
                ctx.finish("cancelled")
                return {"result": "⛔ Cancelado.", "modified_files": ctx.modified_files, "status": "cancelled"}

            name = tc.function.name

            # Bloquear tools fuera del scope del coder
            if name not in _CODER_ALLOWED_TOOLS:
                log.warning("Coder intentó usar tool no permitida: %s", name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": f"Tool `{name}` no está permitida en el coder. Usa solo: {sorted(_CODER_ALLOWED_TOOLS)}",
                })
                continue

            loop_err = guard.record_call(name, _safe_parse_args(tc))
            if loop_err:
                ctx.finish("loop_detected", loop_err)
                return {"result": loop_err, "modified_files": ctx.modified_files, "status": "loop_detected"}

            if progress_callback:
                progress_callback(f"🔧 Coder: `{name}`")

            name, args, result = execute_tool_call(tc)
            ctx.record_tool(name, args, result, iteration + 1)

            loop_err = guard.record_result(name, result)
            if loop_err:
                ctx.finish("loop_detected", loop_err)
                return {"result": loop_err, "modified_files": ctx.modified_files, "status": "loop_detected"}

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    ctx.finish("limit_reached")
    return {
        "result": "⚠️ Límite de iteraciones alcanzado.",
        "modified_files": ctx.modified_files,
        "status": "limit_reached",
    }
