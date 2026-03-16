import os
import logging
from pathlib import Path
from src.llm_runner import run_llm
from src.repomap import get_or_build_repo_map

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")
REPO_PATH = Path(os.getenv("REPO_PATH", "/root/agent-serve"))
log = logging.getLogger("analyst")

_IGNORE = {".git", "venv", "__pycache__", ".mem0", "node_modules", ".serena"}
_CODE_EXTS = {".py", ".js", ".ts", ".json", ".md", ".yaml", ".yml", ".toml", ".env.example"}


def scan_repo() -> dict:
    """Escanea el repo y retorna estructura de archivos con tamaño."""
    files = {}
    for path in REPO_PATH.rglob("*"):
        if any(part in _IGNORE for part in path.parts):
            continue
        if path.is_file() and path.suffix in _CODE_EXTS:
            rel = str(path.relative_to(REPO_PATH))
            try:
                size = path.stat().st_size
                files[rel] = {"size": size, "lines": len(path.read_text(errors="ignore").splitlines())}
            except Exception:
                pass
    return files


def read_file_summary(rel_path: str, max_lines: int = 30) -> str:
    """Lee las primeras N líneas de un archivo para dar contexto al LLM."""
    try:
        content = (REPO_PATH / rel_path).read_text(errors="ignore")
        lines = content.splitlines()
        preview = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            preview += f"\n... ({len(lines) - max_lines} líneas más)"
        return preview
    except Exception as e:
        return f"Error leyendo {rel_path}: {e}"


_RELEVANT_PROMPT = """Dado el siguiente listado de archivos del proyecto y una tarea, 
identifica cuáles archivos son más relevantes para implementar la tarea.

Archivos disponibles:
{file_list}

Tarea: {message}

Responde SOLO con JSON: {{"relevant_files": ["archivo1", "archivo2"], "reason": "..."}}"""

_IMPACT_PROMPT = """Analiza el impacto de la siguiente tarea en el codebase.

Archivos relevantes y su contenido:
{file_contents}

Tarea: {message}

Responde SOLO con JSON:
{{
  "impact_level": "low|medium|high",
  "affected_components": ["..."],
  "breaking_changes": ["..."],
  "recommendations": ["..."]
}}"""


def find_relevant_files(message: str, file_map: dict, repo_map: dict | None = None) -> list[str]:
    """Usa RepoMap + LLM para identificar archivos relevantes para la tarea."""
    msg_lower = message.lower()
    if repo_map:
        candidates = []
        for module in repo_map.get("modules", [])[:500]:
            mod_l = module.lower()
            if any(tok in mod_l for tok in msg_lower.split()[:8]):
                candidates.append(module)
        if candidates:
            return sorted(set(candidates))[:8]

    file_list = "\n".join(f"- {f} ({v['lines']} líneas)" for f, v in file_map.items())
    try:
        result = run_llm(
            messages=[{"role": "user", "content": _RELEVANT_PROMPT.format(
                file_list=file_list, message=message
            )}],
            agent_role="analyst",
            require_tools=False,
        )
        content = result.message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        import json
        start, end = content.find("{"), content.rfind("}") + 1
        data = json.loads(content[start:end])
        return data.get("relevant_files", [])
    except Exception as e:
        log.warning("Error identificando archivos relevantes: %s", e)
        # Fallback: retornar archivos Python del src/
        return [f for f in file_map if f.startswith("src/") and f.endswith(".py")]


def assess_impact(message: str, relevant_files: list[str]) -> dict:
    """Evalúa el impacto del cambio basado en los archivos relevantes."""
    file_contents = ""
    for rel in relevant_files[:5]:  # máximo 5 archivos para no saturar el contexto
        preview = read_file_summary(rel, max_lines=20)
        file_contents += f"\n### {rel}\n{preview}\n"

    try:
        import json
        result = run_llm(
            messages=[{"role": "user", "content": _IMPACT_PROMPT.format(
                file_contents=file_contents, message=message
            )}],
            agent_role="analyst",
            require_tools=False,
        )
        content = result.message.content.strip()
        content = content.replace("```json", "").replace("```", "").strip()
        start, end = content.find("{"), content.rfind("}") + 1
        return json.loads(content[start:end])
    except Exception as e:
        log.warning("Error evaluando impacto: %s", e)
        return {"impact_level": "unknown", "affected_components": relevant_files}


def analyze_codebase(message: str) -> str:
    """
    Punto de entrada principal. Usa RepoMap, identifica archivos relevantes
    y evalúa impacto. Retorna un resumen legible. NO modifica archivos.
    """
    log.info("Analizando codebase para: %s", message[:80])
    repo_map = get_or_build_repo_map(REPO_PATH)
    file_map = scan_repo()
    relevant = find_relevant_files(message, file_map, repo_map=repo_map)
    impact = assess_impact(message, relevant)

    level = impact.get("impact_level", "unknown")
    emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(level, "⚪")

    lines = [
        "🔍 **Análisis de codebase**",
        f"• RepoMap módulos: {len(repo_map.get('modules', []))}",
        f"• Archivos escaneados: {len(file_map)}",
        f"• Archivos relevantes: {', '.join(relevant) or 'ninguno'}",
        f"• Impacto: {emoji} {level}",
    ]
    if impact.get("affected_components"):
        lines.append(f"• Componentes afectados: {', '.join(impact['affected_components'])}")
    if impact.get("breaking_changes"):
        lines.append("• ⚠️ Breaking changes: " + "; ".join(impact["breaking_changes"]))
    if impact.get("recommendations"):
        lines.append("• Recomendaciones: " + "; ".join(impact["recommendations"]))

    return "\n".join(lines)
