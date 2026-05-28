"""
Bot Telegram — interface de controle do WhatsApp Monitor.

Fluxo geral:
  /start ou /menu → menu principal (inline keyboard)
  ➕ Criar instância → pede nome → cria na Evolution → envia QR
  📱 Minhas instâncias → lista → submenu por instância
  Submenu da instância: ligar/desligar, trocar grupo, nome alvo, emoji, reconectar, remover

A autorização é por chat_id na tabela `admins`. Se a tabela estiver vazia,
qualquer chat_id em ADMIN_CHAT_IDS (env) é injetado no init.
"""

import os
import html
import base64
import logging
import threading
from io import BytesIO
from datetime import datetime

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

import db
import evolution

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").rstrip("/")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML") if TELEGRAM_TOKEN else None

# Defesa em profundidade: força parse_mode=HTML em todos os métodos de envio,
# mesmo que o default do construtor não esteja sendo respeitado por algum
# motivo (cache de imagem, versão antiga do telebot, etc.).
if bot is not None:
    bot.parse_mode = "HTML"
    for _m in ("send_message", "reply_to", "send_photo", "edit_message_text",
              "edit_message_caption"):
        _orig = getattr(bot, _m, None)
        if _orig is None:
            continue
        def _wrap(orig):
            def _f(*a, **kw):
                kw.setdefault("parse_mode", "HTML")
                return orig(*a, **kw)
            return _f
        setattr(bot, _m, _wrap(_orig))


def _h(text) -> str:
    """Escapa conteúdo dinâmico para HTML (<, >, &)."""
    return html.escape("" if text is None else str(text), quote=False)

# Cache em memória para mapeamentos curtos por chat (callbacks têm 64 bytes)
# Estrutura: { chat_id: { "groups": {idx: jid_completo}, "inst_em_config": str } }
session_state: dict[int, dict] = {}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _chat_id_de(message_or_call) -> int:
    return (message_or_call.from_user.id
            if hasattr(message_or_call, "from_user")
            else message_or_call.chat.id)


def _check(message_or_call) -> bool:
    """Permite qualquer usuário cadastrado (admin ou user)."""
    chat_id = _chat_id_de(message_or_call)
    if db.eh_usuario(chat_id):
        return True
    if isinstance(message_or_call, types.CallbackQuery):
        bot.answer_callback_query(message_or_call.id, "⛔ Acesso negado.")
    else:
        bot.reply_to(message_or_call, "⛔ Acesso não autorizado.")
    return False


def _check_admin(message_or_call) -> bool:
    """Bloqueia tudo que não seja role=admin."""
    chat_id = _chat_id_de(message_or_call)
    if db.is_admin(chat_id):
        return True
    if isinstance(message_or_call, types.CallbackQuery):
        bot.answer_callback_query(message_or_call.id, "⛔ Só admin pode fazer isso.")
    else:
        bot.reply_to(message_or_call, "⛔ Só admin pode fazer isso.")
    return False


def _pode_ver(chat_id: int, instance_name: str) -> bool:
    """Admin vê tudo; user só vê o que é dele."""
    if db.is_admin(chat_id):
        return True
    return db.get_owner(instance_name) == chat_id


def _state(chat_id: int) -> dict:
    return session_state.setdefault(chat_id, {})


def _safe_edit(text: str, chat_id: int, message_id: int,
               reply_markup: types.InlineKeyboardMarkup | None = None):
    """edit_message_text que ignora o erro 'message is not modified'."""
    try:
        bot.edit_message_text(text, chat_id, message_id,
                              parse_mode="HTML",
                              reply_markup=reply_markup)
    except ApiTelegramException as e:
        if "message is not modified" in str(e):
            return
        raise


def _safe_answer(call_id: str, text: str = "", show_alert: bool = False):
    """answer_callback_query respeitando o limite de 200 chars."""
    if text and len(text) > 195:
        text = text[:195] + "..."
    try:
        bot.answer_callback_query(call_id, text, show_alert=show_alert)
    except Exception as e:
        log.warning(f"answer_callback_query falhou: {e}")


def notificar(chat_id: int, texto: str):
    if not bot:
        return
    def _send():
        try:
            bot.send_message(chat_id, texto)
        except Exception as e:
            log.warning(f"Telegram send error ({chat_id}): {e}")
    threading.Thread(target=_send, daemon=True).start()


def notificar_admins(texto: str):
    for chat_id in db.listar_admins():
        notificar(chat_id, texto)


