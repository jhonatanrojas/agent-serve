import os
import json
import logging
import litellm
from pathlib import Path

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve"))
log = logging.getLogger("reviewer")

_REVIEW_PROMPT = """Eres un revisor de código. Analiza si los cambios realizados cumplen los criterios de aceptación.

Spec de la tarea:
{spec_summary}

Archivos modificados y su contenido actual:
{file_contents}

Criterios de aceptación:
{criteria}

Responde SOLO con JSON:
{{
  "approved": true/false,
  "verdict": "APROBADO|RECHAZADO|PARCIAL",
  "issues": ["problema1", "problema2"],
  "suggestions": ["sugerencia1"],
  "criteria_met": ["criterio cumplido"],
  "criteria_missing": ["criterio no cumplido"]
}}"""


def _read_modified_files(modified_files: list[str]) -> str:
    contents = ""
    for rel_path in modified_files[:5]:  # máximo 5 para no saturar contexto
        path = REPO_PATH / rel_path if not rel_path.startswith("/") else Path(rel_path)
        try:
            text = path.read_text(errors="ignore")
            lines = text.splitlines()[:40]
            preview = "\n".join(lines)
            if len(text.splitlines()) > 40:
                preview += f"\n... ({len(text.splitlines()) - 40} líneas más)"
            contents += f"\n### {rel_path}\n```\n{preview}\n```\n"
        except Exception as e:
            contents += f"\n### {rel_path}\nError leyendo: {e}\n"
    return contents or "Sin archivos modificados para revisar."


def run_reviewer(spec_summary: str, modified_files: list[str],
                 criteria: list[str] = None) -> dict:
    """
    Verifica si los cambios cumplen la spec.
    Retorna {"approved": bool, "verdict": str, "issues": list, "suggestions": list}
    """
    log.info("Reviewer iniciando. Archivos: %s", modified_files)

    if not modified_files:
        return {
            "approved": False,
            "verdict": "RECHAZADO",
            "issues": ["No se modificó ningún archivo"],
            "suggestions": ["Verifica que el coder ejecutó las subtareas correctamente"],
            "criteria_met": [],
            "criteria_missing": criteria or [],
        }

    file_contents = _read_modified_files(modified_files)
    criteria_str = "\n".join(f"- {c}" for c in (criteria or ["Sin criterios definidos"]))

    try:
        response = litellm.completion(
            model=MODEL,
            messages=[{"role": "user", "content": _REVIEW_PROMPT.format(
                spec_summary=spec_summary,
                file_contents=file_contents,
                criteria=criteria_str,
            )}],
            max_tokens=600,
        )
        content = response.choices[0].message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        start, end = content.find("{"), content.rfind("}") + 1
        result = json.loads(content[start:end])
        log.info("Review completado: %s", result.get("verdict"))
        return result
    except Exception as e:
        log.error("Error en reviewer: %s", e)
        return {
            "approved": False,
            "verdict": "ERROR",
            "issues": [f"Error al revisar: {e}"],
            "suggestions": [],
            "criteria_met": [],
            "criteria_missing": [],
        }


def format_review(review: dict) -> str:
    """Formatea el resultado del review para enviar por Telegram."""
    verdict = review.get("verdict", "DESCONOCIDO")
    emoji = {"APROBADO": "✅", "RECHAZADO": "❌", "PARCIAL": "⚠️", "ERROR": "🔴"}.get(verdict, "❓")

    lines = [f"{emoji} **Review: {verdict}**"]

    if review.get("criteria_met"):
        lines.append("**Criterios cumplidos:**")
        lines.extend(f"  ✓ {c}" for c in review["criteria_met"])

    if review.get("criteria_missing"):
        lines.append("**Criterios pendientes:**")
        lines.extend(f"  ✗ {c}" for c in review["criteria_missing"])

    if review.get("issues"):
        lines.append("**Problemas:**")
        lines.extend(f"  • {i}" for i in review["issues"])

    if review.get("suggestions"):
        lines.append("**Sugerencias:**")
        lines.extend(f"  → {s}" for s in review["suggestions"])

    return "\n".join(lines)
