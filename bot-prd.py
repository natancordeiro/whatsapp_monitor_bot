#!/usr/bin/env python3
"""
════════════════════════════════════════════════════════════════
  WhatsApp Group Monitor Bot — EvolutionAPI  [TURBO MODE v3]
  Autor: TGN Technologies

  NOVIDADES v3:
  ⑤ Cooldown configurável após sucesso
  ⑥ Bot Telegram para controle pelo celular
     /status  /ligar  /desligar  /log  /config
════════════════════════════════════════════════════════════════
"""

import os
import time
import queue
import logging
import threading
import requests
import telebot
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from datetime import datetime, timedelta
from collections import deque

# ─────────────────────────────────────────────────────────────
# CONFIGURAÇÕES
# ─────────────────────────────────────────────────────────────
load_dotenv()

EVOLUTION_URL       = os.getenv("EVOLUTION_URL",         "http://localhost:8080")
EVOLUTION_API_KEY   = os.getenv("EVOLUTION_API_KEY",     "")
INSTANCE_NAME       = os.getenv("INSTANCE_NAME",         "")
TARGET_GROUP_JID    = os.getenv("TARGET_GROUP_JID",      "")
TARGET_NAME         = os.getenv("TARGET_NAME",           "")
EMOJI_REPLY         = os.getenv("EMOJI_REPLY",           "🔥")
WEBHOOK_PORT        = int(os.getenv("WEBHOOK_PORT",       "5000"))
CHECK_DELAY_SECONDS = float(os.getenv("CHECK_DELAY_SECONDS", "1.5"))
MAX_RETRIES         = int(os.getenv("MAX_RETRIES",        "10"))
COOLDOWN_MINUTOS    = float(os.getenv("COOLDOWN_MINUTOS", "5.0"))

TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN",        "")
TELEGRAM_CHAT_ID    = int(os.getenv("TELEGRAM_CHAT_ID",  "0"))

# ─────────────────────────────────────────────────────────────
# LOG — buffer em memória para o comando /log do Telegram
# ─────────────────────────────────────────────────────────────
log_buffer: deque[str] = deque(maxlen=30)

class BufferHandler(logging.Handler):
    def emit(self, record):
        log_buffer.append(self.format(record))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
        BufferHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# ESTADO GLOBAL DO BOT
# ─────────────────────────────────────────────────────────────
estado = {
    "ativo":           True,               # monitoramento ligado/desligado
    "cooldown_ate":    0.0,                # timestamp fim do cooldown (0 = sem cooldown)
    "ultima_captura":  None,               # datetime da última captura com sucesso
    "sucesso":         0,                  # total de vezes que fomos os primeiros
    "falha":           0,                  # total de vezes que perdemos
    "tentativas":      0,                  # total de tentativas de envio
}
estado_lock = threading.Lock()

def em_cooldown() -> bool:
    return time.time() < estado["cooldown_ate"]

def cooldown_restante_str() -> str:
    restante = estado["cooldown_ate"] - time.time()
    if restante <= 0:
        return "sem cooldown"
    m, s = divmod(int(restante), 60)
    return f"{m}min {s}s"

def iniciar_cooldown():
    with estado_lock:
        estado["cooldown_ate"] = time.time() + COOLDOWN_MINUTOS * 60
        estado["ultima_captura"] = datetime.now()
        estado["sucesso"] += 1
    fim = datetime.fromtimestamp(estado["cooldown_ate"]).strftime("%H:%M:%S")
    log.info(f"⏸️  Cooldown iniciado por {COOLDOWN_MINUTOS} min. Retomando às {fim}.")
    notificar_telegram(
        f"✅ *CAPTUREI PRIMEIRO!*\n"
        f"⏸️ Cooldown: *{COOLDOWN_MINUTOS} min*\n"
        f"🔁 Retomando às *{fim}*"
    )

# ─────────────────────────────────────────────────────────────
# SESSION GLOBAL — sem handshake TCP a cada request
# ─────────────────────────────────────────────────────────────
api_session = requests.Session()
api_session.headers.update({
    "apikey": EVOLUTION_API_KEY,
    "Content-Type": "application/json"
})

def _warmup_connection():
    try:
        api_session.get(f"{EVOLUTION_URL}/instance/fetchInstances", timeout=5)
        log.info("  🔌 Conexão com EvolutionAPI pré-aquecida.")
    except Exception:
        log.warning("  ⚠️  Warmup falhou (normal se a rota não existir).")

# ─────────────────────────────────────────────────────────────
# TELEGRAM — notificações e comandos
# ─────────────────────────────────────────────────────────────
tg_bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="Markdown") if TELEGRAM_TOKEN else None