def notificar_dono_com_menu_instancia(instance_name: str, prefixo: str = ""):
    """Manda pro DONO da instância o resumo + teclado inline."""
    if not bot:
        return

    owner = db.get_owner(instance_name)
    if not owner:
        log.warning(f"instância '{instance_name}' sem dono — ninguém pra notificar.")
        return

    def _send():
        texto = (f"{prefixo}\n\n" if prefixo else "") + _resumo_instancia(instance_name, owner)
        markup = kb_instancia(instance_name)
        try:
            bot.send_message(owner, texto, reply_markup=markup)
        except Exception as e:
            log.warning(f"Telegram send para dono {owner}: {e}")

    threading.Thread(target=_send, daemon=True).start()


# ─────────────────────────────────────────────────────────────
# Menus (InlineKeyboardMarkup)
# ─────────────────────────────────────────────────────────────

def kb_main(viewer_chat_id: int | None = None) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📱 Instâncias", callback_data="menu:list_inst"),
        types.InlineKeyboardButton("➕ Criar", callback_data="menu:create_inst"),
    )
    if viewer_chat_id is not None and db.is_admin(viewer_chat_id):
        kb.add(
            types.InlineKeyboardButton("📊 Status geral", callback_data="menu:status"),
            types.InlineKeyboardButton("👥 Usuários",     callback_data="menu:users"),
        )
    else:
        kb.add(types.InlineKeyboardButton("📊 Status", callback_data="menu:status"))
    return kb


def kb_lista_instancias(viewer_chat_id: int) -> types.InlineKeyboardMarkup:
    """Admin vê todas (com dono); usuário comum só vê as próprias."""
    kb = types.InlineKeyboardMarkup(row_width=1)
    admin = db.is_admin(viewer_chat_id)
    instancias = db.listar_instancias(owner_chat_id=None if admin else viewer_chat_id)
    for inst in instancias:
        marca = "🟢" if inst["ativo"] else "🔴"
        rotulo = f"{marca} {inst['name']}"
        if admin and inst.get("owner_chat_id") and inst["owner_chat_id"] != viewer_chat_id:
            dono = db.get_usuario(inst["owner_chat_id"]) or {}
            dono_nome = dono.get("name") or str(inst["owner_chat_id"])
            rotulo += f"  · {dono_nome}"
        kb.add(types.InlineKeyboardButton(
            rotulo, callback_data=f"inst:open:{inst['name']}"
        ))
    kb.add(types.InlineKeyboardButton("🔙 Voltar", callback_data="menu:main"))
    return kb


def kb_instancia(name: str) -> types.InlineKeyboardMarkup:
    inst = db.get_instancia(name)
    kb = types.InlineKeyboardMarkup(row_width=2)
    if inst:
        toggle_label = "⏸️ Desligar" if inst["ativo"] else "▶️ Ligar"
        kb.add(
            types.InlineKeyboardButton(toggle_label, callback_data=f"inst:toggle:{name}"),
            types.InlineKeyboardButton("📊 Status",   callback_data=f"inst:status:{name}"),
        )
        kb.add(
            types.InlineKeyboardButton("👥 Trocar grupo", callback_data=f"inst:groups:{name}"),
            types.InlineKeyboardButton("👤 Nomes alvo",   callback_data=f"inst:name:{name}"),
        )
        kb.add(
            types.InlineKeyboardButton("😀 Emoji",      callback_data=f"inst:emoji:{name}"),
            types.InlineKeyboardButton("🔄 QR Code",    callback_data=f"inst:qr:{name}"),
        )
        kb.add(types.InlineKeyboardButton("🗑️ Remover", callback_data=f"inst:del:{name}"))
    kb.add(types.InlineKeyboardButton("🔙 Voltar", callback_data="menu:list_inst"))
    return kb


