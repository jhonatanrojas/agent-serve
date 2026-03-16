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
            model_key   TEXT
        )
    """)
    conn.commit()
    return conn


def get_preference(chat_id: int) -> dict:
    """Devuelve {'mode': 'auto'|'manual', 'model_key': str|None}."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT mode, model_key FROM chat_llm_preferences WHERE chat_id=?",
            (chat_id,)
        ).fetchone()
    if row:
        return {"mode": row[0], "model_key": row[1]}
    return {"mode": "auto", "model_key": None}


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