def notificar_telegram(msg: str):
    """Envia mensagem para o chat do Telegram (sem bloquear)."""
    if not tg_bot or not TELEGRAM_CHAT_ID:
        return
    def _send():
        try:
            tg_bot.send_message(TELEGRAM_CHAT_ID, msg)
        except Exception as e:
            log.warning(f"Telegram send error: {e}")
    threading.Thread(target=_send, daemon=True).start()

def _checar_chat_id(message) -> bool:
    """Bloqueia comandos de chats não autorizados."""
    if TELEGRAM_CHAT_ID and message.chat.id != TELEGRAM_CHAT_ID:
        tg_bot.reply_to(message, "⛔ Acesso não autorizado.")
        return False
    return True

if tg_bot:
    @tg_bot.message_handler(commands=["start", "ligar"])
    def cmd_ligar(message):
        if not _checar_chat_id(message): return
        with estado_lock:
            estado["ativo"] = True
            estado["cooldown_ate"] = 0.0   # reseta cooldown se houver
        tg_bot.reply_to(message, "✅ *Bot LIGADO.* Monitoramento ativo.")
        log.info("🟢 Bot ligado via Telegram.")

    @tg_bot.message_handler(commands=["stop", "desligar"])
    def cmd_desligar(message):
        if not _checar_chat_id(message): return
        with estado_lock:
            estado["ativo"] = False
        tg_bot.reply_to(message, "⛔ *Bot DESLIGADO.* Mensagens serão ignoradas.")
        log.info("🔴 Bot desligado via Telegram.")

    @tg_bot.message_handler(commands=["status"])
    def cmd_status(message):
        if not _checar_chat_id(message): return
        s = estado
        ativo_str    = "🟢 LIGADO" if s["ativo"] else "🔴 DESLIGADO"
        cooldown_str = f"⏸️ Em cooldown — {cooldown_restante_str()}" if em_cooldown() else "▶️ Monitorando"
        captura_str  = s["ultima_captura"].strftime("%d/%m %H:%M:%S") if s["ultima_captura"] else "nenhuma ainda"
        taxa = f"{s['sucesso']}/{s['sucesso']+s['falha']}" if (s['sucesso']+s['falha']) > 0 else "—"
        tg_bot.reply_to(message,
            f"📊 *STATUS DO BOT*\n"
            f"──────────────────\n"
            f"*Estado:* {ativo_str}\n"
            f"*Monitor:* {cooldown_str}\n"
            f"*Última captura:* {captura_str}\n"
            f"*Taxa de sucesso:* {taxa}\n"
            f"*Cooldown config:* {COOLDOWN_MINUTOS} min\n"
            f"*Check delay:* {CHECK_DELAY_SECONDS}s\n"
            f"*Max tentativas:* {MAX_RETRIES}\n"
            f"──────────────────\n"
            f"*Grupo:* `{TARGET_GROUP_JID[-20:]}`\n"
            f"*Alvo:* {TARGET_NAME}"
        )

    @tg_bot.message_handler(commands=["log"])
    def cmd_log(message):
        if not _checar_chat_id(message): return
        linhas = list(log_buffer)[-15:]
        if not linhas:
            tg_bot.reply_to(message, "📋 Log vazio.")
            return
        texto = "```\n" + "\n".join(linhas) + "\n```"
        # Telegram tem limite de 4096 chars
        if len(texto) > 4000:
            texto = "```\n" + "\n".join(linhas[-8:]) + "\n```"
        tg_bot.reply_to(message, texto)

    @tg_bot.message_handler(commands=["config"])
    def cmd_config(message):
        if not _checar_chat_id(message): return
        tg_bot.reply_to(message,
            f"⚙️ *CONFIGURAÇÕES ATIVAS*\n"
            f"──────────────────\n"
            f"`EVOLUTION_URL` = `{EVOLUTION_URL}`\n"
            f"`INSTANCE_NAME` = `{INSTANCE_NAME}`\n"
            f"`TARGET_NAME`   = `{TARGET_NAME}`\n"
            f"`EMOJI_REPLY`   = {EMOJI_REPLY}\n"
            f"`COOLDOWN`      = {COOLDOWN_MINUTOS} min\n"
            f"`CHECK_DELAY`   = {CHECK_DELAY_SECONDS}s\n"
            f"`MAX_RETRIES`   = {MAX_RETRIES}"
        )

    @tg_bot.message_handler(commands=["cooldown"])
    def cmd_cooldown(message):
        """Força início ou fim do cooldown manualmente."""
        if not _checar_chat_id(message): return
        args = message.text.split()
        if len(args) > 1:
            try:
                minutos = float(args[1])
                with estado_lock:
                    estado["cooldown_ate"] = time.time() + minutos * 60
                tg_bot.reply_to(message, f"⏸️ Cooldown manual definido: *{minutos} min*")
                return
            except ValueError:
                pass
        # Sem argumento = zerar cooldown
        with estado_lock:
            estado["cooldown_ate"] = 0.0
        tg_bot.reply_to(message, "▶️ Cooldown *removido*. Monitorando agora.")

    @tg_bot.message_handler(commands=["ajuda", "help"])
    def cmd_ajuda(message):
        if not _checar_chat_id(message): return
        tg_bot.reply_to(message,
            "🤖 *COMANDOS DISPONÍVEIS*\n"
            "──────────────────\n"
            "/status — situação atual\n"
            "/ligar — ativa o monitoramento\n"
            "/desligar — pausa o monitoramento\n"
            "/log — últimas linhas do log\n"
            "/config — configurações ativas\n"
            "/cooldown [min] — define cooldown manual\n"
            "/cooldown — remove o cooldown\n"
            "/ajuda — esta mensagem"
        )