def _resumo_instancia(name: str, viewer_chat_id: int | None = None) -> str:
    inst = db.get_instancia(name)
    if not inst:
        return f"❌ Instância <code>{_h(name)}</code> não existe."
    s = db.get_stats(name)
    estado = "🟢 ATIVA" if inst["ativo"] else "🔴 PARADA"
    captura = _h(s["ultima_captura"] or "nenhuma")
    nomes = db.parse_target_names(inst["target_name"])
    if not nomes:
        alvos_str = "—"
    elif len(nomes) <= 3:
        alvos_str = _h(", ".join(nomes))
    else:
        alvos_str = f"{_h(', '.join(nomes[:3]))}  <i>(+{len(nomes) - 3})</i>"

    linha_dono = ""
    if viewer_chat_id and db.is_admin(viewer_chat_id):
        owner = inst.get("owner_chat_id")
        if owner and owner != viewer_chat_id:
            dono = db.get_usuario(owner) or {}
            nome_dono = dono.get("name") or str(owner)
            linha_dono = f"<b>Dono:</b> <code>{_h(nome_dono)}</code>\n"

    grupo  = (inst['target_group_jid'] or '—')[-25:]
    emoji  = _h(inst['emoji'])
    return (
        f"📱 <b>{_h(name)}</b> — {estado}\n"
        f"──────────────────\n"
        f"{linha_dono}"
        f"<b>Grupo:</b> <code>{_h(grupo)}</code>\n"
        f"<b>Alvos ({len(nomes)}):</b> {alvos_str}\n"
        f"<b>Emoji:</b> {emoji}\n"
        f"<b>Sucessos:</b> {s['sucesso']} | <b>Falhas:</b> {s['falha']}\n"
        f"<b>Última captura:</b> {captura}"
    )


# ─────────────────────────────────────────────────────────────
# Comandos / handlers
# ─────────────────────────────────────────────────────────────

