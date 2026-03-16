import os
import logging
from src.llm_runner import run_llm
from src.loop_guard import LoopGuard
from src.task_context import TaskContext
from src.executor import execute_tool_call, _safe_parse_args, is_cancelled, MAX_ITERATIONS
from src.tools import TOOLS

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
MAX_NO_CODE_CHANGE_RETRIES = int(os.getenv("CODER_MAX_NO_CODE_RETRIES", "2"))
log = logging.getLogger("coder")

# Tools permitidas para el coder — solo lectura/escritura de código y git
_CODER_ALLOWED_TOOLS = {
    "read_file", "write_file", "create_spec",
    "git_pull", "git_push",
    # Serena tools para edición semántica
    "create_text_file", "replace_content",
    "find_file", "list_dir", "find_symbol",
    "insert_after_symbol", "replace_symbol_body",
    # Codex CLI para implementación compleja
    "codex_exec",
    "subtask_done",
    "read_task_context",
}

_CODER_TOOLS = [t for t in TOOLS if t["function"]["name"] in _CODER_ALLOWED_TOOLS]

_CODER_PROMPT = """Eres un agente coder especializado. Tu única responsabilidad es implementar la siguiente subtarea.

Resumen del Contexto:
{context_summary}

Subtarea a implementar:
{subtask}

Reglas estrictas:
- Implementa SOLO lo que dice la subtarea, nada más.
- Para ver detalles técnicos completos (análisis, arquitectura o especificación técnica), DEBES usar la herramienta `read_task_context(section=...)`. No asumas detalles no presentes en el resumen.
- NO entregues solo análisis o explicación: debes aplicar cambios reales en archivos cuando la subtarea sea de implementación.
- Si la subtarea es solo "analizar", "investigar" o "documentar" sin pedir cambios de código, responde con "NECESITA_IMPLEMENTACION_EXPLICITA" y no finalices como completada.
- Para cambiar código usa herramientas de edición (write_file/replace_content/replace_symbol_body/etc.).
- Cuando hayas terminado la implementación, llama OBLIGATORIAMENTE a `subtask_done(status='completed', reason='...')`.
- Si determinas que la subtarea no requiere cambios de código tras analizar los archivos, llama a `subtask_done(status='no_changes_needed', reason='...')`.
- Responde en español."""


