"""
Camada SQLite para o WhatsApp Monitor Bot.

Tabelas:
  - admins:    chat_ids do Telegram autorizados a controlar o bot
  - instances: cada instância da Evolution monitorada (com seu próprio alvo)
  - stats:     estatísticas agregadas por instância
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "bot.db")
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def cursor():
    with _lock:
        conn = _connect()
        try:
            yield conn.cursor()
            conn.commit()
        finally:
            conn.close()


def init_db(seed_admins: list[int] | None = None):
    with cursor() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                chat_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS instances (
                name             TEXT PRIMARY KEY,
                target_group_jid TEXT DEFAULT '',
                target_name      TEXT DEFAULT '',
                emoji            TEXT DEFAULT '🔥',
                ativo            INTEGER DEFAULT 0,
                check_delay      REAL DEFAULT 1.5,
                max_retries      INTEGER DEFAULT 10,
                criado_em        TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                instance_name   TEXT PRIMARY KEY,
                sucesso         INTEGER DEFAULT 0,
                falha           INTEGER DEFAULT 0,
                tentativas      INTEGER DEFAULT 0,
                ultima_captura  TEXT,
                FOREIGN KEY (instance_name) REFERENCES instances(name) ON DELETE CASCADE
            )
        """)
        if seed_admins:
            for chat_id in seed_admins:
                c.execute("INSERT OR IGNORE INTO admins (chat_id) VALUES (?)", (chat_id,))


# ── Admins ──────────────────────────────────────────────────

def is_admin(chat_id: int) -> bool:
    with cursor() as c:
        c.execute("SELECT 1 FROM admins WHERE chat_id = ?", (chat_id,))
        return c.fetchone() is not None


def listar_admins() -> list[int]:
    with cursor() as c:
        c.execute("SELECT chat_id FROM admins ORDER BY chat_id")
        return [row["chat_id"] for row in c.fetchall()]


def adicionar_admin(chat_id: int):
    with cursor() as c:
        c.execute("INSERT OR IGNORE INTO admins (chat_id) VALUES (?)", (chat_id,))


def remover_admin(chat_id: int):
    with cursor() as c:
        c.execute("DELETE FROM admins WHERE chat_id = ?", (chat_id,))


# ── Instâncias ──────────────────────────────────────────────

def criar_instancia(name: str):
    with cursor() as c:
        c.execute("INSERT OR IGNORE INTO instances (name) VALUES (?)", (name,))
        c.execute("INSERT OR IGNORE INTO stats (instance_name) VALUES (?)", (name,))


def remover_instancia(name: str):
    with cursor() as c:
        c.execute("DELETE FROM instances WHERE name = ?", (name,))


def get_instancia(name: str) -> Optional[dict]:
    with cursor() as c:
        c.execute("SELECT * FROM instances WHERE name = ?", (name,))
        row = c.fetchone()
        return dict(row) if row else None


def listar_instancias() -> list[dict]:
    with cursor() as c:
        c.execute("SELECT * FROM instances ORDER BY name")
        return [dict(r) for r in c.fetchall()]


def atualizar_instancia(name: str, **kwargs):
    if not kwargs:
        return
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    valores = list(kwargs.values()) + [name]
    with cursor() as c:
        c.execute(f"UPDATE instances SET {campos} WHERE name = ?", valores)


def set_ativo(name: str, ativo: bool):
    atualizar_instancia(name, ativo=1 if ativo else 0)


# ── Stats ───────────────────────────────────────────────────

def get_stats(name: str) -> dict:
    with cursor() as c:
        c.execute("SELECT * FROM stats WHERE instance_name = ?", (name,))
        row = c.fetchone()
        return dict(row) if row else {
            "instance_name": name, "sucesso": 0, "falha": 0,
            "tentativas": 0, "ultima_captura": None
        }


def incrementar_sucesso(name: str, quando_iso: str):
    with cursor() as c:
        c.execute("""
            UPDATE stats
               SET sucesso = sucesso + 1,
                   ultima_captura = ?
             WHERE instance_name = ?
        """, (quando_iso, name))


def incrementar_falha(name: str):
    with cursor() as c:
        c.execute("UPDATE stats SET falha = falha + 1 WHERE instance_name = ?", (name,))


def incrementar_tentativa(name: str):
    with cursor() as c:
        c.execute("UPDATE stats SET tentativas = tentativas + 1 WHERE instance_name = ?", (name,))
