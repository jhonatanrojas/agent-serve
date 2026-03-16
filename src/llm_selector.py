"""
Selector de modelos LLM.
Resuelve la lista ordenada de candidatos según:
- task_type / agent_role
- require_tools
- modo: auto | manual
- modelo manual fijado (si aplica)
"""
from __future__ import annotations
from src.llm_registry import ModelEntry, list_models, get_model

# Mapeo rol → use_cases preferidos (orden importa)
ROLE_USE_CASES: dict[str, list[str]] = {
    "planner":  ["planner"],
    "analyst":  ["analyst"],
    "coder":    ["coder"],
    "reviewer": ["reviewer"],
    "tests":    ["tests", "coder"],
    "general":  ["general"],
}


def select_candidates(
    task_type: str = "general",
    agent_role: str | None = None,
    require_tools: bool = True,
    mode: str = "auto",
    manual_model_key: str | None = None,
) -> list[ModelEntry]:
    """
    Devuelve lista ordenada de modelos candidatos.

    Modo manual: devuelve [modelo_fijado] si está disponible, sino fallback a auto.
    Modo auto: filtra por use_case, tools y disponibilidad; ordena por prioridad.
    """
    if mode == "manual" and manual_model_key:
        entry = get_model(manual_model_key)
        if entry and entry.is_available:
            return [entry]
        # modelo manual no disponible → fallback a auto silencioso con aviso en caller

    role = agent_role or task_type
    use_cases = ROLE_USE_CASES.get(role, ["general"])

    candidates = [
        m for m in list_models(only_available=True)
        if any(uc in m.use_cases for uc in use_cases)
        and (not require_tools or m.supports_tools)
    ]

    if not candidates:
        # fallback: cualquier modelo disponible con tools si se requiere
        candidates = [
            m for m in list_models(only_available=True)
            if not require_tools or m.supports_tools
        ]

    return candidates  # ya ordenados por priority desde list_models