def _has_code_changes(repo_path: str | None) -> bool:
    if not repo_path:
        return bool(False)
    try:
        import subprocess
        changed_files = [
            p.strip() for p in subprocess.check_output(["git", "diff", "--name-only", "HEAD"], cwd=repo_path, text=True).splitlines()
            if p.strip()
        ]
    except Exception:
        return False

    code_exts = (".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".json", ".yml", ".yaml", ".toml", ".sh")
    return any((f.startswith("src/") or f == "main.py" or f.endswith(code_exts)) and not f.endswith(".md") for f in changed_files)


def run_coder(subtask: str, context: str = "", progress_callback=None,
              mode: str = "auto", manual_model_key: str | None = None,
              repo_path: str | None = None,
              max_llm_calls: int | None = None,
              max_tool_calls: int | None = None) -> dict:
    """
    Ejecuta una subtarea de codificación con scope acotado.
    Retorna {"result": str, "modified_files": list, "status": str}
    """
    log.info("Coder iniciando subtarea: %s", subtask[:80])
    ctx = TaskContext(message=subtask)
    guard = LoopGuard()
    no_code_change_retries = 0
    llm_calls = 0
    tool_calls = 0

    # RAG Dinámico: No inyectar todo el contexto, solo un resumen.
    # El resto se servirá bajo demanda vía `read_task_context`.
    context_summary = context[:1000] + "\n...(Resumen. Usa read_task_context para ver detalles completos)..." if len(context) > 1000 else context
    
    system = _CODER_PROMPT.format(context_summary=context_summary or "Sin contexto adicional.", subtask=subtask)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": subtask},
    ]

    for iteration in range(MAX_ITERATIONS):
        ctx.iterations = iteration + 1

        if is_cancelled():
            ctx.finish("cancelled")
            return {"result": "⛔ Cancelado.", "modified_files": ctx.modified_files, "status": "cancelled", "llm_calls": llm_calls, "tool_calls": tool_calls}

        # Heartbeat: actualizar updated_at del run activo para que el watchdog sepa que sigue vivo
        try:
            from src.run_state import _conn, _now
            with _conn() as conn:
                conn.execute("UPDATE run_states SET updated_at=? WHERE phase NOT IN ('done','failed') ORDER BY datetime(updated_at) DESC LIMIT 1", (_now(),))
                conn.commit()
        except Exception:
            pass

        if max_llm_calls is not None and llm_calls >= max_llm_calls:
            ctx.finish("error", f"Presupuesto LLM agotado ({llm_calls}/{max_llm_calls})")
            return {"result": "Presupuesto LLM agotado para esta corrida.", "modified_files": ctx.modified_files, "status": "error", "llm_calls": llm_calls, "tool_calls": tool_calls}

        try:
            llm_result = run_llm(
                messages=messages,
                agent_role="coder",
                tools=_CODER_TOOLS,
                tool_choice="auto",
                mode=mode,
                manual_model_key=manual_model_key,
                repo_path=repo_path,
            )
            msg = llm_result.message
            llm_calls += 1
            if progress_callback and iteration == 0:
                progress_callback(f"🤖 [coder/{llm_result.model_used}] ejecutando...")

            # Extraer y mostrar la reflexión del modelo si existe
            content_str = getattr(msg, "content", "") or ""
            reasoning = getattr(msg, "reasoning_content", None)
            
            import re
            if not reasoning and "<think>" in content_str:
                match = re.search(r"<think>(.*?)</think>", content_str, re.DOTALL)
                if match:
                    reasoning = match.group(1).strip()
            
            if reasoning and progress_callback:
                display_reasoning = reasoning[:800] + "\n...(truncado)" if len(reasoning) > 800 else reasoning
                progress_callback(f"🧠 [Reflexión {llm_result.model_used}]:\n{display_reasoning}")

        except Exception as e:
            log.error("Error LLM coder: %s", e)
            ctx.finish("error", str(e))
            return {"result": f"Error: {e}", "modified_files": ctx.modified_files, "status": "error", "llm_calls": llm_calls, "tool_calls": tool_calls}

        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            content = (msg.content or "").strip()
            if "NECESITA_IMPLEMENTACION_EXPLICITA" in content:
                ctx.finish("error", "Subtarea sin instrucción de implementación")
                return {
                    "result": "Subtarea solo de análisis/documentación sin instrucción de implementar código.",
                    "modified_files": ctx.modified_files,
                    "status": "error",
                    "llm_calls": llm_calls,
                    "tool_calls": tool_calls,
                }
            if not _has_code_changes(repo_path):
                no_code_change_retries += 1
                retry_msg = "No detecté cambios de código en git diff --stat. Debes modificar archivos de código, no solo analizar."
                messages.append({"role": "user", "content": retry_msg})
                if progress_callback:
                    progress_callback(f"⚠️ Coder sin cambios de código detectados (intento {no_code_change_retries}/{MAX_NO_CODE_CHANGE_RETRIES}); reintentando con instrucción explícita.")
                if no_code_change_retries >= MAX_NO_CODE_CHANGE_RETRIES:
                    ctx.finish("error", "Sin cambios de código tras reintentos controlados")
                    return {
                        "result": "No se detectaron cambios de código tras reintentos controlados. Se aborta para evitar loops/costo innecesario.",
                        "modified_files": ctx.modified_files,
                        "status": "error",
                        "llm_calls": llm_calls,
                        "tool_calls": tool_calls,
                    }
                continue

            ctx.finish("completed")
            log.info("Coder completó subtarea en %d iteraciones", iteration + 1)
            return {
                "result": msg.content,
                "modified_files": ctx.modified_files,
                "status": "completed",
                "llm_calls": llm_calls,
                "tool_calls": tool_calls,
            }

        for tc in msg.tool_calls:
            if max_tool_calls is not None and tool_calls >= max_tool_calls:
                ctx.finish("error", f"Presupuesto tools agotado ({tool_calls}/{max_tool_calls})")
                return {"result": "Presupuesto de tool-calls agotado para esta corrida.", "modified_files": ctx.modified_files, "status": "error", "llm_calls": llm_calls, "tool_calls": tool_calls}
            if is_cancelled():
                ctx.finish("cancelled")
                return {"result": "⛔ Cancelado.", "modified_files": ctx.modified_files, "status": "cancelled", "llm_calls": llm_calls, "tool_calls": tool_calls}

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
                return {"result": loop_err, "modified_files": ctx.modified_files, "status": "loop_detected", "llm_calls": llm_calls, "tool_calls": tool_calls}

            if progress_callback:
                progress_callback(f"🔧 Coder: `{name}`")

            name, args, result = execute_tool_call(tc)
            tool_calls += 1
            ctx.record_tool(name, args, result, iteration + 1)

            if "SUBTASK_DONE_SIGNAL" in str(result):
                ctx.finish("completed")
                log.info("Coder finalizó subtarea vía subtask_done: %s", result)
                return {
                    "result": str(result),
                    "modified_files": ctx.modified_files,
                    "status": "completed",
                    "llm_calls": llm_calls,
                    "tool_calls": tool_calls,
                }

            loop_err = guard.record_result(name, result)
            if loop_err:
                ctx.finish("loop_detected", loop_err)
                return {"result": loop_err, "modified_files": ctx.modified_files, "status": "loop_detected", "llm_calls": llm_calls, "tool_calls": tool_calls}

            if "REQUEST_CONTEXT_SECTION" in str(result):
                section = args.get("section", "spec")
                # El supervisor/coder inyecta el contenido real de la memoria aquí
                if section == "analysis":
                    ctx_content = context # En este MVP, 'context' contiene el análisis/spec completo pasado por el supervisor
                else:
                    ctx_content = context
                result = f"CONTENIDO DE SECCIÓN '{section}':\n\n{ctx_content}"

            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    ctx.finish("limit_reached")
    return {
        "result": "⚠️ Límite de iteraciones alcanzado.",
        "modified_files": ctx.modified_files,
        "status": "limit_reached",
        "llm_calls": llm_calls,
        "tool_calls": tool_calls,
    }
