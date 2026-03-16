"""
Clasifica la intención de un mensaje libre del usuario.
Retorna un dict con intent, repo (opcional) y tasks (lista).
"""
from __future__ import annotations
import json
import logging
from src.llm_runner import run_llm

log = logging.getLogger("intent_classifier")

_SYSTEM = """Eres un clasificador de intenciones para un agente de desarrollo de software.
Analiza el mensaje del usuario y responde SOLO con un JSON válido, sin texto adicional.

Intenciones posibles:
- "setup_repo": el usuario quiere configurar/cambiar el repositorio activo
- "add_tasks": el usuario quiere agregar tareas al backlog (sin cambiar repo)
- "setup_and_task": quiere configurar repo Y agregar tareas en un solo mensaje
- "confirm": el usuario confirma una acción pendiente ("sí", "dale", "ok", "ejecuta", "adelante")
- "cancel": el usuario cancela ("no", "cancela", "espera", "para")
- "do_next": el usuario quiere ejecutar la siguiente tarea pendiente del backlog ("continúa", "siguiente tarea", "ejecuta pendientes", "sigue", "continúa con la tarea", "do next")
- "query": pregunta sobre estado, tareas, logs, etc.
- "other": conversación general o no relacionada

Formato de respuesta:
{
  "intent": "<intent>",
  "repo": "<nombre o URL del repo, o null>",
  "branch": "<branch si se menciona, o null>",
  "tasks": ["<tarea 1>", "<tarea 2>"]
}

Ejemplos:
- "trabaja en agent-serve y agrega logging" → {"intent":"setup_and_task","repo":"agent-serve","branch":null,"tasks":["agregar logging estructurado"]}
- "agrega autenticación JWT al endpoint /login" → {"intent":"add_tasks","repo":null,"branch":null,"tasks":["agregar autenticación JWT al endpoint /login"]}
- "sí, ejecuta" → {"intent":"confirm","repo":null,"branch":null,"tasks":[]}
- "continúa con la tarea pendiente" → {"intent":"do_next","repo":null,"branch":null,"tasks":[]}
- "sigue con lo que falta" → {"intent":"do_next","repo":null,"branch":null,"tasks":[]}
- "¿cuál es el estado?" → {"intent":"query","repo":null,"branch":null,"tasks":[]}"""


def classify_intent(message: str) -> dict:
    """
    Clasifica el mensaje del usuario.
    Retorna dict con keys: intent, repo, branch, tasks.
    """
    try:
        result = run_llm(
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": message},
            ],
            task_type="general",
            agent_role="analyst",
        )
        raw = result.message.content.strip()
        # Extraer JSON si viene envuelto en ```
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        return json.loads(raw)
    except Exception as e:
        log.warning(f"[intent_classifier] error: {e}")
        return {"intent": "other", "repo": None, "branch": None, "tasks": []}
