"""
Wrapper HTTP da EvolutionAPI.

Centraliza chamadas e mantém uma `requests.Session` única para
reaproveitar conexões TCP/TLS (mais rápido).
"""

import os
import logging
import requests

log = logging.getLogger(__name__)

EVOLUTION_URL     = os.getenv("EVOLUTION_URL",     "http://localhost:8080").rstrip("/")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")

session = requests.Session()
session.headers.update({
    "apikey": EVOLUTION_API_KEY,
    "Content-Type": "application/json",
})


class EvolutionError(Exception):
    pass


def _req(method: str, path: str, **kwargs) -> dict:
    url = f"{EVOLUTION_URL}{path}"
    try:
        resp = session.request(method, url, timeout=kwargs.pop("timeout", 15), **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.text else {}
    except requests.HTTPError as e:
        body = e.response.text[:300] if e.response is not None else ""
        raise EvolutionError(f"{e} | body: {body}") from e
    except Exception as e:
        raise EvolutionError(str(e)) from e


def warmup():
    try:
        _req("GET", "/instance/fetchInstances", timeout=5)
        log.info("🔌 EvolutionAPI conectada (warmup ok).")
    except Exception:
        log.warning("⚠️ Warmup falhou — talvez a Evolution esteja offline.")


# ── Instâncias ──────────────────────────────────────────────

def criar_instancia(instance_name: str, token: str | None = None) -> dict:
    """
    Cria uma nova instância. Equivalente ao nó 'Criar instancia' do n8n.
    Retorna o JSON com {instance, hash, ...}.
    """
    payload = {
        "instanceName": instance_name,
        "qrcode":       True,
        "integration":  "WHATSAPP-BAILEYS",
        "rejectCall":   True,
    }
    if token:
        payload["token"] = token
    return _req("POST", "/instance/create", json=payload)


def conectar_instancia(instance_name: str) -> dict:
    """
    Obtém o QR Code (data.base64) para conectar a instância.
    Equivalente ao nó 'Conectar instancia' do n8n.
    """
    return _req("GET", f"/instance/connect/{instance_name}")


def deletar_instancia(instance_name: str) -> dict:
    return _req("DELETE", f"/instance/delete/{instance_name}")


def status_instancia(instance_name: str) -> dict:
    return _req("GET", f"/instance/connectionState/{instance_name}")


def logout_instancia(instance_name: str) -> dict:
    return _req("DELETE", f"/instance/logout/{instance_name}")


def configurar_webhook(instance_name: str, url: str,
                       events: list[str] | None = None) -> dict:
    """
    Configura o webhook da instância para apontar para o nosso bot.
    Tenta primeiro o formato Evolution v2 (aninhado em "webhook") e
    cai para o formato flat caso o servidor reclame.
    """
    eventos = events or ["MESSAGES_UPSERT"]
    body_v2 = {
        "webhook": {
            "enabled":          True,
            "url":              url,
            "events":           eventos,
            "webhookByEvents":  False,
            "webhookBase64":    False,
        }
    }
    try:
        return _req("POST", f"/webhook/set/{instance_name}", json=body_v2)
    except EvolutionError as e:
        log.warning(f"webhook v2 falhou ({e}); tentando formato flat.")
        body_flat = {
            "enabled":         True,
            "url":             url,
            "events":          eventos,
            "webhookByEvents": False,
            "webhookBase64":   False,
        }
        return _req("POST", f"/webhook/set/{instance_name}", json=body_flat)


def buscar_webhook(instance_name: str) -> dict:
    try:
        return _req("GET", f"/webhook/find/{instance_name}")
    except Exception as e:
        log.warning(f"buscar_webhook falhou: {e}")
        return {}


def listar_instancias() -> list[dict]:
    data = _req("GET", "/instance/fetchInstances")
    if isinstance(data, list):
        return data
    return data.get("instances", [])


# ── Grupos ──────────────────────────────────────────────────

def listar_grupos(instance_name: str, get_participants: bool = False) -> list[dict]:
    """
    GET /group/fetchAllGroups/{instance}?getParticipants=false
    """
    params = {"getParticipants": "true" if get_participants else "false"}
    data = _req("GET", f"/group/fetchAllGroups/{instance_name}", params=params)
    if isinstance(data, list):
        return data
    return data.get("groups", data.get("data", []))


# ── Mensagens ───────────────────────────────────────────────

def enviar_texto(instance_name: str, number: str, text: str, quoted: dict | None = None) -> dict:
    payload = {"number": number, "text": text}
    if quoted:
        payload["quoted"] = quoted
    return _req("POST", f"/message/sendText/{instance_name}", json=payload, timeout=10)


def enviar_emoji_citando(
    instance_name: str,
    group_jid: str,
    msg_id: str,
    participant_jid: str,
    texto_original: str,
    emoji: str,
) -> dict:
    quoted = {
        "key": {
            "remoteJid":   group_jid,
            "fromMe":      False,
            "id":          msg_id,
            "participant": participant_jid,
        },
        "message": {"conversation": texto_original},
    }
    return enviar_texto(instance_name, group_jid, emoji, quoted=quoted)


def listar_participantes_grupo(instance_name: str, group_jid: str) -> list[dict]:
    """
    GET /group/participants/{instance}?groupJid={jid}
    Retorna [{"id": "<jid>", "admin": null|"admin"|"superadmin"}, ...]
    """
    data = _req("GET", f"/group/participants/{instance_name}",
                params={"groupJid": group_jid})
    if isinstance(data, list):
        return data
    return data.get("participants", []) or data.get("data", [])


def listar_contatos(instance_name: str) -> list[dict]:
    """
    POST /chat/findContacts/{instance}
    Retorna [{"id": "<jid>", "pushName": "...", ...}, ...]
    """
    data = _req("POST", f"/chat/findContacts/{instance_name}", json={"where": {}})
    if isinstance(data, list):
        return data
    return data.get("contacts", []) or data.get("data", [])


def listar_participantes_por_mensagens(instance_name: str, group_jid: str,
                                       limit: int = 300) -> list[dict]:
    """
    Coleta participantes do grupo através do histórico de mensagens.

    Retorna [{"pushName": str, "participant": jid}, ...] deduplicado por pushName.
    É a forma mais confiável de obter o pushName *exatamente como o webhook recebe*.
    """
    msgs = buscar_mensagens(instance_name, group_jid, limit=limit)
    seen: dict[str, str] = {}
    for m in msgs:
        push = (m.get("pushName") or "").strip()
        if not push or push in seen:
            continue
        jid = m.get("key", {}).get("participant") or ""
        seen[push] = jid
    return [{"pushName": k, "participant": v} for k, v in seen.items()]


def buscar_mensagens(instance_name: str, group_jid: str, limit: int = 50) -> list[dict]:
    payload = {"where": {"key": {"remoteJid": group_jid}}, "limit": limit}
    data = _req("POST", f"/chat/findMessages/{instance_name}", json=payload, timeout=10)
    records = data.get("messages", {}).get("records", [])
    if not records:
        records = data if isinstance(data, list) else data.get("records", [])
    return records
