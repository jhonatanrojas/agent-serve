"""
Notificador central — envía mensajes a Telegram desde cualquier módulo.
Soporta modo live por chat_id (persistido en SQLite).
"""
from __future__ import annotations
import logging
import re
import threading
import sqlite3
import os

log = logging.getLogger("notifier")

_send_callback = None
_lock = threading.Lock()

LIVE_MAX_CHARS = 300

def _db_path() -> str:
    return os.getenv("SQLITE_DB_PATH", os.getenv("RUNSTATE_DB_PATH", ".agent.db"))

def _ensure_table():
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS live_chats (chat_id INTEGER PRIMARY KEY)")

def enable_live(chat_id):
    _ensure_table()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("INSERT OR IGNORE INTO live_chats VALUES (?)", (int(chat_id),))

def disable_live(chat_id):
    _ensure_table()
    with sqlite3.connect(_db_path()) as conn:
        conn.execute("DELETE FROM live_chats WHERE chat_id=?", (int(chat_id),))

def is_live(chat_id) -> bool:
    try:
        _ensure_table()
        with sqlite3.connect(_db_path()) as conn:
            row = conn.execute("SELECT 1 FROM live_chats WHERE chat_id=?", (int(chat_id),)).fetchone()
            return row is not None
    except Exception:
        return False


def set_send_callback(fn):
    global _send_callback
    _send_callback = fn


def get_send_callback():
    return _send_callback


def _to_natural(msg: str) -> str:
    """Convierte logs técnicos a lenguaje natural resumido."""
    msg = msg.strip()
    # Quitar prefijos de log
    msg = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ \[.*?\] ", "", msg)
    # Truncar
    if len(msg) > LIVE_MAX_CHARS:
        msg = msg[:LIVE_MAX_CHARS] + "…"
    return msg


def notify(msg: str, *, error: bool = False, live_chat_id=None):
    """Envía mensaje a Telegram. Si live_chat_id está activo, también envía en modo live."""
    if _send_callback:
        try:
            _send_callback(msg)
        except Exception as e:
            log.warning(f"[notifier] send error: {e}")


def notify_error(msg: str, context: str = ""):
    """Notifica un error operacional a Telegram."""
    text = f"⚠️ **Error operacional**"
    if context:
        text += f" en `{context}`"
    text += f"\n`{msg[:200]}`"
    log.error(f"[notifier] {context}: {msg}")
    notify(text)


def live_update(chat_id, msg: str):
    """Envía actualización live si el chat tiene live activado."""
    if not is_live(chat_id):
        return
    natural = _to_natural(msg)
    if not natural:
        return
    if _send_callback:
        try:
            _send_callback(f"🔴 `{natural}`")
        except Exception:
            pass
