import os
import json
import logging
from pathlib import Path

import git
from src.llm_runner import run_llm
from src.workspace_context import get_active_repo_path

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
log = logging.getLogger("reviewer")

_REVIEW_PROMPT = """Eres un revisor de código. Analiza si los cambios realizados cumplen los criterios de aceptación.

Spec de la tarea:
{spec_summary}

Diff de cambios (resumen + patch parcial):
{diff_content}

Archivos modificados y su contenido actual:
{file_contents}

Criterios de aceptación:
{criteria}

Responde SOLO con JSON:
{{
  "approved": true/false,
  "verdict": "APROBADO|RECHAZADO|PARCIAL",
  "issues": ["problema1", "problema2"],
  "required_fixes": ["fix1", "fix2"],
  "suggestions": ["sugerencia1"],
  "criteria_met": ["criterio cumplido"],
  "criteria_missing": ["criterio no cumplido"]
}}"""


def _read_modified_files(modified_files: list[str]) -> str:
    contents = ""
    for rel_path in modified_files[:5]:
        path = get_active_repo_path() / rel_path if not rel_path.startswith("/") else Path(rel_path)
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


def _read_diff(modified_files: list[str]) -> str:
    try:
        repo = git.Repo(str(get_active_repo_path()))
    except Exception as e:
        return f"No se pudo abrir repo git: {e}"

    sections: list[str] = []
    for rel in modified_files[:10]:
        try:
            patch = repo.git.diff("--", rel)
            if not patch.strip():
                continue
            lines = patch.splitlines()
            clipped = "\n".join(lines[:120])
            if len(lines) > 120:
                clipped += f"\n... ({len(lines)-120} líneas más)"
            sections.append(f"### {rel}\n{clipped}")
        except Exception as e:
            sections.append(f"### {rel}\nError obteniendo diff: {e}")

    if not sections:
        try:
            stat = repo.git.diff("--stat")
            return stat or "Sin diff disponible"
        except Exception as e:
            return f"Sin diff disponible: {e}"

    return "\n\n".join(sections)


def _normalize_review_result(result: dict) -> dict:
    required_fixes = result.get("required_fixes")
    if not isinstance(required_fixes, list):
        required_fixes = []

    for issue in result.get("issues", []):
        if issue not in required_fixes:
            required_fixes.append(issue)

    for missing in result.get("criteria_missing", []):
        fix = f"Cumplir criterio pendiente: {missing}"
        if fix not in required_fixes:
            required_fixes.append(fix)

    result["required_fixes"] = required_fixes
    return result


def run_reviewer(spec_summary: str, modified_files: list[str],
                 criteria: list[str] = None,
                 mode: str = "auto", manual_model_key: str | None = None) -> dict:
    """
    Verifica si los cambios cumplen la spec, considerando contenido final + diff.
    Retorna JSON estructurado con lista de required_fixes.
    """
    log.info("Reviewer iniciando. Archivos: %s", modified_files)

    if not modified_files:
        return {
            "approved": False,
            "verdict": "RECHAZADO",
            "issues": ["No se modificó ningún archivo"],
            "required_fixes": ["Aplicar los cambios solicitados en al menos un archivo relevante"],
            "suggestions": ["Verifica que el coder ejecutó las subtareas correctamente"],
            "criteria_met": [],
            "criteria_missing": criteria or [],
        }

    file_contents = _read_modified_files(modified_files)
    diff_content = _read_diff(modified_files)
    criteria_str = "\n".join(f"- {c}" for c in (criteria or ["Sin criterios definidos"]))

    try:
        llm_result = run_llm(
            messages=[{"role": "user", "content": _REVIEW_PROMPT.format(
                spec_summary=spec_summary,
                diff_content=diff_content,
                file_contents=file_contents,
                criteria=criteria_str,
            )}],
            agent_role="reviewer",
            require_tools=False,
            mode=mode,
            manual_model_key=manual_model_key,
        )
        content = llm_result.message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        start, end = content.find("{"), content.rfind("}") + 1
        result = json.loads(content[start:end])
        result = _normalize_review_result(result)
        log.info("Review completado: %s", result.get("verdict"))
        return result
    except Exception as e:
        log.error("Error en reviewer: %s", e)
        return {
            "approved": False,
            "verdict": "ERROR",
            "issues": [f"Error al revisar: {e}"],
            "required_fixes": ["Reintentar revisión automática o realizar revisión manual"],
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

    if review.get("required_fixes"):
        lines.append("**Fixes requeridos:**")
        lines.extend(f"  🔧 {f}" for f in review["required_fixes"])

    if review.get("suggestions"):
        lines.append("**Sugerencias:**")
        lines.extend(f"  → {s}" for s in review["suggestions"])

    return "\n".join(lines)
