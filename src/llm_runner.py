"""
Runner LLM con fallback ordenado y observabilidad.
Ejecuta llamadas a LiteLLM probando candidatos en orden hasta que uno responda.
"""
from __future__ import annotations
import logging
import litellm
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from src.llm_registry import ModelEntry
from src.llm_selector import select_candidates

log = logging.getLogger("llm_runner")

# ---------------------------------------------------------------------------
# Métricas en memoria (se resetean al reiniciar el servicio)
# ---------------------------------------------------------------------------
_stats: dict[str, dict] = defaultdict(lambda: {
    "calls": 0, "success": 0, "failures": 0, "fallbacks_triggered": 0
})


def get_stats() -> dict[str, dict]:
    return dict(_stats)


def stats_text() -> str:
    if not _stats:
        return "📊 Sin métricas aún."
    lines = ["📊 *LLM Stats (sesión actual):*\n"]
    for key, s in sorted(_stats.items()):
        lines.append(
            f"• `{key}`: ✅{s['success']} ❌{s['failures']} "
            f"🔁fallbacks={s['fallbacks_triggered']} calls={s['calls']}"
        )
    return "\n".join(lines)

# Errores que justifican fallback al siguiente modelo
_FALLBACK_EXCEPTIONS = (
    litellm.AuthenticationError,
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.APIConnectionError,
    litellm.APIError,
)


def _classify_error(e: Exception) -> str:
    if isinstance(e, litellm.AuthenticationError):
        return "auth_error"
    if isinstance(e, litellm.RateLimitError):
        return "rate_limit"
    if isinstance(e, litellm.ServiceUnavailableError):
        return "unavailable"
    if isinstance(e, litellm.APIConnectionError):
        return "connection_error"
    if isinstance(e, litellm.APIError):
        return "api_error"
    return "unknown_error"


@dataclass
class LLMResult:
    message: Any                          # choices[0].message
    model_used: str                       # key interna del modelo exitoso
    model_str: str                        # string LiteLLM usado
    mode: str                             # auto | manual
    attempts: list[dict] = field(default_factory=list)  # trazabilidad de intentos

    @property
    def fallback_count(self) -> int:
        return len(self.attempts) - 1


@dataclass
class LLMError(Exception):
    message: str
    attempts: list[dict] = field(default_factory=list)

    def __str__(self):
        return self.message


def run_llm(
    messages: list,
    task_type: str = "general",
    agent_role: str | None = None,
    require_tools: bool = False,
    tools: Optional[list] = None,
    tool_choice: str = "auto",
    mode: str = "auto",
    manual_model_key: str | None = None,
    repo_path: str | None = None,
) -> LLMResult:
    """
    Ejecuta la llamada LLM con fallback automático.
    Devuelve LLMResult con el mensaje y metadata de ejecución.
    Lanza LLMError si todos los candidatos fallan.
    """
    candidates = select_candidates(
        task_type=task_type,
        agent_role=agent_role,
        require_tools=require_tools or bool(tools),
        mode=mode,
        manual_model_key=manual_model_key,
    )

    if not candidates:
        raise LLMError("No hay modelos disponibles para esta tarea.", attempts=[])

    # --- Codex CLI runner (coder/tests con sesión activa, o cuando manual_model_key=codex_mini) ---
    _CODEX_ROLES = ("tests",)
    use_codex_cli = ((agent_role in _CODEX_ROLES) or (manual_model_key == "codex_mini" and agent_role != "coder")) and repo_path
    if use_codex_cli:
        from src.codex_runner import is_codex_session_active, run_codex_task
        if is_codex_session_active():
            prompt = next(
                (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                None,
            )
            if prompt:
                _stats["codex_cli"]["calls"] += 1
                try:
                    log.info(f"[llm_runner] codex_cli role={agent_role} repo={repo_path}")
                    output = run_codex_task(prompt, repo_path)
                    _stats["codex_cli"]["success"] += 1

                    # Construir un message-like compatible con el resto del sistema
                    class _Msg:
                        def __init__(self, content):
                            self.content = content
                            self.tool_calls = None
                            self.role = "assistant"

                    return LLMResult(
                        message=_Msg(output),
                        model_used="codex_cli",
                        model_str="codex/codex-mini-latest",
                        mode=mode,
                        attempts=[{"model_key": "codex_cli", "status": "ok"}],
                    )
                except Exception as e:
                    _stats["codex_cli"]["failures"] += 1
                    log.warning(f"[llm_runner] codex_cli falló, fallback a LiteLLM: {e}")
                    # Continúa con el loop normal de candidatos

    attempts: list[dict] = []

    for entry in candidates:
        # codex_mini no tiene API key → nunca intentar via LiteLLM
        if entry.key == "codex_mini" and not os.getenv("OPENAI_API_KEY"):
            continue
        attempt: dict = {"model_key": entry.key, "model_str": entry.model}
        _stats[entry.key]["calls"] += 1
        try:
            kwargs: dict = {"model": entry.model, "messages": messages}
            if tools and entry.supports_tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = tool_choice

            log.info(f"[llm_runner] model={entry.key} role={agent_role or task_type} mode={mode}")
            response = litellm.completion(**kwargs)
            message = response.choices[0].message

            attempt["status"] = "ok"
            attempts.append(attempt)
            _stats[entry.key]["success"] += 1

            if len(attempts) > 1:
                _stats[entry.key]["fallbacks_triggered"] += 1
                log.info(f"[llm_runner] Fallback exitoso model={entry.key} tras {len(attempts)-1} fallo(s)")

            log.info(
                f"[llm_runner] OK model={entry.key} fallbacks={len(attempts)-1} mode={mode}"
            )
            return LLMResult(
                message=message,
                model_used=entry.key,
                model_str=entry.model,
                mode=mode,
                attempts=attempts,
            )

        except _FALLBACK_EXCEPTIONS as e:
            error_type = _classify_error(e)
            attempt["status"] = "failed"
            attempt["error_type"] = error_type
            attempt["error"] = str(e)[:200]
            attempts.append(attempt)
            _stats[entry.key]["failures"] += 1
            log.warning(f"[llm_runner] FAIL model={entry.key} error={error_type}: {str(e)[:100]}")

        except Exception as e:
            attempt["status"] = "failed"
            attempt["error_type"] = "unexpected"
            attempt["error"] = str(e)[:200]
            attempts.append(attempt)
            _stats[entry.key]["failures"] += 1
            log.error(f"[llm_runner] ERROR model={entry.key}: {e}")

    raise LLMError(
        f"Todos los modelos fallaron ({len(attempts)} intento(s)).",
        attempts=attempts,
    )