def _iniciar_telegram_polling():
    """Roda o polling do Telegram em thread dedicada."""
    if not tg_bot:
        log.warning("TELEGRAM_TOKEN não configurado. Controle via Telegram desativado.")
        return
    log.info("📱 Telegram bot iniciado. Aguardando comandos...")
    notificar_telegram(
        f"🤖 *Bot iniciado!*\n"
        f"Monitorando: *{TARGET_NAME}*\n"
        f"Emoji: {EMOJI_REPLY} | Cooldown: {COOLDOWN_MINUTOS}min\n"
        f"Digite /ajuda para ver os comandos."
    )
    tg_bot.infinity_polling(timeout=30, long_polling_timeout=20)

# ─────────────────────────────────────────────────────────────
# FILA + WORKER PRÉ-INICIADO
# ─────────────────────────────────────────────────────────────
fila_mensagens: queue.Queue = queue.Queue()
em_processamento: set[str] = set()
lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
# FLASK
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)


# ═════════════════════════════════════════════════════════════
# FUNÇÕES DE API
# ═════════════════════════════════════════════════════════════

def enviar_emoji_citando(group_jid, msg_id, participant_jid, texto_original):
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
            "message": {"conversation": texto_original}
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
        with estado_lock:
            estado["tentativas"] += 1
        return data
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        log.error(f"  ❌ Erro ao enviar emoji ({ms}ms): {e}")
        return None


def buscar_mensagens_grupo(group_jid, limit=50):
    url = f"{EVOLUTION_URL}/chat/findMessages/{INSTANCE_NAME}"
    payload = {"where": {"key": {"remoteJid": group_jid}}, "limit": limit}
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


def encontrar_citacoes(mensagens, target_msg_id):
    citacoes = []
    for msg in mensagens:
        context_info = msg.get("contextInfo", {})
        if not context_info:
            continue
        if context_info.get("stanzaId", "") == target_msg_id:
            citacoes.append(msg)
    return citacoes


def verificar_se_fomos_primeiro(group_jid, target_msg_id, nosso_msg_id):
    mensagens = buscar_mensagens_grupo(group_jid, limit=50)
    if not mensagens:
        log.warning("  ⚠️  Não foi possível buscar mensagens.")
        return False

    citacoes = encontrar_citacoes(mensagens, target_msg_id)
    if not citacoes:
        log.warning("  ⚠️  Nenhuma citação encontrada ainda.")
        return False

    citacoes.sort(key=lambda x: x.get("messageTimestamp", 0))

    log.info(f"  📋 Citações ({len(citacoes)}):")
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
# VERIFICAÇÃO ASSÍNCRONA + RETRY
# ═════════════════════════════════════════════════════════════

def _verificar_e_retry(group_jid, msg_id, participant_jid, texto, nosso_msg_id, tentativa):
    time.sleep(CHECK_DELAY_SECONDS)

    sucesso = verificar_se_fomos_primeiro(group_jid, msg_id, nosso_msg_id)

    if sucesso:
        log.info("✅ BOT CONCLUÍDO COM SUCESSO! Aguardando próximas mensagens...")
        iniciar_cooldown()   # ⑤ Inicia o cooldown após sucesso
        with lock:
            em_processamento.discard(msg_id)
        return

    with estado_lock:
        estado["falha"] += 1

    if tentativa >= MAX_RETRIES:
        log.warning(f"⚠️  Limite de {MAX_RETRIES} tentativas atingido.")
        notificar_telegram(f"⚠️ Não consegui ser o primeiro após {MAX_RETRIES} tentativas.")
        with lock:
            em_processamento.discard(msg_id)
        return

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

    with lock:
        em_processamento.discard(msg_id)


