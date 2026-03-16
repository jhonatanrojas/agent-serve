import os
import json
import logging
from src.llm_runner import run_llm
from src.workspace_context import get_active_repo_path

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")

log = logging.getLogger("planner")

# Palabras clave que indican alta complejidad
_COMPLEX_SIGNALS = [
    "refactor", "arquitectura", "migrar", "migración", "autenticación",
    "seguridad", "api", "base de datos", "schema", "módulo", "sistema",
    "integrar", "integración", "subagente", "orquestador", "pipeline",
    "varios archivos", "múltiples", "rediseñar", "reestructurar",
]

_CLASSIFY_PROMPT = """Analiza la siguiente instrucción y determina si es SIMPLE o COMPLEJA.

SIMPLE: tarea de un solo paso, un archivo, o una acción directa (git pull, leer archivo, buscar algo).
COMPLEJA: afecta múltiples archivos, cambia arquitectura, introduce nueva feature, requiere subtareas encadenadas.

Responde SOLO con un JSON: {{"complexity": "simple"}} o {{"complexity": "complex", "reason": "..."}}

Instrucción: {message}"""

_SPEC_PROMPT = """Eres un arquitecto de software. Genera una spec técnica para la siguiente tarea.

Responde SOLO con un JSON con esta estructura:
{{
  "title": "...",
  "objective": "...",
  "scope": ["..."],
  "out_of_scope": ["..."],
  "impacted_files": ["..."],
  "subtasks": ["..."],
  "acceptance_criteria": ["..."],
  "risks": ["..."]
}}

Tarea: {message}"""




def normalize_spec(spec: dict) -> dict:
    """Normaliza spec para soportar subtareas jerárquicas y mantener compatibilidad."""
    if not isinstance(spec, dict):
        return {"title": "spec", "objective": "", "subtasks": []}

    subtasks = spec.get("subtasks", [])
    flat: list[str] = []
    hierarchical: list[dict] = []

    if isinstance(subtasks, list):
        for item in subtasks:
            if isinstance(item, str):
                flat.append(item)
            elif isinstance(item, dict):
                phase = str(item.get("phase") or item.get("name") or "phase").strip()
                phase_tasks = item.get("tasks") or item.get("subtasks") or []
                phase_flat = [str(t).strip() for t in phase_tasks if str(t).strip()]
                if phase_flat:
                    hierarchical.append({"phase": phase, "tasks": phase_flat})
                    for t in phase_flat:
                        flat.append(f"[{phase}] {t}")

    if hierarchical:
        spec["subtasks_hierarchical"] = hierarchical
    spec["subtasks"] = flat
    return spec


def classify_task(message: str, mode: str = "auto", manual_model_key: str | None = None) -> tuple[dict, str]:
    """Retorna ({"complexity": "simple"|"complex", ...}, model_used)"""
    msg_lower = message.lower()
    if any(signal in msg_lower for signal in _COMPLEX_SIGNALS):
        log.info("Tarea clasificada como compleja por palabras clave")
        return {"complexity": "complex", "reason": "contiene señales de alta complejidad"}, "keyword"

    try:
        result = run_llm(
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(message=message)}],
            agent_role="planner",
            require_tools=False,
            mode=mode,
            manual_model_key=manual_model_key,
        )
        content = result.message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        return normalize_spec(json.loads(content[start:end])), result.model_used
    except Exception as e:
        log.warning("Error clasificando tarea: %s — asumiendo simple", e)
        return {"complexity": "simple"}, "?"


def generate_spec(message: str, mode: str = "auto", manual_model_key: str | None = None) -> tuple[dict, str]:
    """Llama al LLM para generar una spec estructurada. Retorna (spec, model_used)."""
    try:
        result = run_llm(
            messages=[{"role": "user", "content": _SPEC_PROMPT.format(message=message)}],
            agent_role="planner",
            require_tools=False,
            mode=mode,
            manual_model_key=manual_model_key,
        )
        content = result.message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        return normalize_spec(json.loads(content[start:end])), result.model_used
    except Exception as e:
        log.error("Error generando spec: %s", e)
        return normalize_spec({"title": "spec", "objective": message, "subtasks": [], "error": str(e)}), "?"


def save_spec(spec: dict) -> str:
    """Guarda la spec como archivo .md en specs/ y retorna el path."""
    specs_dir = get_active_repo_path() / "specs"
    specs_dir.mkdir(exist_ok=True)
    title = spec.get("title", "spec").lower().replace(" ", "-")
    path = specs_dir / f"{title}.md"

    lines = [f"# {spec.get('title', 'Spec')}\n"]
    for key, label in [
        ("objective", "## Objetivo"),
        ("scope", "## Alcance"),
        ("out_of_scope", "## Fuera de alcance"),
        ("impacted_files", "## Archivos impactados"),
        ("subtasks", "## Subtareas"),
        ("acceptance_criteria", "## Criterios de aceptación"),
        ("risks", "## Riesgos"),
    ]:
        val = spec.get(key)
        if val:
            lines.append(label)
            if isinstance(val, list):
                lines.extend(f"- {item}" for item in val)
            else:
                lines.append(str(val))
            lines.append("")

    path.write_text("\n".join(lines))
    log.info("Spec guardada: %s", path)
    return str(path)


def plan_task(message: str, mode: str = "auto", manual_model_key: str | None = None) -> tuple[bool, str, str]:
    """
    Evalúa la tarea. Si es compleja, genera y guarda spec.
    Retorna (is_complex, spec_summary_or_empty, model_used).
    """
    classification, model_used = classify_task(message, mode=mode, manual_model_key=manual_model_key)
    if classification.get("complexity") != "complex":
        return False, "", model_used

    log.info("Tarea compleja detectada. Generando spec...")
    spec, model_used = generate_spec(message, mode=mode, manual_model_key=manual_model_key)
    path = save_spec(spec)

    summary = (
        f"📐 **Spec generada**: `{path}`\n"
        f"**Objetivo**: {spec.get('objective', '')}\n"
        f"**Subtareas**:\n" +
        "\n".join(f"  - {s}" for s in spec.get("subtasks", []))
    )
    return True, summary, model_used


def enrich_task_plan(spec: dict) -> dict:
    subtasks = spec.get("subtasks", []) or []
    grouped: dict[str, list[str]] = {}
    dependencies: list[dict] = []
    risk = "low"

    high_risk_signals = ("migr", "auth", "security", "schema", "refactor", "payment", "prod")

    for idx, st in enumerate(subtasks, 1):
        text = str(st)
        phase = "implementation"
        if text.startswith("[") and "]" in text:
            phase = text[1:text.index("]")].strip().lower() or "implementation"
            text = text[text.index("]") + 1 :].strip()
        grouped.setdefault(phase, []).append(text)

        low = text.lower()
        if any(sig in low for sig in high_risk_signals):
            risk = "high"
        elif risk != "high" and any(k in low for k in ("api", "db", "cache", "queue")):
            risk = "medium"

        if "depends on" in low or "dep:" in low:
            dependencies.append({"subtask_index": idx, "raw": st})

    ordered_phases = sorted(grouped.keys(), key=lambda x: ["analysis", "design", "implementation", "tests", "review"].index(x) if x in ["analysis", "design", "implementation", "tests", "review"] else 99)

    return {
        "grouped_subtasks": grouped,
        "ordered_phases": ordered_phases,
        "dependencies": dependencies,
        "risk": risk,
        "total_subtasks": len(subtasks),
    }
