# 🤖 WhatsApp Monitor Bot — EvolutionAPI

Bot que monitora um grupo do WhatsApp, responde com emoji citando mensagens de um número específico, e verifica se foi o primeiro a citar.

---

## 📋 PRÉ-REQUISITOS

- Python 3.11 ou superior
- EvolutionAPI instalado e rodando
- Instância do WhatsApp conectada no EvolutionAPI
- Porta do bot acessível pela EvolutionAPI (mesma máquina = sem problema)

---

## 🚀 INSTALAÇÃO (passo a passo)

### 1. Instalar as dependências Python

Abra o terminal na pasta do projeto e execute:

```bash
pip install -r requirements.txt
```

---

### 2. Descobrir o JID do grupo

Você precisa do **JID** do grupo (formato: `120363XXXXXXXXXX@g.us`).

**Método rápido via API:**

Abra o navegador e acesse:
```
http://SEU-IP:8080/group/fetchAllGroups/NOME-DA-INSTANCIA?getParticipants=false
```
> Substitua `SEU-IP` e `NOME-DA-INSTANCIA` pelos seus dados.

Procure o grupo pelo nome no JSON retornado e copie o campo `"id"`.

---

### 3. Configurar o arquivo `.env`

Abra o arquivo `.env` com um editor de texto (Bloco de Notas, VS Code, etc.) e preencha:

```env
EVOLUTION_URL=http://localhost:8080        # URL do seu EvolutionAPI
EVOLUTION_API_KEY=sua-chave-aqui          # API Key do painel do Evolution
INSTANCE_NAME=minha-instancia             # Nome da instância WhatsApp
TARGET_GROUP_JID=120363000000@g.us        # JID do grupo (passo 2)
TARGET_NUMBER=5511999999999               # Número a monitorar (DDI+DDD+número)
EMOJI_REPLY=🔥                            # Emoji da resposta
WEBHOOK_PORT=5000                         # Porta do bot
CHECK_DELAY_SECONDS=2.0                   # Delay antes de verificar
MAX_RETRIES=10                            # Máximo de tentativas por mensagem
```

---

### 4. Configurar o Webhook no EvolutionAPI

Acesse o painel do EvolutionAPI (ou use a API) e configure o webhook da instância:

**Via painel (Swagger / UI):**
- Endpoint: `PUT /webhook/set/{instance}`
- Body:
```json
{
  "url": "http://SEU-IP:5000/webhook",
  "webhook_by_events": false,
  "webhook_base64": false,
  "events": [
    "MESSAGES_UPSERT"
  ]
}
```

> ⚠️ Se o EvolutionAPI estiver na **mesma máquina** que o bot, use `http://localhost:5000/webhook`.
> Se estiver em máquinas diferentes, use o IP da máquina do bot.

---

### 5. Iniciar o bot

```bash
python bot.py
```

Você verá uma saída parecida com:
```
════════════════════════════════════════════════
  🤖 WhatsApp Monitor Bot — EvolutionAPI
════════════════════════════════════════════════
  Instância:       minha-instancia
  Grupo alvo:      120363000000@g.us
  Número alvo:     5511999999999
  Emoji:           🔥
  Webhook URL:     http://SEU-IP:5000/webhook
════════════════════════════════════════════════
```

---

## ⚙️ COMO O BOT FUNCIONA

```
┌─────────────────────────────────────────────────────────┐
│  1. EvolutionAPI recebe mensagem no grupo               │
│  2. Envia para o webhook do bot (porta 5000)            │
│  3. Bot verifica:                                       │
│     ✓ É o grupo alvo?                                   │
│     ✓ É o número alvo?                                  │
│     ✓ A mensagem NÃO contém "@"?                        │
│  4. Se tudo OK → envia emoji citando a mensagem         │
│  5. Aguarda CHECK_DELAY_SECONDS segundos                │
│  6. Busca mensagens do grupo e verifica se fomos        │
│     os PRIMEIROS a citar aquela mensagem                │
│  7. Se SIM → para. Sucesso! ✅                          │
│     Se NÃO → tenta novamente (volta ao passo 4)        │
│  8. Para após MAX_RETRIES tentativas sem sucesso        │
└─────────────────────────────────────────────────────────┘
```

---

## 📊 EXEMPLO DE LOG DE SUCESSO

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 MENSAGEM ALVO DETECTADA
   De:    5511999999999@s.whatsapp.net
   Texto: oi galera!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔄 Tentativa 1/10
  ✅ Emoji enviado! ID da nossa mensagem: ABC123...
  ⏳ Aguardando 2.0s para verificar...
  📋 Mensagens que citaram a alvo (2 encontradas):
     [1] 16:09:46 | ⭐ NÓS   | 55119... | ID: ...ABC123
     [2] 16:09:50 |   outro  | 55117... | ID: ...XYZ789
  🏆 FOMOS OS PRIMEIROS! Objetivo alcançado.

✅ BOT CONCLUÍDO COM SUCESSO!
```

---

## 🔧 AJUSTES FINOS

| Variável | Para que serve | Dica |
|---|---|---|
| `CHECK_DELAY_SECONDS` | Tempo para aguardar antes de verificar | Diminua se sua internet for rápida |
| `MAX_RETRIES` | Quantas vezes tenta por mensagem | Aumente se houver muita concorrência |
| `EMOJI_REPLY` | O emoji enviado | Pode usar qualquer emoji ou texto curto |

---

## 🛟 SUPORTE

- Verifique o arquivo `bot.log` para diagnóstico de erros
- Endpoint de saúde: `http://SEU-IP:5000/health`
- Certifique-se que a instância está conectada no EvolutionAPI antes de iniciar o bot
