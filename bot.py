#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════
  WhatsApp Group Monitor Bot — EvolutionAPI  [TURBO MODE]
  Autor: TGN Technologies

  OTIMIZAÇÕES v2:
  ① requests.Session global   → reutiliza conexão TCP (sem handshake a cada envio)
  ② Worker thread pré-iniciado → sem overhead de criar thread no momento crítico
  ③ Webhook retorna em <5ms   → Flask não bloqueia esperando o envio
  ④ Verificação em thread separada → envio e verificação correm em paralelo
════════════════════════════════════════════════════════════════
"""

import os
import time
import queue
import logging
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÕES
# ─────────────────────────────────────────────────────────────
load_dotenv()

EVOLUTION_URL       = os.getenv("EVOLUTION_URL",         "http://localhost:8080")
EVOLUTION_API_KEY   = os.getenv("EVOLUTION_API_KEY",     "")
INSTANCE_NAME       = os.getenv("INSTANCE_NAME",         "")
TARGET_GROUP_JID    = os.getenv("TARGET_GROUP_JID",      "")
TARGET_NAME         = os.getenv("TARGET_NAME",           "")   # pushName do alvo
EMOJI_REPLY         = os.getenv("EMOJI_REPLY",           "🔥")
WEBHOOK_PORT        = int(os.getenv("WEBHOOK_PORT",       "5000"))
CHECK_DELAY_SECONDS = float(os.getenv("CHECK_DELAY_SECONDS", "1.5"))
MAX_RETRIES         = int(os.getenv("MAX_RETRIES",        "10"))

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# ① SESSION GLOBAL — reutiliza conexão TCP
#    Elimina o handshake TLS/TCP a cada request (~500ms de ganho)
# ─────────────────────────────────────────────────────────────
api_session = requests.Session()
api_session.headers.update({
    "apikey": EVOLUTION_API_KEY,
    "Content-Type": "application/json"
})

def _warmup_connection():
    """Pré-aquece a conexão com o servidor Evolution na inicialização."""
    try:
        api_session.get(f"{EVOLUTION_URL}/instance/fetchInstances", timeout=5)
        log.info("  🔌 Conexão com EvolutionAPI pré-aquecida.")
    except Exception:
        log.warning("  ⚠️  Warmup falhou (normal se a rota não existir). Continuando...")

# ─────────────────────────────────────────────────────────────
# ② FILA + WORKER PRÉ-INICIADO
#    A thread já está rodando antes de qualquer mensagem chegar.
#    Quando a mensagem chega, é só fazer queue.put() e a thread
#    processa imediatamente — sem tempo de criação de thread.
# ─────────────────────────────────────────────────────────────
fila_mensagens: queue.Queue = queue.Queue()

# Controle de duplicatas
em_processamento: set[str] = set()
lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
# FLASK
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)


# ═════════════════════════════════════════════════════════════
# FUNÇÕES DE API
# ═════════════════════════════════════════════════════════════

def enviar_emoji_citando(group_jid: str, msg_id: str, participant_jid: str, texto_original: str) -> dict | None:
    """Envia o emoji citando a mensagem alvo. Usa Session global (sem handshake)."""
    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE_NAME}"
    payload = {
        "number": group_jid,
        "text": EMOJI_REPLY,
        "quoted": {
            "key": {
                "remoteJid": group_jid,
                "fromMe": False,
                "id": msg_id,
                "participant": participant_jid
            },
            "message": {
                "conversation": texto_original
            }
        }
    }
    t0 = time.perf_counter()
    try:
        resp = api_session.post(url, json=payload, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        ms = int((time.perf_counter() - t0) * 1000)
        nosso_id = data.get("key", {}).get("id", "N/A")
        log.info(f"  ✅ Emoji enviado em {ms}ms | ID: {nosso_id}")
        return data
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        log.error(f"  ❌ Erro ao enviar emoji ({ms}ms): {e}")
        return None


def buscar_mensagens_grupo(group_jid: str, limit: int = 50) -> list:
    """Busca mensagens recentes do grupo."""
    url = f"{EVOLUTION_URL}/chat/findMessages/{INSTANCE_NAME}"
    payload = {
        "where": {"key": {"remoteJid": group_jid}},
        "limit": limit
    }
    try:
        resp = api_session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        records = data.get("messages", {}).get("records", [])
        if not records:
            records = data if isinstance(data, list) else data.get("records", [])
        return records
    except Exception as e:
        log.error(f"  ❌ Erro ao buscar mensagens: {e}")
        return []


def encontrar_citacoes(mensagens: list, target_msg_id: str) -> list:
    """Filtra mensagens que citam a mensagem alvo pelo stanzaId."""
    citacoes = []
    for msg in mensagens:
        context_info = msg.get("contextInfo", {})
        if not context_info:
            continue
        if context_info.get("stanzaId", "") == target_msg_id:
            citacoes.append(msg)
    return citacoes


def verificar_se_fomos_primeiro(group_jid: str, target_msg_id: str, nosso_msg_id: str) -> bool:
    """Verifica se nossa mensagem foi a primeira a citar a mensagem alvo."""
    mensagens = buscar_mensagens_grupo(group_jid, limit=50)
    if not mensagens:
        log.warning("  ⚠️  Não foi possível buscar mensagens para verificação.")
        return False

    citacoes = encontrar_citacoes(mensagens, target_msg_id)
    if not citacoes:
        log.warning("  ⚠️  Nenhuma citação encontrada ainda.")
        return False

    citacoes.sort(key=lambda x: x.get("messageTimestamp", 0))

    log.info(f"  📋 Citações encontradas ({len(citacoes)}):")
    for i, c in enumerate(citacoes):
        ts      = c.get("messageTimestamp", 0)
        horario = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        cid     = c.get("key", {}).get("id", "???")
        quem    = c.get("key", {}).get("participant", c.get("key", {}).get("remoteJid", "???"))
        marca   = "⭐ NÓS" if cid == nosso_msg_id else "  outro"
        log.info(f"     [{i+1}] {horario} | {marca} | {quem} | ID: ...{cid[-15:]}")

    primeiro_id = citacoes[0].get("key", {}).get("id", "")
    if primeiro_id == nosso_msg_id:
        log.info("  🏆 FOMOS OS PRIMEIROS!")
        return True
    else:
        ts = datetime.fromtimestamp(citacoes[0].get("messageTimestamp", 0)).strftime("%H:%M:%S")
        log.warning(f"  ❌ Não fomos os primeiros. Outro chegou às {ts}.")
        return False


# ═════════════════════════════════════════════════════════════
# ④ VERIFICAÇÃO ASSÍNCRONA
#    Roda em thread separada APÓS o envio, sem bloquear o worker.
#    O worker fica livre para processar a próxima mensagem.
# ═════════════════════════════════════════════════════════════

def _verificar_e_retry(group_jid: str, msg_id: str, participant_jid: str,
                        texto: str, nosso_msg_id: str, tentativa: int):
    """
    Thread de verificação + retry.
    Aguarda CHECK_DELAY, verifica se fomos primeiros.
    Se não, reenvia e cria nova thread de verificação.
    """
    time.sleep(CHECK_DELAY_SECONDS)

    sucesso = verificar_se_fomos_primeiro(group_jid, msg_id, nosso_msg_id)

    if sucesso:
        log.info("✅ BOT CONCLUÍDO COM SUCESSO! Aguardando próximas mensagens...")
        with lock:
            em_processamento.discard(msg_id)
        return

    if tentativa >= MAX_RETRIES:
        log.warning(f"⚠️  Limite de {MAX_RETRIES} tentativas atingido.")
        with lock:
            em_processamento.discard(msg_id)
        return

    # Reenviar e criar nova thread de verificação
    log.info(f"\n🔄 Tentativa {tentativa + 1}/{MAX_RETRIES} — reenviando...")
    nova_resposta = enviar_emoji_citando(group_jid, msg_id, participant_jid, texto)
    if nova_resposta:
        novo_id = nova_resposta.get("key", {}).get("id", "")
        if novo_id:
            t = threading.Thread(
                target=_verificar_e_retry,
                args=(group_jid, msg_id, participant_jid, texto, novo_id, tentativa + 1),
                daemon=True
            )
            t.start()
            return

    # Falha no reenvio: liberar e desistir
    with lock:
        em_processamento.discard(msg_id)


# ═════════════════════════════════════════════════════════════
# PROCESSADOR PRINCIPAL (roda no worker thread)
# ═════════════════════════════════════════════════════════════

def processar_mensagem_alvo(msg_data: dict):
    """
    Caminho crítico: extrai dados e dispara o envio o mais rápido possível.
    A verificação é delegada a uma thread separada (não bloqueia).
    """
    key         = msg_data.get("key", {})
    msg_id      = key.get("id", "")
    group_jid   = key.get("remoteJid", "")
    participant = key.get("participant", "")
    message     = msg_data.get("message", {})
    texto       = (
        message.get("conversation", "") or
        message.get("extendedTextMessage", {}).get("text", "")
    )

    log.info("━" * 60)
    log.info("🎯 MENSAGEM ALVO DETECTADA")
    log.info(f"   De:    {participant}")
    log.info(f"   Texto: {texto[:120]}")
    log.info(f"   ID:    {msg_id}")
    log.info("━" * 60)
    log.info("🔄 Tentativa 1/%d — enviando emoji...", MAX_RETRIES)

    # ── ENVIO IMEDIATO ────────────────────────────────────────
    resposta = enviar_emoji_citando(group_jid, msg_id, participant, texto)

    if not resposta:
        log.error("Falha no envio inicial. Abortando para esta mensagem.")
        with lock:
            em_processamento.discard(msg_id)
        return

    nosso_msg_id = resposta.get("key", {}).get("id", "")
    if not nosso_msg_id:
        log.error("ID da nossa mensagem não retornado. Abortando.")
        with lock:
            em_processamento.discard(msg_id)
        return

    # ── VERIFICAÇÃO ASSÍNCRONA ────────────────────────────────
    # Não bloqueia o worker. A thread de verificação cuida do retry.
    t = threading.Thread(
        target=_verificar_e_retry,
        args=(group_jid, msg_id, participant, texto, nosso_msg_id, 1),
        daemon=True
    )
    t.start()


# ═════════════════════════════════════════════════════════════
# ② WORKER LOOP — thread sempre em espera, zero overhead de criação
# ═════════════════════════════════════════════════════════════

def _worker_loop():
    log.info("⚙️  Worker pré-iniciado. Aguardando mensagens...")
    while True:
        try:
            msg_data = fila_mensagens.get(timeout=30)
            processar_mensagem_alvo(msg_data)
        except queue.Empty:
            continue
        except Exception as e:
            log.error(f"Erro no worker: {e}")


# ═════════════════════════════════════════════════════════════
# ③ WEBHOOK — retorna em <5ms, sem esperar o envio
# ═════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data  = request.json or {}
    event = data.get("event", "")

    if event != "messages.upsert":
        return jsonify({"status": "ignored", "event": event})

    msgs = data.get("data", [])
    if isinstance(msgs, dict):
        msgs = [msgs]

    for msg_data in msgs:
        key        = msg_data.get("key", {})
        remote_jid = key.get("remoteJid", "")

        # Filtro: grupo alvo
        if remote_jid != TARGET_GROUP_JID:
            continue

        # Filtro: nome alvo (pushName)
        push_name = (msg_data.get("pushName") or "").strip().lower()
        nome_alvo = TARGET_NAME.strip().lower()
        if not nome_alvo or push_name != nome_alvo:
            continue

        # Filtro: extrair texto
        message = msg_data.get("message", {})
        texto = (
            message.get("conversation", "") or
            message.get("extendedTextMessage", {}).get("text", "")
        )
        if not texto:
            continue

        # Filtro: ignorar mensagens com "@"
        if "@" in texto:
            log.info(f"⏭️  Ignorado (contém '@'): {texto[:50]}...")
            continue

        msg_id = key.get("id", "")

        # Filtro: deduplicação
        with lock:
            if msg_id in em_processamento:
                continue
            em_processamento.add(msg_id)

        # ── Enfileirar e retornar IMEDIATAMENTE ──────────────
        fila_mensagens.put(msg_data)

    # Resposta ao Evolution em milissegundos
    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":          "online",
        "instance":        INSTANCE_NAME,
        "grupo_alvo":      TARGET_GROUP_JID,
        "nome_alvo":       TARGET_NAME,
        "emoji":           EMOJI_REPLY,
        "max_tentativas":  MAX_RETRIES,
        "check_delay":     f"{CHECK_DELAY_SECONDS}s",
        "fila_pendente":   fila_mensagens.qsize()
    })


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 60)
    log.info("  🤖 WhatsApp Monitor Bot — TURBO MODE")
    log.info("═" * 60)
    log.info(f"  Instância:       {INSTANCE_NAME}")
    log.info(f"  Grupo alvo:      {TARGET_GROUP_JID}")
    log.info(f"  Nome alvo:       {TARGET_NAME}")
    log.info(f"  Emoji:           {EMOJI_REPLY}")
    log.info(f"  Porta Webhook:   {WEBHOOK_PORT}")
    log.info(f"  Delay checks:    {CHECK_DELAY_SECONDS}s")
    log.info(f"  Max tentativas:  {MAX_RETRIES}")
    log.info("═" * 60)

    # Pré-aquecer conexão TCP com o servidor Evolution
    _warmup_connection()

    # Iniciar worker pré-aquecido (sem overhead no momento da mensagem)
    worker = threading.Thread(target=_worker_loop, daemon=True)
    worker.start()

    log.info(f"  Webhook URL:  http://SEU-IP:{WEBHOOK_PORT}/webhook")
    log.info(f"  Health:       http://SEU-IP:{WEBHOOK_PORT}/health")
    log.info("═" * 60 + "\n")

    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)