def _registrar_handlers():
    if not bot:
        return

    @bot.message_handler(commands=["start", "menu"])
    def cmd_start(message):
        if not _check(message): return
        bot.send_message(
            message.chat.id,
            "🤖 <b>WhatsApp Monitor Bot</b>\nEscolha uma opção:",
            reply_markup=kb_main(message.from_user.id)
        )

    @bot.message_handler(commands=["meuid"])
    def cmd_meuid(message):
        bot.reply_to(message, f"Seu chat_id: <code>{message.from_user.id}</code>")

    @bot.message_handler(commands=["ajuda", "help"])
    def cmd_ajuda(message):
        if not _check(message): return
        admin = db.is_admin(message.from_user.id)
        texto = (
            "<b>Comandos:</b>\n"
            "/menu — abrir menu principal\n"
            "/meuid — ver seu chat_id\n"
        )
        if admin:
            texto += (
                "\n<b>Admin:</b>\n"
                "/users — listar usuários\n"
                "/user_add [chat_id] [nome] — cria um user comum\n"
                "/user_rm [chat_id] — remove um user comum\n"
                "\n<i>Admins são gerenciados apenas via ADMIN_CHAT_IDS no .env.</i>"
            )
        bot.reply_to(message, texto)

    # ── Gestão de usuários (admin only) ───────────────────────

    @bot.message_handler(commands=["users"])
    def cmd_users(message):
        if not _check_admin(message): return
        usuarios = db.listar_usuarios()
        if not usuarios:
            bot.reply_to(message, "Nenhum usuário cadastrado.")
            return
        linhas = []
        for u in usuarios:
            badge = "👑" if u["role"] == "admin" else "👤"
            nome  = _h(u.get("name") or "—")
            n_inst = db.contar_instancias_do_dono(u["chat_id"])
            linhas.append(f"{badge} <code>{u['chat_id']}</code> · {nome} · {n_inst} inst.")
        bot.reply_to(message, "👥 <b>Usuários</b>\n" + "\n".join(linhas))

    @bot.message_handler(commands=["user_add"])
    def cmd_user_add(message):
        if not _check_admin(message): return
        parts = message.text.split(maxsplit=2)
        if len(parts) < 2:
            bot.reply_to(message, "Uso: /user_add [chat_id] [nome]")
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            bot.reply_to(message, "chat_id inválido.")
            return
        if db.is_admin(chat_id):
            bot.reply_to(message,
                "⚠️ Esse chat_id já é admin (.env). Remova do "
                "<code>ADMIN_CHAT_IDS</code> se quiser virar user comum.")
            return
        nome = parts[2] if len(parts) > 2 else None
        db.adicionar_usuario(chat_id, role="user", name=nome)
        bot.reply_to(message, f"✅ User <code>{chat_id}</code> cadastrado.")

    @bot.message_handler(commands=["user_rm"])
    def cmd_user_rm(message):
        if not _check_admin(message): return
        parts = message.text.split()
        if len(parts) < 2:
            bot.reply_to(message, "Uso: /user_rm [chat_id]")
            return
        try:
            chat_id = int(parts[1])
        except ValueError:
            bot.reply_to(message, "chat_id inválido.")
            return
        if db.is_admin(chat_id):
            bot.reply_to(message,
                "⚠️ Não posso remover um admin. "
                "Tire o chat_id de <code>ADMIN_CHAT_IDS</code> no .env e reinicie o bot.")
            return
        db.remover_usuario(chat_id)
        bot.reply_to(message, f"✅ User <code>{chat_id}</code> removido.")

    # ── Callbacks (inline keyboard) ───────────────────────────

    @bot.callback_query_handler(func=lambda c: True)
    def cb_router(call: types.CallbackQuery):
        if not _check(call): return
        data = call.data
        chat_id = call.message.chat.id
        user_chat_id = call.from_user.id   # quem está clicando
        admin = db.is_admin(user_chat_id)

        # Guarda de propriedade para callbacks que tocam instância
        if data.startswith("inst:"):
            partes = data.split(":")
            if len(partes) >= 3:
                inst_alvo = partes[2]
                if inst_alvo and not _pode_ver(user_chat_id, inst_alvo):
                    _safe_answer(call.id, "⛔ Essa instância não é sua.", show_alert=True)
                    return

        try:
            if data == "menu:main":
                _safe_edit("🤖 <b>WhatsApp Monitor Bot</b>\nEscolha uma opção:",
                                      chat_id, call.message.message_id,
                                      reply_markup=kb_main(user_chat_id))

            elif data == "menu:list_inst":
                instancias = db.listar_instancias(None if admin else user_chat_id)
                texto = "📱 <b>Instâncias cadastradas:</b>" if instancias else "Nenhuma instância ainda."
                _safe_edit(texto, chat_id, call.message.message_id,
                                      reply_markup=kb_lista_instancias(user_chat_id))

            elif data == "menu:create_inst":
                if not admin and db.contar_instancias_do_dono(user_chat_id) >= 1:
                    _safe_answer(call.id,
                        "Você já tem 1 instância. Remova-a antes de criar outra.",
                        show_alert=True)
                    return
                bot.send_message(chat_id, "Digite o <b>nome</b> da nova instância (sem espaços):")
                bot.register_next_step_handler(call.message, _passo_criar_instancia)

            elif data == "menu:status":
                instancias = db.listar_instancias(None if admin else user_chat_id)
                if not instancias:
                    texto = "Nenhuma instância cadastrada."
                else:
                    linhas = []
                    for inst in instancias:
                        s = db.get_stats(inst["name"])
                        emoji = "🟢" if inst["ativo"] else "🔴"
                        linhas.append(
                            f"{emoji} <b>{_h(inst['name'])}</b> — "
                            f"✅ {s['sucesso']} | ❌ {s['falha']}"
                        )
                    texto = "📊 <b>Status</b>\n──────────────────\n" + "\n".join(linhas)
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("🔙 Voltar", callback_data="menu:main"))
                _safe_edit(texto, chat_id, call.message.message_id, reply_markup=kb)

            elif data == "menu:users":
                if not _check_admin(call):
                    return
                usuarios = db.listar_usuarios()
                linhas = []
                for u in usuarios:
                    badge = "👑" if u["role"] == "admin" else "👤"
                    nome  = _h(u.get("name") or "—")
                    linhas.append(f"{badge} <code>{u['chat_id']}</code> · {nome}")
                texto = "👥 <b>Usuários</b>\n──────────────────\n" + "\n".join(linhas)
                texto += (
                    "\n\nComandos:\n"
                    "/user_add [chat_id] [nome]\n"
                    "/user_rm [chat_id]\n"
                    "\n<i>Admins: só via ADMIN_CHAT_IDS no .env.</i>"
                )
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("🔙 Voltar", callback_data="menu:main"))
                _safe_edit(texto, chat_id, call.message.message_id, reply_markup=kb)

            elif data.startswith("inst:open:"):
                name = data.split(":", 2)[2]
                _safe_edit(_resumo_instancia(name, user_chat_id), chat_id, call.message.message_id,
                                      reply_markup=kb_instancia(name))

            elif data.startswith("inst:status:"):
                name = data.split(":", 2)[2]
                _safe_edit(_resumo_instancia(name, user_chat_id), chat_id, call.message.message_id,
                                      reply_markup=kb_instancia(name))

            elif data.startswith("inst:toggle:"):
                name = data.split(":", 2)[2]
                inst = db.get_instancia(name)
                if not inst:
                    bot.answer_callback_query(call.id, "Instância não encontrada.")
                    return
                # Não deixa ligar sem grupo e ao menos um alvo configurados
                if not inst["ativo"] and (not inst["target_group_jid"]
                                         or not db.get_target_names(name)):
                    bot.answer_callback_query(call.id,
                        "Configure grupo e pelo menos 1 nome alvo antes de ligar.",
                        show_alert=True)
                    return
                db.set_ativo(name, not inst["ativo"])
                bot.answer_callback_query(call.id,
                    "Ligado!" if not inst["ativo"] else "Desligado.")
                _safe_edit(_resumo_instancia(name, user_chat_id), chat_id, call.message.message_id,
                                      reply_markup=kb_instancia(name))

            elif data.startswith("inst:groups:"):
                name = data.split(":", 2)[2]
                _listar_grupos_para_escolha(chat_id, call.message.message_id, name)

            elif data.startswith("inst:setgroup:"):
                _, _, name, idx = data.split(":", 3)
                jid = _state(chat_id).get("groups", {}).get(int(idx))
                if not jid:
                    bot.answer_callback_query(call.id, "Grupo expirou. Liste de novo.", show_alert=True)
                    return
                db.atualizar_instancia(name, target_group_jid=jid)
                bot.answer_callback_query(call.id, "Grupo atualizado.")
                _safe_edit(_resumo_instancia(name, user_chat_id), chat_id, call.message.message_id,
                                      reply_markup=kb_instancia(name))

            elif data.startswith("inst:name:"):
                name = data.split(":", 2)[2]
                _listar_participantes_para_escolha(chat_id, call.message.message_id, name, page=0)

            elif data.startswith("inst:namepg:"):
                _, _, name, page = data.split(":", 3)
                _renderizar_pagina_participantes(chat_id, call.message.message_id,
                                                  name, page=int(page))

            elif data.startswith("inst:setname:"):
                _, _, name, idx = data.split(":", 3)
                push = _state(chat_id).get("participants", {}).get(int(idx))
                if not push:
                    bot.answer_callback_query(call.id, "Lista expirou. Atualize.", show_alert=True)
                    return
                atual = set(db.get_target_names(name))
                if push in atual:
                    atual.discard(push)
                    aviso = f"➖ Removido: {push}"
                else:
                    atual.add(push)
                    aviso = f"➕ Adicionado: {push}"
                db.set_target_names(name, sorted(atual))
                _safe_answer(call.id, aviso)
                pagina = _state(chat_id).get("name_page", 0)
                _renderizar_pagina_participantes(chat_id, call.message.message_id, name, pagina)

            elif data.startswith("inst:clearnames:"):
                name = data.split(":", 2)[2]
                db.set_target_names(name, [])
                _safe_answer(call.id, "Todos os alvos removidos.")
                pagina = _state(chat_id).get("name_page", 0)
                _renderizar_pagina_participantes(chat_id, call.message.message_id, name, pagina)

            elif data.startswith("inst:donename:"):
                name = data.split(":", 2)[2]
                _safe_edit(_resumo_instancia(name, user_chat_id), chat_id, call.message.message_id,
                                      reply_markup=kb_instancia(name))

            elif data.startswith("inst:emoji:"):
                name = data.split(":", 2)[2]
                _state(chat_id)["inst_em_config"] = name
                bot.send_message(chat_id,
                    f"Digite o <b>novo emoji</b> para <code>{_h(name)}</code>:")
                bot.register_next_step_handler(call.message, _passo_set_emoji)

            elif data.startswith("inst:qr:"):
                name = data.split(":", 2)[2]
                _enviar_qr(chat_id, name)

            elif data.startswith("inst:del:"):
                name = data.split(":", 2)[2]
                kb = types.InlineKeyboardMarkup(row_width=2)
                kb.add(
                    types.InlineKeyboardButton("⚠️ SIM, remover", callback_data=f"inst:del_ok:{name}"),
                    types.InlineKeyboardButton("Cancelar",        callback_data=f"inst:open:{name}"),
                )
                _safe_edit(
                    f"⚠️ Remover instância <code>{_h(name)}</code> da Evolution e do banco?",
                    chat_id, call.message.message_id, reply_markup=kb
                )

            elif data.startswith("inst:del_ok:"):
                name = data.split(":", 2)[2]
                try:
                    evolution.deletar_instancia(name)
                except Exception as e:
                    log.warning(f"Falha ao deletar na Evolution: {e}")
                db.remover_instancia(name)
                _safe_edit(f"🗑️ Instância <code>{_h(name)}</code> removida.",
                                      chat_id, call.message.message_id,
                                      reply_markup=kb_main())

        except Exception as e:
            log.exception("Erro no callback")
            _safe_answer(call.id, f"Erro: {e}", show_alert=True)


