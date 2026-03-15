import os
import json
import litellm
from src.tools import TOOLS, TOOL_MAP
from src.memory import search_memory

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")

SYSTEM_PROMPT = """Eres un agente de desarrollo autónomo. Puedes:
- Hacer git pull y git push
- Crear specs de cambios
- Leer y escribir archivos
- Buscar en internet con DuckDuckGo
- Guardar y recuperar memorias persistentes
- Ejecutar queries SQL en la base de datos local
- Programar tareas recurrentes con cron
- Interactuar con Notion y analizar código con Serena

Responde siempre en español. Sé conciso y reporta cada acción que realizas.

{memories}"""


def run_agent(user_message: str, progress_callback=None) -> str:
    # Recuperar memorias relevantes
    memories = search_memory(user_message)
    system = SYSTEM_PROMPT.format(
        memories=f"\nMemorias relevantes:\n{memories}" if "Sin memorias" not in memories else ""
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]

    while True:
        response = litellm.completion(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))

        if not msg.tool_calls:
            return msg.content

        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)

            if progress_callback:
                progress_callback(f"⚙️ Ejecutando: `{name}`")

            result = TOOL_MAP[name](args)

            if progress_callback:
                progress_callback(f"✅ `{name}`: {result}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })
