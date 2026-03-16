"""
Preferencias de modelo LLM por chat_id.
Persiste en la misma SQLite del proyecto (.agent.db).
"""
from __future__ import annotations
import sqlite3
import os
from typing import Optional

DB_PATH = os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_llm_preferences (
            chat_id     INTEGER PRIMARY KEY,
            mode        TEXT NOT NULL DEFAULT 'auto',
            model_key   TEXT,
            max_llm_calls INTEGER,
            max_tool_calls INTEGER
        )
    """)
    try:
        conn.execute("ALTER TABLE chat_llm_preferences ADD COLUMN max_llm_calls INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE chat_llm_preferences ADD COLUMN max_tool_calls INTEGER")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def get_preference(chat_id: int) -> dict:
    """Devuelve {'mode': 'auto'|'manual', 'model_key': str|None}."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT mode, model_key, max_llm_calls, max_tool_calls FROM chat_llm_preferences WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
    if row:
        return {"mode": row[0], "model_key": row[1], "max_llm_calls": row[2], "max_tool_calls": row[3]}
    return {"mode": "auto", "model_key": None, "max_llm_calls": None, "max_tool_calls": None}


def set_auto(chat_id: int) -> None:
    """Vuelve al modo automático."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO chat_llm_preferences (chat_id, mode, model_key)
            VALUES (?, 'auto', NULL)
            ON CONFLICT(chat_id) DO UPDATE SET mode='auto', model_key=NULL
        """, (chat_id,))
        conn.commit()


def set_manual(chat_id: int, model_key: str) -> None:
    """Fija un modelo manual para el chat."""
    with _conn() as conn:
        conn.execute("""
            INSERT INTO chat_llm_preferences (chat_id, mode, model_key)
            VALUES (?, 'manual', ?)
            ON CONFLICT(chat_id) DO UPDATE SET mode='manual', model_key=?
        """, (chat_id, model_key, model_key))
        conn.commit()


def set_budget(chat_id: int, max_llm_calls: int | None, max_tool_calls: int | None) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO chat_llm_preferences (chat_id, mode, model_key, max_llm_calls, max_tool_calls)
            VALUES (?, 'auto', NULL, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET max_llm_calls=excluded.max_llm_calls, max_tool_calls=excluded.max_tool_calls
            """,
            (chat_id, max_llm_calls, max_tool_calls),
        )
        conn.commit()
