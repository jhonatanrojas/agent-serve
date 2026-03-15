import os
import json
import litellm
from src.tools import TOOLS, TOOL_MAP

MODEL = os.getenv("LLM_MODEL", "deepseek/deepseek-chat")

SYSTEM_PROMPT = """Eres un agente de desarrollo autónomo. Puedes:
- Hacer git pull y git push
- Crear specs de cambios
- Leer y escribir archivos
- Reportar avances

Responde siempre en español. Sé conciso y reporta cada acción que realizas."""


def run_agent(user_message: str, progress_callback=None) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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

        # Sin tool calls → respuesta final
        if not msg.tool_calls:
            return msg.content

        # Ejecutar tools
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
