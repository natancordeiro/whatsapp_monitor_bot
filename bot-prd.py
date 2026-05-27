#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════
  WhatsApp Group Monitor Bot — EvolutionAPI  [Multi-instância]
  Autor: TGN Technologies

  Arquitetura:
    • Multi-instância: cada registro em `instances` tem seu próprio
      grupo alvo, nome alvo e emoji. O webhook roteia por instância.
    • Sem cooldown: ao ser o primeiro, a instância é AUTO-DESLIGADA.
      O cliente liga de novo pelo Telegram quando quiser.
    • Controle 100% pelo Telegram (ver telegram_handlers.py).

  Comandos Telegram principais: /menu, /ajuda, /meuid
════════════════════════════════════════════════════════════════
"""

import os
import time
import queue
import logging
import threading
from datetime import datetime
from collections import deque

from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

import db
import evolution
import telegram_handlers

# ─────────────────────────────────────────────────────────────
# CONFIG (somente infra: porta, log, bootstrap de admins)
# ─────────────────────────────────────────────────────────────
WEBHOOK_PORT     = int(os.getenv("WEBHOOK_PORT", "5000"))
WEBHOOK_URL      = os.getenv("WEBHOOK_URL", "").rstrip("/")   # ex: http://meu-ip:5000
ADMIN_CHAT_IDS   = [int(x) for x in os.getenv("ADMIN_CHAT_IDS", "").replace(" ", "").split(",") if x]
LOG_PATH         = os.getenv("LOG_PATH", "bot.log")

# ─────────────────────────────────────────────────────────────
# LOG
# ─────────────────────────────────────────────────────────────
log_buffer: deque[str] = deque(maxlen=50)

class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
        BufferHandler(),
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Fila + dedupe por msg_id
# ─────────────────────────────────────────────────────────────
fila_mensagens: "queue.Queue[tuple[str, dict]]" = queue.Queue()
em_processamento: set[str] = set()
em_processamento_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)


# ═════════════════════════════════════════════════════════════
# Processamento (verificação assíncrona + retry + auto-stop)
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
    primeiro_id = citacoes[0].get("key", {}).get("id", "")
    log.info(f"[{instance_name}] {len(citacoes)} citações | primeiro: ...{primeiro_id[-12:]}")
    return primeiro_id == nosso_msg_id


def _verificar_e_retry(instance_name: str, group_jid: str, msg_id: str,
                        participant: str, texto: str, emoji: str,
                        nosso_msg_id: str, tentativa: int, max_retries: int,
                        check_delay: float):
    time.sleep(check_delay)

    if _verificar_se_primeiro(instance_name, group_jid, msg_id, nosso_msg_id):
        log.info(f"🏆 [{instance_name}] FOMOS OS PRIMEIROS!")
        db.incrementar_sucesso(instance_name, datetime.now().isoformat(timespec="seconds"))
        db.set_ativo(instance_name, False)   # ⏸ auto-stop
        telegram_handlers.notificar_admins(
            f"🏆 *[{instance_name}]* FOMOS OS PRIMEIROS!\n"
            f"Instância *desligada automaticamente*.\n"
            f"Use /menu para religar quando quiser."
        )
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    db.incrementar_falha(instance_name)

    if tentativa >= max_retries:
        log.warning(f"[{instance_name}] limite de {max_retries} tentativas atingido.")
        telegram_handlers.notificar_admins(
            f"⚠️ *[{instance_name}]* não consegui ser o primeiro após {max_retries} tentativas."
        )
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    log.info(f"[{instance_name}] tentativa {tentativa + 1}/{max_retries}")
    try:
        nova = evolution.enviar_emoji_citando(instance_name, group_jid, msg_id,
                                              participant, texto, emoji)
        db.incrementar_tentativa(instance_name)
    except Exception as e:
        log.error(f"[{instance_name}] erro no reenvio: {e}")
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    novo_id = nova.get("key", {}).get("id", "")
    if not novo_id:
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    threading.Thread(
        target=_verificar_e_retry,
        args=(instance_name, group_jid, msg_id, participant, texto, emoji,
              novo_id, tentativa + 1, max_retries, check_delay),
        daemon=True
    ).start()


def _processar(instance_name: str, msg_data: dict):
    """Envia primeiro emoji e dispara verificação assíncrona."""
    inst = db.get_instancia(instance_name)
    if not inst or not inst["ativo"]:
        return

    key         = msg_data.get("key", {})
    msg_id      = key.get("id", "")
    group_jid   = key.get("remoteJid", "")
    participant = key.get("participant", "")
    message     = msg_data.get("message", {})
    texto       = (message.get("conversation", "") or
                   message.get("extendedTextMessage", {}).get("text", ""))
    emoji       = inst["emoji"] or "🔥"
    max_retries = inst["max_retries"]
    check_delay = inst["check_delay"]

    log.info("━" * 50)
    log.info(f"🎯 [{instance_name}] alvo detectado: {texto[:80]}")

    telegram_handlers.notificar_admins(
        f"🎯 *[{instance_name}]* detectado!\n`{texto[:100]}`\nEnviando..."
    )

    try:
        resp = evolution.enviar_emoji_citando(instance_name, group_jid, msg_id,
                                              participant, texto, emoji)
        db.incrementar_tentativa(instance_name)
    except Exception as e:
        log.error(f"[{instance_name}] erro no envio inicial: {e}")
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    nosso_id = resp.get("key", {}).get("id", "")
    if not nosso_id:
        log.error(f"[{instance_name}] API não devolveu ID.")
        with em_processamento_lock:
            em_processamento.discard(msg_id)
        return

    threading.Thread(
        target=_verificar_e_retry,
        args=(instance_name, group_jid, msg_id, participant, texto, emoji,
              nosso_id, 1, max_retries, check_delay),
        daemon=True
    ).start()


def _worker_loop():
    log.info("⚙️  Worker iniciado.")
    while True:
        try:
            instance_name, msg_data = fila_mensagens.get(timeout=30)
            _processar(instance_name, msg_data)
        except queue.Empty:
            continue
        except Exception as e:
            log.exception(f"Erro no worker: {e}")


# ═════════════════════════════════════════════════════════════
# Webhook
# ═════════════════════════════════════════════════════════════

def _evento_normalizado(event: str) -> str:
    return (event or "").lower().replace("_", ".")


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<path:_>", methods=["POST"])   # Evolution pode enviar com sufixo
def webhook(_=None):
    data  = request.json or {}
    event = _evento_normalizado(data.get("event", ""))

    instance_name = data.get("instance") or data.get("instanceName") or ""

    if event != "messages.upsert":
        return jsonify({"status": "ignored", "event": event})

    if not instance_name:
        log.warning(f"⚠️ payload sem instance — keys: {list(data.keys())}")
        return jsonify({"status": "no_instance"})

    inst = db.get_instancia(instance_name)
    if not inst:
        # Comum: outras instâncias da mesma Evolution mandam pra cá também.
        # Silencioso pra não poluir o log.
        return jsonify({"status": "unknown_instance", "instance": instance_name})

    log.info(f"📩 webhook IN  event={event!r}  instance={instance_name!r}")
    if not inst["ativo"]:
        log.info(f"  ⏸ instância '{instance_name}' desligada.")
        return jsonify({"status": "paused", "instance": instance_name})

    target_group = inst["target_group_jid"]
    target_name  = (inst["target_name"] or "").strip().lower()
    if not target_group or not target_name:
        log.warning(f"  ⚠️ '{instance_name}' sem grupo/nome configurado.")
        return jsonify({"status": "not_configured", "instance": instance_name})

    msgs = data.get("data", [])
    if isinstance(msgs, dict):
        msgs = [msgs]

    for msg_data in msgs:
        key        = msg_data.get("key", {})
        remote_jid = key.get("remoteJid", "")
        push_name_raw = (msg_data.get("pushName") or "").strip()
        log.info(f"  📨 msg de '{push_name_raw}' em {remote_jid} | alvo={inst['target_name']} {target_group}")
        if remote_jid != target_group:
            continue

        push_name = push_name_raw.lower()
        if push_name != target_name:
            continue

        message = msg_data.get("message", {})
        texto = (message.get("conversation", "") or
                 message.get("extendedTextMessage", {}).get("text", ""))
        if not texto or "@" in texto:
            continue

        msg_id = key.get("id", "")
        with em_processamento_lock:
            if msg_id in em_processamento:
                continue
            em_processamento.add(msg_id)

        fila_mensagens.put((instance_name, msg_data))

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    instancias = db.listar_instancias()
    return jsonify({
        "status":     "online",
        "instances":  [
            {"name": i["name"], "ativo": bool(i["ativo"]),
             "grupo": i["target_group_jid"], "alvo": i["target_name"]}
            for i in instancias
        ],
        "fila":       fila_mensagens.qsize(),
    })


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 60)
    log.info("  🤖 WhatsApp Monitor Bot — Multi-instância")
    log.info("═" * 60)

    db.init_db(seed_admins=ADMIN_CHAT_IDS)
    log.info(f"  Admins:    {db.listar_admins()}")
    log.info(f"  Instâncias: {[i['name'] for i in db.listar_instancias()]}")
    log.info(f"  Porta:     {WEBHOOK_PORT}")
    log.info("═" * 60)

    evolution.warmup()

    threading.Thread(target=_worker_loop, daemon=True).start()
    threading.Thread(target=telegram_handlers.iniciar_polling, daemon=True).start()

    log.info(f"  Webhook: http://SEU-IP:{WEBHOOK_PORT}/webhook\n")
    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)
