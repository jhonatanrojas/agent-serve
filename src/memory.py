import os
import json
import sqlite3
from pathlib import Path

# Memoria simple basada en SQLite — sin dependencias de embeddings
DB_PATH = "/root/agent-serve/.agent.db"
AGENT_ID = "agent-serve"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        text TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    return conn


def add_memory(text: str, user_id: str = "default") -> str:
    try:
        with _conn() as conn:
            conn.execute("INSERT INTO memories (user_id, text) VALUES (?, ?)", (user_id, text))
        return f"Memoria guardada: {text[:80]}"
    except Exception as e:
        return f"Error guardando memoria: {e}"


def search_memory(query: str, user_id: str = "default") -> str:
    try:
        words = query.lower().split()[:5]
        with _conn() as conn:
            rows = conn.execute(
                "SELECT text FROM memories WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                (user_id,)
            ).fetchall()
        if not rows:
            return "Sin memorias relevantes"
        # Filtro simple por palabras clave
        relevant = [r[0] for r in rows if any(w in r[0].lower() for w in words)]
        results = relevant[:5] if relevant else [r[0] for r in rows[:3]]
        return "\n".join(f"- {r}" for r in results)
    except Exception:
        return "Sin memorias relevantes"


def get_all_memories(user_id: str = "default") -> str:
    try:
        with _conn() as conn:
            rows = conn.execute(
                "SELECT text, created_at FROM memories WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
                (user_id,)
            ).fetchall()
        if not rows:
            return "Sin memorias guardadas"
        return "\n".join(f"- [{r[1][:10]}] {r[0]}" for r in rows)
    except Exception as e:
        return f"Error: {e}"
