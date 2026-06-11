#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════
  WhatsApp Group Monitor Bot — EvolutionAPI  [HOT PATH ⚡]
  Autor: TGN Technologies

  Arquitetura otimizada para minimizar o tempo entre
  recebimento do webhook e envio do emoji:

    1. Cache em memória das instâncias  → 0 SQLite reads no hot path
    2. ThreadPoolExecutor                → sem fila/worker, paralelismo real
    3. Send-first, log/db-later          → POST p/ Evolution antes de qualquer I/O
    4. Logging assíncrono (QueueHandler) → escrita em arquivo fora do hot path
    5. requests.Session com pool 64       → reuso TCP/TLS
    6. Keep-alive periódico               → conexão sempre quente
    7. Waitress em produção (Dockerfile) → WSGI threaded de verdade
════════════════════════════════════════════════════════════════
"""

import os
import html
import time
import logging
import threading
from datetime import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import QueueHandler, QueueListener
from queue import Queue

from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

import db
import evolution
import telegram_handlers

# ─────────────────────────────────────────────────────────────
# CONFIG (somente infra)
# ─────────────────────────────────────────────────────────────
WEBHOOK_PORT   = int(os.getenv("WEBHOOK_PORT", "5000"))
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").rstrip("/")
ADMIN_CHAT_IDS = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x]
LOG_PATH       = os.getenv("LOG_PATH", "bot.log")
SEND_POOL_SIZE = int(os.getenv("SEND_POOL_SIZE", "16"))
# Quanto tempo um user comum espera antes de enviar quando há admin ativo
# no MESMO grupo. Garante prioridade do admin em caso de concorrência.
USER_PRIORITY_DELAY_MS = int(os.getenv("USER_PRIORITY_DELAY_MS", "150"))

# ─────────────────────────────────────────────────────────────
# LOGGING ASSÍNCRONO
# Hot path emite com QueueHandler (zerinho de I/O); listener
# em thread separada persiste em arquivo/stdout/buffer.
# ─────────────────────────────────────────────────────────────
log_buffer: deque[str] = deque(maxlen=100)

class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))

_log_queue: Queue = Queue(-1)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_handlers_reais = [
    logging.FileHandler(LOG_PATH, encoding="utf-8"),
    logging.StreamHandler(),
    BufferHandler(),
]
for h in _handlers_reais:
    h.setFormatter(_fmt)

_listener = QueueListener(_log_queue, *_handlers_reais, respect_handler_level=False)
_listener.start()

root = logging.getLogger()
root.setLevel(logging.INFO)
root.addHandler(QueueHandler(_log_queue))

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CACHE EM MEMÓRIA DE INSTÂNCIAS
# Sem isso pagamos SQLite + lock global toda vez que chega um webhook.
# ─────────────────────────────────────────────────────────────
_inst_cache: dict[str, dict | None] = {}
_inst_cache_lock = threading.Lock()


def _cache_get(name: str) -> dict | None:
    with _inst_cache_lock:
        if name in _inst_cache:
            return _inst_cache[name]
    inst = db.get_instancia(name)
    if inst:
        # pré-computa set de alvos em lowercase pra o hot path ser O(1)
        nomes = db.parse_target_names(inst["target_name"])
        inst["_targets_lower"] = frozenset(n.strip().lower() for n in nomes if n.strip())
        # pré-resolve a role do dono pra evitar lookup no hot path
        owner = inst.get("owner_chat_id")
        if owner:
            u = db.get_usuario(owner)
            inst["_owner_role"] = u["role"] if u else None
        else:
            inst["_owner_role"] = None
        # pré-computa filtros de texto bloqueado (lowercase)
        bloqueados = db.parse_textos_bloqueados(inst.get("texto_bloqueado"))
        inst["_blocked_lower"] = tuple(b.lower() for b in bloqueados if b)
    with _inst_cache_lock:
        _inst_cache[name] = inst
    return inst


# Cache de grupos onde existe uma instância de admin ATIVA.
# Usado pra decidir se uma instância de user comum deve ceder a vez.
_admin_groups_cache: set[str] | None = None
_admin_groups_lock = threading.Lock()


def _grupos_com_admin_ativo() -> set[str]:
    global _admin_groups_cache
    with _admin_groups_lock:
        if _admin_groups_cache is not None:
            return _admin_groups_cache
        grupos: set[str] = set()
        for inst in db.listar_instancias():
            if not inst["ativo"]:
                continue
            owner = inst.get("owner_chat_id")
            if not owner or not db.is_admin(owner):
                continue
            gjid = inst.get("target_group_jid")
            if gjid:
                grupos.add(gjid)
        _admin_groups_cache = grupos
        return grupos


def _cache_invalidate(name: str | None = None):
    global _admin_groups_cache
    with _inst_cache_lock:
        if name is None:
            _inst_cache.clear()
        else:
            _inst_cache.pop(name, None)
    # Invalida também o cache de "grupos com admin ativo"
    with _admin_groups_lock:
        _admin_groups_cache = None

# Plugando o invalidator no módulo db
db.set_invalidate_hook(_cache_invalidate)

# ─────────────────────────────────────────────────────────────
# DEDUPE — mensagens já processadas
# ─────────────────────────────────────────────────────────────
em_processamento: set[str] = set()
em_processamento_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
# THREAD POOL — todo envio passa por aqui (sem fila intermediária)
# ─────────────────────────────────────────────────────────────
executor = ThreadPoolExecutor(max_workers=SEND_POOL_SIZE, thread_name_prefix="send")

# ─────────────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)


# ═════════════════════════════════════════════════════════════
# Verificação assíncrona (em background, fora do hot path)
# ═════════════════════════════════════════════════════════════

def _encontrar_citacoes(mensagens: list, target_msg_id: str) -> list:
    out = []
    for m in mensagens:
        ctx = m.get("contextInfo") or {}
        if ctx.get("stanzaId", "") == target_msg_id:
            out.append(m)
    return out


def _verificar_se_primeiro(instance_name: str, group_jid: str,
                            target_msg_id: str, nosso_msg_id: str) -> bool:
    try:
        mensagens = evolution.buscar_mensagens(instance_name, group_jid, limit=50)
    except Exception as e:
        log.error(f"[{instance_name}] erro ao buscar mensagens: {e}")
        return False

    citacoes = _encontrar_citacoes(mensagens, target_msg_id)
    if not citacoes:
        return False
    citacoes.sort(key=lambda x: x.get("messageTimestamp", 0))
    return citacoes[0].get("key", {}).get("id", "") == nosso_msg_id


def _verificar_uma_vez(instance_name: str, group_jid: str, msg_id: str,
                         nosso_msg_id: str, check_delay: float):
    """
    Após o envio, espera `check_delay`, confere se fomos o primeiro a citar e
    DESLIGA a instância em ambos os casos (sucesso ou fracasso). Sem retry.
    """
    time.sleep(check_delay)

    fomos_primeiro = _verificar_se_primeiro(instance_name, group_jid, msg_id, nosso_msg_id)

    # Auto-stop em qualquer cenário — o cliente religa quando quiser.
    db.set_ativo(instance_name, False)

    if fomos_primeiro:
        db.incrementar_sucesso(instance_name, datetime.now().isoformat(timespec="seconds"))
        s = db.get_stats(instance_name)
        log.info(f"🏆 [{instance_name}] FOMOS OS PRIMEIROS! "
                 f"stats agora: ✅ {s['sucesso']} | ❌ {s['falha']}")
        prefixo = "🏆 <b>FOMOS OS PRIMEIROS!</b>\nInstância desligada automaticamente."
    else:
        db.incrementar_falha(instance_name)
        s = db.get_stats(instance_name)
        log.warning(f"[{instance_name}] não fomos o primeiro — desligando. "
                    f"stats agora: ✅ {s['sucesso']} | ❌ {s['falha']}")
        prefixo = "⚠️ <b>Não fomos o primeiro.</b>\nInstância desligada automaticamente."

    telegram_handlers.notificar_dono_com_menu_instancia(instance_name, prefixo=prefixo)

    with em_processamento_lock:
        em_processamento.discard(msg_id)


# ═════════════════════════════════════════════════════════════
# HOT PATH ⚡  — função única que é chamada pelo executor
#   1. Envia POST IMEDIATAMENTE (esse é o único trabalho crítico)
#   2. Tudo o resto (log, db, telegram, verify) vem depois
# ═════════════════════════════════════════════════════════════

def _enviar_imediato(instance_name: str, inst: dict, msg_data: dict, t_webhook: float):
    key         = msg_data["key"]
    msg_id      = key["id"]
    group_jid   = key["remoteJid"]
    participant = key.get("participant", "")
    message     = msg_data.get("message") or {}
    texto       = (message.get("conversation")
                   or (message.get("extendedTextMessage") or {}).get("text")
                   or "")
    emoji       = inst["emoji"] or "🔥"

    # Prioridade: se não sou admin E há admin ativo no MESMO grupo, cedo a vez
    if (USER_PRIORITY_DELAY_MS > 0
            and inst.get("_owner_role") != "admin"
            and group_jid in _grupos_com_admin_ativo()):
        log.info(f"⏳ [{instance_name}] aguardando {USER_PRIORITY_DELAY_MS}ms — admin tem prioridade no grupo")
        time.sleep(USER_PRIORITY_DELAY_MS / 1000.0)

    t_send = time.perf_counter()
    try:
        resp = evolution.enviar_emoji_citando(instance_name, group_jid, msg_id,
                                              participant, texto, emoji)
    except Exception as e:
        log.error(f"⚡ [{instance_name}] FALHOU envio: {e}")
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    t_done = time.perf_counter()
    nosso_id = (resp.get("key") or {}).get("id", "")

    # ── Telemetria ──────────────────────────────────────────
    dt_wh_to_send = (t_send  - t_webhook) * 1000   # webhook → começo POST
    dt_post       = (t_done  - t_send)    * 1000   # tempo da Evolution
    dt_total      = (t_done  - t_webhook) * 1000   # total
    log.info(
        f"⚡ [{instance_name}] emoji enviado | "
        f"interno={dt_wh_to_send:.0f}ms | post={dt_post:.0f}ms | total={dt_total:.0f}ms | "
        f"texto={texto[:60]!r}"
    )

    # ── Pós-envio (sem afetar o tempo de resposta) ──────────
    executor.submit(_pos_envio, instance_name, inst, group_jid, msg_id,
                    texto, nosso_id)


def _pos_envio(instance_name: str, inst: dict, group_jid: str, msg_id: str,
                texto: str, nosso_id: str):
    """Tudo que pode esperar: telemetria, telegram, verificação."""
    try:
        db.incrementar_tentativa(instance_name)
    except Exception as e:
        log.warning(f"incrementar_tentativa: {e}")

    telegram_handlers.notificar_alvo(
        instance_name,
        f"🎯 <b>[{html.escape(instance_name)}]</b> alvo detectado!\n"
        f"<code>{html.escape(texto[:120])}</code>"
    )

    if not nosso_id:
        log.error(f"[{instance_name}] Evolution não devolveu ID — não vou verificar.")
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    executor.submit(_verificar_uma_vez, instance_name, group_jid, msg_id,
                    nosso_id, inst["check_delay"])


# ═════════════════════════════════════════════════════════════
# WEBHOOK — só faz validação rápida e despacha
# ═════════════════════════════════════════════════════════════

def _evento_normalizado(event: str) -> str:
    return (event or "").lower().replace("_", ".")


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<path:_>", methods=["POST"])
def webhook(_=None):
    t_webhook = time.perf_counter()

    data = request.get_json(silent=True) or {}
    event = _evento_normalizado(data.get("event", ""))

    if event != "messages.upsert":
        return jsonify({"status": "ignored"})

    instance_name = data.get("instance") or data.get("instanceName") or ""
    if not instance_name:
        return jsonify({"status": "no_instance"})

    inst = _cache_get(instance_name)
    if not inst:
        return jsonify({"status": "unknown_instance"})
    if not inst["ativo"]:
        return jsonify({"status": "paused"})

    target_group  = inst["target_group_jid"]
    targets_lower = inst.get("_targets_lower") or frozenset()
    if not target_group or not targets_lower:
        return jsonify({"status": "not_configured"})

    msgs = data.get("data", [])
    if isinstance(msgs, dict):
        msgs = [msgs]

    despachados = 0
    for msg_data in msgs:
        key        = msg_data.get("key") or {}
        remote_jid = key.get("remoteJid", "")
        if remote_jid != target_group:
            continue

        push_name = (msg_data.get("pushName") or "").strip().lower()
        if push_name not in targets_lower:
            continue

        message = msg_data.get("message") or {}
        texto = (message.get("conversation")
                 or (message.get("extendedTextMessage") or {}).get("text")
                 or "")
        if not texto:
            continue
        # Filtros configuráveis por instância (case-insensitive, substring)
        bloqueados = inst.get("_blocked_lower") or ()
        if bloqueados:
            texto_lower = texto.lower()
            if any(b in texto_lower for b in bloqueados):
                continue

        msg_id = key.get("id", "")
        with em_processamento_lock:
            if msg_id in em_processamento:
                continue
            em_processamento.add(msg_id)

        # ⚡ Submete IMEDIATAMENTE. Não bloqueia o webhook.
        executor.submit(_enviar_imediato, instance_name, inst, msg_data, t_webhook)
        despachados += 1

    return jsonify({"status": "ok", "despachados": despachados})


@app.route("/health", methods=["GET"])
def health():
    instancias = db.listar_instancias()
    return jsonify({
        "status":    "online",
        "instances": [
            {"name": i["name"], "ativo": bool(i["ativo"]),
             "grupo": i["target_group_jid"], "alvo": i["target_name"]}
            for i in instancias
        ],
        "pool_size": SEND_POOL_SIZE,
    })


# ═════════════════════════════════════════════════════════════
# MAIN — usa waitress em produção (Linux/Docker), Flask local
# ═════════════════════════════════════════════════════════════

def _run_server():
    try:
        from waitress import serve
        log.info(f"  Servindo via waitress (threads={SEND_POOL_SIZE * 2})")
        serve(app, host="0.0.0.0", port=WEBHOOK_PORT,
              threads=SEND_POOL_SIZE * 2, _quiet=True)
    except ImportError:
        log.info("  waitress não disponível — usando Flask dev server.")
        app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    log.info("═" * 60)
    log.info("  🤖 WhatsApp Monitor Bot — HOT PATH ⚡")
    log.info("═" * 60)

    db.init_db(seed_admins=ADMIN_CHAT_IDS)
    log.info(f"  DB_PATH:    {db.DB_PATH}")
    log.info(f"  LOG_PATH:   {LOG_PATH}")
    log.info(f"  Admins:     {db.listar_admins()}")
    instancias_existentes = db.listar_instancias()
    log.info(f"  Instâncias: {[i['name'] for i in instancias_existentes]}")
    for i in instancias_existentes:
        s = db.get_stats(i["name"])
        log.info(f"    └─ {i['name']:20s} ✅ {s['sucesso']} | ❌ {s['falha']} | "
                 f"última: {s['ultima_captura'] or '—'}")
    log.info(f"  Pool size:  {SEND_POOL_SIZE}")
    log.info(f"  Porta:      {WEBHOOK_PORT}")
    log.info("═" * 60)

    # Aviso se o DB está em caminho NÃO persistido (sem volume Docker)
    if not db.DB_PATH.startswith("/data") and db.DB_PATH != "bot.db":
        pass
    elif db.DB_PATH.startswith("/data"):
        import os as _os
        if not _os.path.exists("/data"):
            log.warning("⚠️  /data NÃO existe — DB e logs não vão persistir!")
        elif not instancias_existentes:
            log.info("ℹ️  Nenhuma instância salva (primeira execução ou volume vazio).")

    evolution.warmup()
    evolution.iniciar_keepalive_loop(intervalo_seg=25)

    threading.Thread(target=telegram_handlers.iniciar_polling,
                     daemon=True, name="telegram-polling").start()

    log.info(f"  Webhook: {WEBHOOK_URL or f'http://SEU-IP:{WEBHOOK_PORT}'}/webhook\n")
    _run_server()
