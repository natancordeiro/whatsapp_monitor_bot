"""
Camada SQLite para o WhatsApp Monitor Bot.

Tabelas:
  - admins:    chat_ids do Telegram autorizados a controlar o bot
  - instances: cada instância da Evolution monitorada (com seu próprio alvo)
  - stats:     estatísticas agregadas por instância
"""

import os
import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Callable, Optional

DB_PATH = os.getenv("DB_PATH", "bot.db")
_lock = threading.Lock()

# Hook injetado pelo bot-prd.py para invalidar cache em memória ao gravar.
# Mantém o db.py desacoplado da camada de cache.
_invalidate_hook: Optional[Callable[[Optional[str]], None]] = None


def set_invalidate_hook(fn: Callable[[Optional[str]], None]):
    global _invalidate_hook
    _invalidate_hook = fn


def _invalidate(name: Optional[str] = None):
    if _invalidate_hook:
        try:
            _invalidate_hook(name)
        except Exception:
            pass


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
        # users (substitui admins): role pode ser 'admin' ou 'user'
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id  INTEGER PRIMARY KEY,
                role     TEXT NOT NULL DEFAULT 'user',
                name     TEXT,
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
                owner_chat_id    INTEGER,
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

        # ── Migração: tabela 'admins' antiga → 'users' com role=admin ──
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admins'")
        if c.fetchone():
            c.execute("""
                INSERT OR IGNORE INTO users (chat_id, role)
                SELECT chat_id, 'admin' FROM admins
            """)
            c.execute("DROP TABLE admins")

        # ── Migração: instances sem owner_chat_id ──
        c.execute("PRAGMA table_info(instances)")
        cols = [row["name"] for row in c.fetchall()]
        if "owner_chat_id" not in cols:
            c.execute("ALTER TABLE instances ADD COLUMN owner_chat_id INTEGER")

        # ── Admins vêm SÓ do .env (fonte de verdade) ──
        # 1. Cria/promove a admin todo chat_id listado
        if seed_admins:
            for chat_id in seed_admins:
                c.execute("""
                    INSERT INTO users (chat_id, role) VALUES (?, 'admin')
                    ON CONFLICT(chat_id) DO UPDATE SET role='admin'
                """, (chat_id,))
        # 2. Rebaixa pra 'user' qualquer admin no DB que não esteja mais no .env.
        #    SE o .env estiver vazio, NÃO rebaixa ninguém (proteção contra
        #    deploy acidental sem ADMIN_CHAT_IDS — não queremos trancar todo
        #    mundo fora do sistema).
        if seed_admins:
            placeholders = ",".join("?" * len(seed_admins))
            c.execute(
                f"UPDATE users SET role='user' WHERE role='admin' "
                f"AND chat_id NOT IN ({placeholders})",
                tuple(seed_admins),
            )

        # Adopta instâncias órfãs (sem dono) pro primeiro admin disponível
        c.execute("SELECT chat_id FROM users WHERE role='admin' ORDER BY chat_id LIMIT 1")
        row = c.fetchone()
        if row:
            c.execute("UPDATE instances SET owner_chat_id = ? WHERE owner_chat_id IS NULL",
                      (row["chat_id"],))


# ── Usuários (admin + user) ─────────────────────────────────

def get_usuario(chat_id: int) -> Optional[dict]:
    with cursor() as c:
        c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))
        row = c.fetchone()
        return dict(row) if row else None


def eh_usuario(chat_id: int) -> bool:
    return get_usuario(chat_id) is not None


def is_admin(chat_id: int) -> bool:
    u = get_usuario(chat_id)
    return bool(u and u.get("role") == "admin")


def listar_usuarios() -> list[dict]:
    with cursor() as c:
        c.execute("SELECT * FROM users ORDER BY role DESC, chat_id")
        return [dict(r) for r in c.fetchall()]


def listar_admins() -> list[int]:
    with cursor() as c:
        c.execute("SELECT chat_id FROM users WHERE role='admin' ORDER BY chat_id")
        return [row["chat_id"] for row in c.fetchall()]


def adicionar_usuario(chat_id: int, role: str = "user", name: Optional[str] = None):
    if role not in ("admin", "user"):
        raise ValueError("role deve ser 'admin' ou 'user'")
    with cursor() as c:
        c.execute("""
            INSERT INTO users (chat_id, role, name) VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                role = excluded.role,
                name = COALESCE(excluded.name, users.name)
        """, (chat_id, role, name))


def remover_usuario(chat_id: int):
    with cursor() as c:
        c.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))


def set_role(chat_id: int, role: str):
    if role not in ("admin", "user"):
        raise ValueError("role deve ser 'admin' ou 'user'")
    with cursor() as c:
        c.execute("UPDATE users SET role = ? WHERE chat_id = ?", (role, chat_id))


# ── Instâncias ──────────────────────────────────────────────

def criar_instancia(name: str, owner_chat_id: Optional[int] = None):
    with cursor() as c:
        c.execute("INSERT OR IGNORE INTO instances (name, owner_chat_id) VALUES (?, ?)",
                  (name, owner_chat_id))
        c.execute("INSERT OR IGNORE INTO stats (instance_name) VALUES (?)", (name,))
    _invalidate(name)


def remover_instancia(name: str):
    with cursor() as c:
        c.execute("DELETE FROM instances WHERE name = ?", (name,))
    _invalidate(name)


def get_instancia(name: str) -> Optional[dict]:
    with cursor() as c:
        c.execute("SELECT * FROM instances WHERE name = ?", (name,))
        row = c.fetchone()
        return dict(row) if row else None


def listar_instancias(owner_chat_id: Optional[int] = None) -> list[dict]:
    with cursor() as c:
        if owner_chat_id is None:
            c.execute("SELECT * FROM instances ORDER BY name")
        else:
            c.execute("SELECT * FROM instances WHERE owner_chat_id = ? ORDER BY name",
                      (owner_chat_id,))
        return [dict(r) for r in c.fetchall()]


def contar_instancias_do_dono(owner_chat_id: int) -> int:
    with cursor() as c:
        c.execute("SELECT COUNT(*) AS n FROM instances WHERE owner_chat_id = ?",
                  (owner_chat_id,))
        return c.fetchone()["n"]


def get_owner(instance_name: str) -> Optional[int]:
    inst = get_instancia(instance_name)
    return inst.get("owner_chat_id") if inst else None


def atualizar_instancia(name: str, **kwargs):
    if not kwargs:
        return
    campos = ", ".join(f"{k} = ?" for k in kwargs)
    valores = list(kwargs.values()) + [name]
    with cursor() as c:
        c.execute(f"UPDATE instances SET {campos} WHERE name = ?", valores)
    _invalidate(name)


def set_ativo(name: str, ativo: bool):
    atualizar_instancia(name, ativo=1 if ativo else 0)


# ── Múltiplos alvos por instância ───────────────────────────
# Persistidos como JSON na coluna `target_name`. Compat: se vier texto
# simples (instância antiga), tratamos como lista de 1 elemento.

def parse_target_names(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return [raw]


def get_target_names(instance_name: str) -> list[str]:
    inst = get_instancia(instance_name)
    return parse_target_names(inst["target_name"]) if inst else []


def set_target_names(instance_name: str, names: list[str]):
    limpos = [n.strip() for n in names if n and n.strip()]
    # remove duplicatas mantendo ordem
    vistos: set[str] = set()
    unicos: list[str] = []
    for n in limpos:
        if n not in vistos:
            vistos.add(n)
            unicos.append(n)
    atualizar_instancia(instance_name, target_name=json.dumps(unicos, ensure_ascii=False))


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
