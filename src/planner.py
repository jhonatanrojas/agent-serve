import os
import json
import logging
from pathlib import Path
import litellm

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
SPECS_DIR = Path(os.getenv("REPO_PATH", "/root/agent-serve")) / "specs"

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

Responde SOLO con un JSON: {"complexity": "simple"} o {"complexity": "complex", "reason": "..."}

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


def classify_task(message: str) -> dict:
    """Retorna {"complexity": "simple"} o {"complexity": "complex", "reason": "..."}"""
    # Clasificación rápida por palabras clave antes de llamar al LLM
    msg_lower = message.lower()
    if any(signal in msg_lower for signal in _COMPLEX_SIGNALS):
        log.info("Tarea clasificada como compleja por palabras clave")
        return {"complexity": "complex", "reason": "contiene señales de alta complejidad"}

    try:
        response = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": _CLASSIFY_PROMPT.format(message=message)}],
            max_tokens=100,
        )
        content = response.choices[0].message.content.strip()
        # Limpiar posibles bloques de código markdown
        content = content.replace("```json", "").replace("```", "").strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        return json.loads(content[start:end])
    except Exception as e:
        log.warning("Error clasificando tarea: %s — asumiendo simple", e)
        return {"complexity": "simple"}


def generate_spec(message: str) -> dict:
    """Llama al LLM para generar una spec estructurada."""
    try:
        response = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": _SPEC_PROMPT.format(message=message)}],
            max_tokens=1000,
        )
        content = response.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        start = content.find("{")
        end = content.rfind("}") + 1
        return json.loads(content[start:end])
    except Exception as e:
        log.error("Error generando spec: %s", e)
        return {"title": "spec", "objective": message, "subtasks": [], "error": str(e)}


def save_spec(spec: dict) -> str:
    """Guarda la spec como archivo .md en specs/ y retorna el path."""
    SPECS_DIR.mkdir(exist_ok=True)
    title = spec.get("title", "spec").lower().replace(" ", "-")
    path = SPECS_DIR / f"{title}.md"

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


def plan_task(message: str) -> tuple[bool, str]:
    """
    Evalúa la tarea. Si es compleja, genera y guarda spec.
    Retorna (is_complex, spec_summary_or_empty).
    """
    classification = classify_task(message)
    if classification.get("complexity") != "complex":
        return False, ""

    log.info("Tarea compleja detectada. Generando spec...")
    spec = generate_spec(message)
    path = save_spec(spec)

    summary = (
        f"📐 **Spec generada**: `{path}`\n"
        f"**Objetivo**: {spec.get('objective', '')}\n"
        f"**Subtareas**:\n" +
        "\n".join(f"  - {s}" for s in spec.get("subtasks", []))
    )
    return True, summary