# ─────────────────────────────────────────────────────────────
# Fluxos com next_step_handler
# ─────────────────────────────────────────────────────────────

def _passo_criar_instancia(message):
    if not _check(message): return
    user_chat_id = message.from_user.id
    admin = db.is_admin(user_chat_id)

    # Limite para usuários comuns
    if not admin and db.contar_instancias_do_dono(user_chat_id) >= 1:
        bot.reply_to(message,
            "Você já tem 1 instância. Remova-a no menu antes de criar outra.")
        return

    nome = (message.text or "").strip()
    if not nome or " " in nome:
        bot.reply_to(message, "Nome inválido. Use letras/números/underscore, sem espaços.")
        return

    if db.get_instancia(nome):
        bot.reply_to(message, f"Já existe uma instância com nome <code>{_h(nome)}</code>.")
        return

    bot.reply_to(message, f"⏳ Criando instância <code>{_h(nome)}</code> na Evolution...")
    try:
        evolution.criar_instancia(nome)
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Erro ao criar: <code>{_h(e)}</code>")
        return

    db.criar_instancia(nome, owner_chat_id=user_chat_id)

    # Configura webhook ANTES do QR — sem ele a Evolution não nos chama.
    ok, msg = _configurar_webhook_evolution(nome)
    if ok:
        bot.send_message(message.chat.id, f"🔗 {_h(msg)}")
    else:
        bot.send_message(message.chat.id,
            f"⚠️ <b>Webhook não foi configurado:</b> {_h(msg)}")

    _enviar_qr(message.chat.id, nome)
    bot.send_message(message.chat.id,
        f"✅ Instância <code>{_h(nome)}</code> criada.\n"
        f"Agora abra o menu da instância para configurar o <b>grupo</b> e o <b>nome alvo</b>.",
        reply_markup=kb_instancia(nome))
    log.info(f"📦 instância '{nome}' criada por chat_id={user_chat_id}")


def _passo_set_emoji(message):
    if not _check(message): return
    nome_inst = _state(message.from_user.id).get("inst_em_config")
    if not nome_inst:
        bot.reply_to(message, "Sessão expirada. Use /menu.")
        return
    valor = (message.text or "").strip()
    if not valor:
        bot.reply_to(message, "Emoji vazio.")
        return
    db.atualizar_instancia(nome_inst, emoji=valor)
    bot.reply_to(message, f"✅ Emoji atualizado: {_h(valor)}",
                 reply_markup=kb_instancia(nome_inst))


# ─────────────────────────────────────────────────────────────
# Ações pesadas
# ─────────────────────────────────────────────────────────────

def _configurar_webhook_evolution(instance_name: str) -> tuple[bool, str]:
    """Aponta a Evolution para o nosso /webhook desta instância."""
    if not WEBHOOK_URL:
        return False, "WEBHOOK_URL não está no .env. Sem isso a Evolution não nos chama."
    base = WEBHOOK_URL.rstrip("/")
    # Aceita .env com ou sem /webhook no final
    if base.endswith("/webhook"):
        base = base[: -len("/webhook")]
    url = f"{base}/webhook"
    try:
        evolution.configurar_webhook(instance_name, url, events=["MESSAGES_UPSERT"])
    except Exception as e:
        log.exception("Erro ao configurar webhook")
        return False, f"Falhou: {e}"
    log.info(f"🔗 webhook de '{instance_name}' apontado para {url}")
    return True, f"Webhook configurado: {url}"


def _enviar_qr(chat_id: int, instance_name: str):
    """Conecta a instância e envia o QR Code como foto."""
    bot.send_message(chat_id,
        f"⏳ Gerando QR Code para <code>{_h(instance_name)}</code>...")
    try:
        resp = evolution.conectar_instancia(instance_name)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Erro ao conectar: <code>{_h(e)}</code>")
        return

    b64 = resp.get("base64") or resp.get("data", {}).get("base64") or resp.get("qrcode", {}).get("base64")
    if not b64:
        bot.send_message(chat_id,
            "⚠️ Resposta sem QR. A instância pode já estar conectada.\n"
            f"<pre>{_h(str(resp)[:300])}</pre>")
        return

    if "," in b64:
        b64 = b64.split(",", 1)[1]
    try:
        img = base64.b64decode(b64)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Base64 inválido: {_h(e)}")
        return

    bot.send_photo(
        chat_id, BytesIO(img),
        caption=(f"📱 <b>QR Code — <code>{_h(instance_name)}</code></b>\n\n"
                 f"Escaneie no WhatsApp em até 60s.\n"
                 f"⚠️ Após conectar, configure o grupo e o nome alvo no menu.")
    )


PAGE_SIZE = 10


def _listar_participantes_para_escolha(chat_id: int, message_id: int,
                                       instance_name: str, page: int = 0):
    """
    Coleta a lista (Evolution) e armazena no session_state. Depois renderiza
    a primeira página. As páginas seguintes reaproveitam o cache.
    """
    inst = db.get_instancia(instance_name)
    if not inst:
        _safe_edit("Instância não encontrada.", chat_id, message_id, reply_markup=kb_main())
        return
    if not inst["target_group_jid"]:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("👥 Configurar grupo", callback_data=f"inst:groups:{instance_name}"))
        kb.add(types.InlineKeyboardButton("🔙 Voltar", callback_data=f"inst:open:{instance_name}"))
        _safe_edit(
            "⚠️ Configure primeiro o *grupo alvo* — só depois eu consigo listar as pessoas.",
            chat_id, message_id, reply_markup=kb
        )
        return

    participantes = _coletar_alvos_possiveis(instance_name, inst["target_group_jid"])
    com_nome = [p for p in participantes if p["pushName"]]

    if not com_nome:
        _safe_edit(
            f"⚠️ O grupo tem {len(participantes)} pessoa(s), mas nenhuma com "
            f"`pushName` conhecido ainda.\n"
            f"Peça para alguém enviar uma mensagem no grupo e tente de novo.",
            chat_id, message_id, reply_markup=kb_instancia(instance_name)
        )
        return

    # Cache: idx → pushName (idx é absoluto, não relativo à página)
    mapa = {i: p["pushName"] for i, p in enumerate(com_nome)}
    _state(chat_id)["participants"]   = mapa
    _state(chat_id)["participants_n"] = len(com_nome)

    _renderizar_pagina_participantes(chat_id, message_id, instance_name, page)


def _renderizar_pagina_participantes(chat_id: int, message_id: int,
                                     instance_name: str, page: int):
    mapa = _state(chat_id).get("participants") or {}
    total = _state(chat_id).get("participants_n", 0)
    if not mapa:
        _listar_participantes_para_escolha(chat_id, message_id, instance_name, page=0)
        return

    total_paginas = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_paginas - 1))
    _state(chat_id)["name_page"] = page

    inicio = page * PAGE_SIZE
    fim    = min(inicio + PAGE_SIZE, total)

    selecionados = set(db.get_target_names(instance_name))

    kb = types.InlineKeyboardMarkup(row_width=1)
    for i in range(inicio, fim):
        push = mapa[i]
        marca = "✅ " if push in selecionados else "▫️ "
        kb.add(types.InlineKeyboardButton(
            f"{marca}{push[:38]}",
            callback_data=f"inst:setname:{instance_name}:{i}"
        ))

    # Navegação
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(
            "◀️", callback_data=f"inst:namepg:{instance_name}:{page - 1}"))
    nav.append(types.InlineKeyboardButton(
        f"{page + 1}/{total_paginas}", callback_data=f"inst:namepg:{instance_name}:{page}"))
    if page < total_paginas - 1:
        nav.append(types.InlineKeyboardButton(
            "▶️", callback_data=f"inst:namepg:{instance_name}:{page + 1}"))
    kb.row(*nav)

    kb.row(
        types.InlineKeyboardButton("🔄 Atualizar", callback_data=f"inst:name:{instance_name}"),
        types.InlineKeyboardButton("🗑️ Limpar",    callback_data=f"inst:clearnames:{instance_name}"),
    )
    kb.add(types.InlineKeyboardButton("✅ Concluir", callback_data=f"inst:donename:{instance_name}"))

    _safe_edit(
        f"👤 <b>Toque para marcar/desmarcar</b> os alvos de "
        f"<code>{_h(instance_name)}</code>:\n"
        f"<i>{len(selecionados)} selecionado(s) · {total} pessoas no grupo · "
        f"mostrando {inicio + 1}-{fim}.</i>",
        chat_id, message_id, reply_markup=kb
    )


