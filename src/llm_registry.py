"""
Registro central de modelos LLM disponibles.
Cada entrada define capacidades, prioridad y casos de uso.
Para agregar un modelo: añadir entrada en MODELS_REGISTRY.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModelEntry:
    key: str                        # identificador interno
    model: str                      # string para LiteLLM (provider/model)
    priority: int                   # menor = más prioritario
    enabled: bool = True
    supports_tools: bool = True
    supports_reasoning: bool = False
    supports_long_context: bool = False
    supports_stream: bool = True
    use_cases: list[str] = field(default_factory=list)
    notes: Optional[str] = None

    @property
    def api_key_env(self) -> Optional[str]:
        """Devuelve la variable de entorno de API key según el provider."""
        provider = self.model.split("/")[0].lower()
        mapping = {
            "deepseek": "DEEPSEEK_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gemini": "GEMINI_API_KEY",
            "mistral": "MISTRAL_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
        }
        return mapping.get(provider)

    @property
    def is_available(self) -> bool:
        """True si está habilitado y tiene API key configurada."""
        if not self.enabled:
            return False
        env = self.api_key_env
        if env and not os.getenv(env):
            # codex_mini acepta sesión de Codex CLI como alternativa
            if self.key == "codex_mini":
                import os as _os
                return _os.path.exists(_os.path.expanduser("~/.codex/auth.json"))
            return False
        return True


# ---------------------------------------------------------------------------
# Registro central — editar aquí para agregar/quitar modelos
# ---------------------------------------------------------------------------
MODELS_REGISTRY: dict[str, ModelEntry] = {
    "deepseek_main": ModelEntry(
        key="deepseek_main",
        model=os.getenv("LLM_MODEL", "deepseek/deepseek-chat"),
        priority=1,
        supports_tools=True,
        supports_reasoning=False,
        supports_long_context=True,
        use_cases=["general", "coder", "analyst", "planner", "reviewer"],
    ),
    "deepseek_reasoner": ModelEntry(
        key="deepseek_reasoner",
        model="deepseek/deepseek-reasoner",
        priority=2,
        supports_tools=False,   # reasoner no soporta tool_choice
        supports_reasoning=True,
        supports_long_context=True,
        use_cases=["planner", "reviewer"],
        notes="Sin tool calling; usar solo para razonamiento puro",
    ),
    "gpt_main": ModelEntry(
        key="gpt_main",
        model="openai/gpt-4o",
        priority=3,
        supports_tools=True,
        supports_reasoning=False,
        supports_long_context=True,
        use_cases=["general", "coder", "reviewer", "planner"],
        enabled=bool(os.getenv("OPENAI_API_KEY")),
    ),
    "gemini_fast": ModelEntry(
        key="gemini_fast",
        model="gemini/gemini-2.0-flash",
        priority=4,
        supports_tools=True,
        supports_reasoning=False,
        supports_long_context=True,
        use_cases=["analyst", "general"],
        enabled=bool(os.getenv("GEMINI_API_KEY")),
    ),
    "mistral_code": ModelEntry(
        key="mistral_code",
        model="mistral/codestral-latest",
        priority=5,
        supports_tools=True,
        supports_reasoning=False,
        supports_long_context=False,
        use_cases=["coder", "tests"],
        enabled=bool(os.getenv("MISTRAL_API_KEY")),
    ),
    "codex_mini": ModelEntry(
        key="codex_mini",
        model="openai/codex-mini-latest",
        priority=3,
        supports_tools=True,
        supports_reasoning=False,
        supports_long_context=False,
        use_cases=["coder", "tests"],
        enabled=True,  # is_available verifica OPENAI_API_KEY o sesión Codex CLI
        notes="OpenAI Codex — optimizado para generación de código",
    ),
}


def get_model(key: str) -> Optional[ModelEntry]:
    return MODELS_REGISTRY.get(key)


def list_models(only_available: bool = False) -> list[ModelEntry]:
    models = list(MODELS_REGISTRY.values())
    if only_available:
        models = [m for m in models if m.is_available]
    return sorted(models, key=lambda m: m.priority)


# ---------------------------------------------------------------------------
# Registry dinámico — modelos agregados en runtime via Telegram
# ---------------------------------------------------------------------------
import sqlite3

_DB_PATH = os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db")


def _dyn_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_dynamic_models (
            key         TEXT PRIMARY KEY,
            model       TEXT NOT NULL,
            api_key_env TEXT,
            api_key_val TEXT,
            priority    INTEGER DEFAULT 10,
            use_cases   TEXT DEFAULT 'general',
            notes       TEXT
        )
    """)
    conn.commit()
    return conn


def load_dynamic_models():
    """Carga modelos dinámicos desde SQLite al MODELS_REGISTRY en memoria."""
    try:
        with _dyn_conn() as conn:
            rows = conn.execute(
                "SELECT key, model, api_key_env, api_key_val, priority, use_cases, notes FROM llm_dynamic_models"
            ).fetchall()
        for key, model, api_key_env, api_key_val, priority, use_cases, notes in rows:
            # Inyectar la API key en el entorno si fue guardada
            if api_key_env and api_key_val:
                os.environ.setdefault(api_key_env, api_key_val)
            MODELS_REGISTRY[key] = ModelEntry(
                key=key,
                model=model,
                priority=priority,
                supports_tools=True,
                use_cases=[u.strip() for u in (use_cases or "general").split(",")],
                notes=notes,
                enabled=True,
            )
    except Exception:
        pass


def register_dynamic_model(key: str, model: str, api_key_env: str, api_key_val: str,
                            priority: int = 10, use_cases: str = "general",
                            notes: str = "") -> ModelEntry:
    """Persiste un modelo nuevo en SQLite y lo agrega al registry en memoria."""
    if api_key_env and api_key_val:
        os.environ[api_key_env] = api_key_val
    with _dyn_conn() as conn:
        conn.execute("""
            INSERT INTO llm_dynamic_models (key, model, api_key_env, api_key_val, priority, use_cases, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                model=excluded.model, api_key_env=excluded.api_key_env,
                api_key_val=excluded.api_key_val, priority=excluded.priority,
                use_cases=excluded.use_cases, notes=excluded.notes
        """, (key, model, api_key_env, api_key_val, priority, use_cases, notes))
        conn.commit()
    entry = ModelEntry(
        key=key, model=model, priority=priority, supports_tools=True,
        use_cases=[u.strip() for u in use_cases.split(",")],
        notes=notes, enabled=True,
    )
    MODELS_REGISTRY[key] = entry
    return entry


# Cargar modelos dinámicos al importar el módulo
load_dynamic_models()


def models_status_text() -> str:
    """Texto legible para el comando /models de Telegram."""
    lines = ["📋 *Modelos disponibles:*\n"]
    for m in list_models():
        status = "✅" if m.is_available else "❌"
        tools = "🔧" if m.supports_tools else "  "
        reason = "🧠" if m.supports_reasoning else "  "
        lines.append(
            f"{status} {tools}{reason} `{m.key}` — `{m.model}` (p{m.priority})"
        )
        if m.notes:
            lines.append(f"   _{m.notes}_")
    lines.append("\n🔧=tools  🧠=reasoning  ✅=disponible  ❌=sin API key")
    return "\n".join(lines)