# ═════════════════════════════════════════════════════════════
# PROCESSADOR PRINCIPAL
# ═════════════════════════════════════════════════════════════

def processar_mensagem_alvo(msg_data):
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

    notificar_telegram(f"🎯 *Mensagem detectada!*\n`{texto[:100]}`\nEnviando emoji...")

    log.info("🔄 Tentativa 1/%d — enviando emoji...", MAX_RETRIES)
    resposta = enviar_emoji_citando(group_jid, msg_id, participant, texto)

    if not resposta:
        log.error("Falha no envio inicial. Abortando.")
        with lock:
            em_processamento.discard(msg_id)
        return

    nosso_msg_id = resposta.get("key", {}).get("id", "")
    if not nosso_msg_id:
        log.error("ID não retornado pela API. Abortando.")
        with lock:
            em_processamento.discard(msg_id)
        return

    t = threading.Thread(
        target=_verificar_e_retry,
        args=(group_jid, msg_id, participant, texto, nosso_msg_id, 1),
        daemon=True
    )
    t.start()


# ═════════════════════════════════════════════════════════════
# WORKER LOOP
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
# WEBHOOK
# ═════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data  = request.json or {}
    event = data.get("event", "")

    if event != "messages.upsert":
        return jsonify({"status": "ignored", "event": event})

    # ── Verificações globais antes de qualquer filtro ──────
    if not estado["ativo"]:
        return jsonify({"status": "paused"})

    if em_cooldown():
        log.info(f"⏸️  Em cooldown. Ignorando mensagem. Restam {cooldown_restante_str()}.")
        return jsonify({"status": "cooldown", "restante": cooldown_restante_str()})

    msgs = data.get("data", [])
    if isinstance(msgs, dict):
        msgs = [msgs]

    for msg_data in msgs:
        key        = msg_data.get("key", {})
        remote_jid = key.get("remoteJid", "")

        if remote_jid != TARGET_GROUP_JID:
            continue

        push_name = (msg_data.get("pushName") or "").strip().lower()
        nome_alvo = TARGET_NAME.strip().lower()
        if not nome_alvo or push_name != nome_alvo:
            continue

        message = msg_data.get("message", {})
        texto = (
            message.get("conversation", "") or
            message.get("extendedTextMessage", {}).get("text", "")
        )
        if not texto:
            continue

        if "@" in texto:
            log.info(f"⏭️  Ignorado (contém '@'): {texto[:50]}...")
            continue

        msg_id = key.get("id", "")
        with lock:
            if msg_id in em_processamento:
                continue
            em_processamento.add(msg_id)

        fila_mensagens.put(msg_data)

    return jsonify({"status": "ok"})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":         "online" if estado["ativo"] else "paused",
        "cooldown":       cooldown_restante_str(),
        "ultima_captura": estado["ultima_captura"].isoformat() if estado["ultima_captura"] else None,
        "sucesso":        estado["sucesso"],
        "falha":          estado["falha"],
        "fila_pendente":  fila_mensagens.qsize()
    })


# ═════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════

if __name__ == "__main__":
    log.info("═" * 60)
    log.info("  🤖 WhatsApp Monitor Bot — TURBO v3")
    log.info("═" * 60)
    log.info(f"  Instância:     {INSTANCE_NAME}")
    log.info(f"  Grupo alvo:    {TARGET_GROUP_JID}")
    log.info(f"  Nome alvo:     {TARGET_NAME}")
    log.info(f"  Emoji:         {EMOJI_REPLY}")
    log.info(f"  Cooldown:      {COOLDOWN_MINUTOS} min após sucesso")
    log.info(f"  Porta:         {WEBHOOK_PORT}")
    log.info(f"  Delay check:   {CHECK_DELAY_SECONDS}s")
    log.info(f"  Max tentativas:{MAX_RETRIES}")
    log.info(f"  Telegram:      {'✅ configurado' if TELEGRAM_TOKEN else '❌ não configurado'}")
    log.info("═" * 60)

    _warmup_connection()

    # Worker de mensagens WhatsApp
    threading.Thread(target=_worker_loop, daemon=True).start()

    # Bot Telegram (polling em thread própria)
    threading.Thread(target=_iniciar_telegram_polling, daemon=True).start()

    log.info(f"  Webhook: http://SEU-IP:{WEBHOOK_PORT}/webhook\n")
    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)