def _coletar_alvos_possiveis(instance_name: str, group_jid: str) -> list[dict]:
    """
    Lista APENAS participantes do grupo configurado. A composição do grupo
    é a fonte de verdade — contatos avulsos e mensagens fora do grupo são
    ignorados.

    Para cada participante, tenta resolver o pushName em duas fontes:
      a) mensagens recentes desse grupo (mais confiável — é o pushName que
         chega no webhook);
      b) /chat/findContacts (fallback para quem nunca enviou no grupo).

    Retorna [{"pushName": str, "label": str, "jid": str}, ...]
    """
    # ── 1. Composição do grupo (fonte de verdade) ─────────
    try:
        grupo = evolution.listar_participantes_grupo(instance_name, group_jid)
    except Exception as e:
        log.warning(f"listar_participantes_grupo falhou: {e}")
        grupo = []

    # JIDs que estão no grupo
    grupo_jids = {(gp.get("id") or gp.get("remoteJid") or "") for gp in grupo}
    grupo_jids.discard("")

    # ── 2. pushNames vistos em mensagens DESTE grupo ───────
    msg_push: dict[str, str] = {}    # jid -> pushName
    try:
        for p in evolution.listar_participantes_por_mensagens(instance_name, group_jid, limit=300):
            jid  = p.get("participant") or ""
            push = (p.get("pushName") or "").strip()
            if jid and push and jid not in msg_push:
                msg_push[jid] = push
    except Exception as e:
        log.warning(f"listar_participantes_por_mensagens falhou: {e}")

    # ── 3. Contatos (apenas como dicionário de consulta) ───
    contatos_push: dict[str, str] = {}
    try:
        for c in evolution.listar_contatos(instance_name):
            jid  = c.get("id") or c.get("remoteJid") or ""
            nome = (c.get("pushName") or c.get("name") or "").strip()
            if jid and nome:
                contatos_push[jid] = nome
    except Exception as e:
        log.warning(f"findContacts falhou: {e}")

    # ── 4. Se /group/participants não respondeu, cai pra
    #       msgs do grupo como aproximação ────────────────
    if not grupo_jids:
        log.info("Composição do grupo vazia — usando apenas pushNames das mensagens.")
        grupo_jids = set(msg_push.keys())

    # ── 5. Monta a lista final restrita ao grupo ──────────
    resultado: list[dict] = []
    for jid in grupo_jids:
        push = msg_push.get(jid) or contatos_push.get(jid) or ""
        label = push or (jid.split("@")[0] or "(sem nome)")
        resultado.append({"pushName": push, "label": label, "jid": jid})

    # com pushName primeiro, depois por ordem alfabética
    resultado.sort(key=lambda x: (not x["pushName"], x["label"].lower()))
    return resultado


def _listar_grupos_para_escolha(chat_id: int, message_id: int, instance_name: str):
    try:
        grupos = evolution.listar_grupos(instance_name, get_participants=False)
    except Exception as e:
        _safe_edit(f"❌ Erro ao listar grupos: <code>{_h(e)}</code>",
                   chat_id, message_id, reply_markup=kb_instancia(instance_name))
        return

    if not grupos:
        bot.edit_message_text("Nenhum grupo encontrado.", chat_id, message_id,
                              reply_markup=kb_instancia(instance_name))
        return

    # Cache local: idx → jid (callbacks têm limite de 64 bytes)
    mapa = {}
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, g in enumerate(grupos):
        jid = g.get("id") or g.get("remoteJid") or ""
        nome = g.get("subject") or g.get("name") or jid[:25]
        if not jid:
            continue
        mapa[i] = jid
        label = nome[:50]
        kb.add(types.InlineKeyboardButton(
            label,
            callback_data=f"inst:setgroup:{instance_name}:{i}"
        ))
    _state(chat_id)["groups"] = mapa
    kb.add(types.InlineKeyboardButton("🔙 Voltar", callback_data=f"inst:open:{instance_name}"))

    _safe_edit(
        f"👥 <b>Selecione o grupo alvo</b> para <code>{_h(instance_name)}</code>:",
        chat_id, message_id, reply_markup=kb
    )


# ─────────────────────────────────────────────────────────────
# Inicialização (chamado pelo bot-prd.py)
# ─────────────────────────────────────────────────────────────

def iniciar_polling():
    if not bot:
        log.warning("TELEGRAM_TOKEN não configurado — Telegram desativado.")
        return
    _registrar_handlers()
    log.info("📱 Telegram bot iniciado. Polling ativo.")
    notificar_admins("🤖 <b>Bot iniciado!</b> Use /menu para começar.")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)
