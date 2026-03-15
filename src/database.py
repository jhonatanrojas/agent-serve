import sqlite3
import os

DB_PATH = os.getenv("SQLITE_DB_PATH", "/root/agent-serve/.agent.db")


def _conn():
    return sqlite3.connect(DB_PATH)


def sql_query(query: str) -> str:
    try:
        with _conn() as conn:
            cur = conn.execute(query)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                result = " | ".join(cols) + "\n"
                result += "\n".join(" | ".join(str(v) for v in row) for row in rows)
                return result or "Sin resultados"
            conn.commit()
            return f"OK — filas afectadas: {cur.rowcount}"
    except Exception as e:
        return f"SQL error: {e}"


def list_tables() -> str:
    return sql_query("SELECT name FROM sqlite_master WHERE type='table'")
