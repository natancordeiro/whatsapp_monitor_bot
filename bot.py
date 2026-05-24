#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════
  WhatsApp Group Monitor Bot — EvolutionAPI
  Autor: TGN Technologies
  Descrição:
    Monitora um grupo do WhatsApp. Quando o número alvo enviar
    uma mensagem (sem "@"), responde com emoji citando a mensagem.
    Verifica se foi o PRIMEIRO a citar. Se não foi, tenta novamente.
════════════════════════════════════════════════════════════════
"""

import os
import time
import logging
import threading
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# CARREGAR CONFIGURAÇÕES DO .env
# ─────────────────────────────────────────────────────────────
load_dotenv()

EVOLUTION_URL        = os.getenv("EVOLUTION_URL",         "http://localhost:8080")
EVOLUTION_API_KEY    = os.getenv("EVOLUTION_API_KEY",     "")
INSTANCE_NAME        = os.getenv("INSTANCE_NAME",         "")
TARGET_GROUP_JID     = os.getenv("TARGET_GROUP_JID",      "")   # ex: 120363XXXXX@g.us
TARGET_NUMBER        = os.getenv("TARGET_NUMBER",         "")   # ex: 5511999999999
EMOJI_REPLY          = os.getenv("EMOJI_REPLY",           "🔥")
WEBHOOK_PORT         = int(os.getenv("WEBHOOK_PORT",       "5000"))
CHECK_DELAY_SECONDS  = float(os.getenv("CHECK_DELAY_SECONDS", "2.0"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES",        "10"))

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
# FLASK
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

# Controle de mensagens em processamento (evita duplicação)
em_processamento: set[str] = set()
lock = threading.Lock()


# ═════════════════════════════════════════════════════════════
# FUNÇÕES DE API (EvolutionAPI)
# ═════════════════════════════════════════════════════════════

def _headers() -> dict:
    return {
        "apikey": EVOLUTION_API_KEY,
        "Content-Type": "application/json"
    }


def enviar_emoji_citando(group_jid: str, msg_id: str, participant_jid: str, texto_original: str) -> dict | None:
    """
    Envia o emoji citando (quotando) a mensagem alvo no grupo.
    Retorna o JSON da resposta da API ou None em caso de erro.
    """
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
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        nosso_id = data.get("key", {}).get("id", "N/A")
        log.info(f"  ✅ Emoji enviado! ID da nossa mensagem: {nosso_id}")
        return data
    except Exception as e:
        log.error(f"  ❌ Erro ao enviar emoji: {e}")
        return None


def buscar_mensagens_grupo(group_jid: str, limit: int = 150) -> list:
    """
    Busca as últimas mensagens do grupo via EvolutionAPI.
    Suporta a estrutura de resposta do EvolutionAPI v1 e v2.
    """
    url = f"{EVOLUTION_URL}/chat/findMessages/{INSTANCE_NAME}"
    payload = {
        "where": {
            "key": {
                "remoteJid": group_jid
            }
        },
        "limit": limit
    }
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # EvolutionAPI v2: {"messages": {"total": N, "records": [...]}}
        records = data.get("messages", {}).get("records", [])
        if not records:
            # Fallback: estrutura plana [...]
            records = data if isinstance(data, list) else data.get("records", [])
        return records
    except Exception as e:
        log.error(f"  ❌ Erro ao buscar mensagens do grupo: {e}")
        return []


def encontrar_citacoes(mensagens: list, target_msg_id: str) -> list:
    """
    Filtra mensagens que citam (quotam) a mensagem com o ID alvo.
    Verifica os campos contextInfo dos tipos de mensagem mais comuns.
    """
    citacoes = []
    tipos_com_contexto = [
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "audioMessage",
        "documentMessage",
        "stickerMessage",
    ]

    for msg in mensagens:
        conteudo = msg.get("message", {})
        context_info = {}

        for tipo in tipos_com_contexto:
            ctx = conteudo.get(tipo, {}).get("contextInfo", {})
            if ctx:
                context_info = ctx
                break

        stanza_id = context_info.get("stanzaId", "")
        if stanza_id == target_msg_id:
            citacoes.append(msg)

    return citacoes


def verificar_se_fomos_primeiro(group_jid: str, target_msg_id: str, nosso_msg_id: str) -> bool:
    """
    Busca mensagens do grupo e verifica se a nossa foi a PRIMEIRA
    a citar a mensagem alvo (por timestamp).

    Retorna True se fomos os primeiros, False caso contrário.
    """
    mensagens = buscar_mensagens_grupo(group_jid, limit=200)
    if not mensagens:
        log.warning("  ⚠️  Não consegui buscar mensagens para verificação.")
        return False

    citacoes = encontrar_citacoes(mensagens, target_msg_id)

    if not citacoes:
        log.warning("  ⚠️  Nenhuma citação da mensagem alvo encontrada ainda.")
        return False

    # Ordenar cronologicamente
    citacoes.sort(key=lambda x: x.get("messageTimestamp", 0))

    log.info(f"  📋 Mensagens que citaram a alvo ({len(citacoes)} encontradas):")
    for i, c in enumerate(citacoes):
        ts = c.get("messageTimestamp", 0)
        horario = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        cid = c.get("key", {}).get("id", "???")
        quem = c.get("key", {}).get("participant", c.get("key", {}).get("remoteJid", "???"))
        marcador = "⭐ NÓS" if cid == nosso_msg_id else "  outro"
        log.info(f"     [{i+1}] {horario} | {marcador} | {quem} | ID: ...{cid[-15:]}")

    primeiro = citacoes[0]
    primeiro_id = primeiro.get("key", {}).get("id", "")

    if primeiro_id == nosso_msg_id:
        log.info("  🏆 FOMOS OS PRIMEIROS! Objetivo alcançado.")
        return True
    else:
        ts_primeiro = datetime.fromtimestamp(primeiro.get("messageTimestamp", 0)).strftime("%H:%M:%S")
        log.warning(f"  ❌ Não fomos os primeiros. Primeiro chegou às {ts_primeiro}.")
        return False


# ═════════════════════════════════════════════════════════════
# LÓGICA PRINCIPAL (roda em thread separada)
# ═════════════════════════════════════════════════════════════

def processar_mensagem_alvo(msg_data: dict):
    """
    Orquestra a resposta + verificação + retry para uma mensagem alvo.
    """
    key           = msg_data.get("key", {})
    msg_id        = key.get("id", "")
    group_jid     = key.get("remoteJid", "")
    participant   = key.get("participant", "")
    message       = msg_data.get("message", {})
    texto         = (
        message.get("conversation", "") or
        message.get("extendedTextMessage", {}).get("text", "")
    )

    log.info("━" * 60)
    log.info(f"🎯 MENSAGEM ALVO DETECTADA")
    log.info(f"   De:    {participant}")
    log.info(f"   Texto: {texto[:120]}")
    log.info(f"   ID:    {msg_id}")
    log.info("━" * 60)

    sucesso = False

    for tentativa in range(1, MAX_RETRIES + 1):
        log.info(f"\n🔄 Tentativa {tentativa}/{MAX_RETRIES}")

        # 1. Enviar o emoji citando a mensagem
        resposta = enviar_emoji_citando(group_jid, msg_id, participant, texto)

        if not resposta:
            log.error("  Falha no envio. Aguardando para tentar novamente...")
            time.sleep(CHECK_DELAY_SECONDS)
            continue

        nosso_msg_id = resposta.get("key", {}).get("id", "")
        if not nosso_msg_id:
            log.error("  ID da nossa mensagem não retornado pela API.")
            time.sleep(CHECK_DELAY_SECONDS)
            continue

        # 2. Aguardar propagação da mensagem
        log.info(f"  ⏳ Aguardando {CHECK_DELAY_SECONDS}s para verificar...")
        time.sleep(CHECK_DELAY_SECONDS)

        # 3. Verificar se fomos os primeiros
        sucesso = verificar_se_fomos_primeiro(group_jid, msg_id, nosso_msg_id)

        if sucesso:
            log.info("\n✅ BOT CONCLUÍDO COM SUCESSO! Monitorando próximas mensagens...")
            break
        else:
            if tentativa < MAX_RETRIES:
                log.info(f"  ⏳ Aguardando antes de nova tentativa...")
                time.sleep(CHECK_DELAY_SECONDS)

    if not sucesso:
        log.warning(f"\n⚠️  Limite de {MAX_RETRIES} tentativas atingido sem sucesso para esta mensagem.")

    log.info("━" * 60)

    # Liberar o ID para que possa ser reprocessado no futuro se necessário
    with lock:
        em_processamento.discard(msg_id)


# ═════════════════════════════════════════════════════════════
# WEBHOOK ENDPOINT
# ═════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data  = request.json or {}
    event = data.get("event", "")

    # Ignorar eventos que não sejam de mensagens recebidas
    if event != "messages.upsert":
        return jsonify({"status": "ignored", "event": event})

    msgs = data.get("data", [])
    if isinstance(msgs, dict):
        msgs = [msgs]

    for msg_data in msgs:
        key       = msg_data.get("key", {})
        from_me   = key.get("fromMe", False)
        remote_jid = key.get("remoteJid", "")

        # ── Filtros ──────────────────────────────────────────

        # Ignorar mensagens próprias
        if from_me:
            continue

        # Verificar se é o grupo alvo
        if remote_jid != TARGET_GROUP_JID:
            continue

        # Verificar se o remetente é o número alvo
        participant   = key.get("participant", "")
        numero_sender = participant.replace("@s.whatsapp.net", "").replace("+", "")
        numero_alvo   = TARGET_NUMBER.replace("+", "").replace("-", "").replace(" ", "")

        if numero_alvo not in numero_sender:
            continue

        # Extrair texto
        message = msg_data.get("message", {})
        texto   = (
            message.get("conversation", "") or
            message.get("extendedTextMessage", {}).get("text", "")
        )

        if not texto:
            continue

        # Filtro principal: ignorar mensagens com "@" no corpo
        if "@" in texto:
            log.info(f"⏭️  Mensagem ignorada (contém '@'): {texto[:60]}...")
            continue

        msg_id = key.get("id", "")

        # Evitar processar a mesma mensagem duas vezes
        with lock:
            if msg_id in em_processamento:
                log.info(f"⏭️  Mensagem {msg_id} já em processamento.")
                continue
            em_processamento.add(msg_id)

        # Processar em thread separada para não bloquear o webhook
        t = threading.Thread(
            target=processar_mensagem_alvo,
            args=(msg_data,),
            daemon=True
        )
        t.start()

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    """Endpoint de saúde para verificar se o bot está rodando."""
    return jsonify({
        "status":        "online",
        "instance":      INSTANCE_NAME,
        "grupo_alvo":    TARGET_GROUP_JID,
        "numero_alvo":   TARGET_NUMBER,
        "emoji":         EMOJI_REPLY,
        "max_tentativas": MAX_RETRIES,
        "delay_check":   f"{CHECK_DELAY_SECONDS}s"
    })


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 60)
    log.info("  🤖 WhatsApp Monitor Bot — EvolutionAPI")
    log.info("═" * 60)
    log.info(f"  Instância:       {INSTANCE_NAME}")
    log.info(f"  Grupo alvo:      {TARGET_GROUP_JID}")
    log.info(f"  Número alvo:     {TARGET_NUMBER}")
    log.info(f"  Emoji:           {EMOJI_REPLY}")
    log.info(f"  Porta Webhook:   {WEBHOOK_PORT}")
    log.info(f"  Delay checks:    {CHECK_DELAY_SECONDS}s")
    log.info(f"  Max tentativas:  {MAX_RETRIES}")
    log.info("═" * 60)
    log.info(f"  Webhook URL:     http://SEU-IP:{WEBHOOK_PORT}/webhook")
    log.info(f"  Health check:    http://SEU-IP:{WEBHOOK_PORT}/health")
    log.info("═" * 60 + "\n")

    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)
