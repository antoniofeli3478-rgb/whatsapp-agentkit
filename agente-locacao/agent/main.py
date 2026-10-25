# agent/main.py — Servidor FastAPI + webhook do WhatsApp
# Baseado no main.py do AgentKit (CLAUDE.md §3.7), com a passagem para a equipe

"""
Servidor do agente. Funciona com qualquer provedor (Zernio, Meta) pela camada providers/.

Regra de ouro: a IA atende, a equipe fecha.
  - O cliente escreve -> o agente responde, sugere brinquedos, manda fotos e anota a ficha.
  - Quando a ficha fica pronta (ou o cliente pede preço, uma pessoa etc.), o agente passa a
    conversa para a equipe: avisa a equipe com a ficha e fica quieto com aquele cliente.
  - Se alguém da equipe responde pelo celular (Zernio avisa), o agente também fica quieto.
  - O agente volta sozinho depois de PAUSA_HORAS, pelo painel, ou com "#bot voltar <telefone>"
    mandado de um número da equipe.
"""

import asyncio
import html
import logging
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from agent import memory
from agent.brain import gerar_resposta
from agent.empresa import empresa_atual
from agent.providers import CLIENTE, HUMANO, MensagemRecebida, obter_provedor
from agent.tools import montar_ficha

load_dotenv()

AMBIENTE = os.getenv("ENVIRONMENT", "development")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# Em desenvolvimento queremos o detalhe do NOSSO agente, não o barulho das bibliotecas
logger.setLevel(logging.DEBUG if AMBIENTE == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))
PAUSA_HORAS = float(os.getenv("PAUSA_HORAS") or "12")
PAINEL_TOKEN = os.getenv("PAINEL_TOKEN", "").strip()


def _so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


# Números da equipe (só dígitos, com DDI), separados por vírgula. Recebem a ficha e podem
# mandar comandos "#bot ...".
EQUIPE = [_so_digitos(n) for n in os.getenv("EQUIPE_WHATSAPP", "").split(",") if _so_digitos(n)]

# Um cadeado por cliente: "oi" e a pergunta de verdade meio segundo depois são atendidos em
# ordem, sem ler o mesmo histórico ao mesmo tempo.
_cadeados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Configuração errada não derruba o servidor: o erro aparece no health check.
provedor = None
empresa = None
erros_configuracao: list[str] = []
try:
    provedor = obter_provedor()
except Exception as e:  # noqa: BLE001 — qualquer problema de configuração
    erros_configuracao.append(str(e))
try:
    empresa = empresa_atual()
except Exception as e:  # noqa: BLE001
    erros_configuracao.append(str(e))

estado_provedor: dict = {"ok": None, "detalhe": "não verificado"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara o banco e testa o provedor ao subir."""
    await memory.inicializar_db()
    await memory.limpar_eventos_antigos()
    logger.info(f"Servidor no ar na porta {PORT}")

    global estado_provedor
    if empresa is not None:
        logger.info(f"Empresa: {empresa.nome} — agente '{empresa.nome_agente}'")
    if provedor is not None:
        ok, detalhe = await provedor.verificar_conexao()
        estado_provedor = {"ok": ok, "detalhe": detalhe}
        logger.info(f"Provedor {provedor.__class__.__name__}: {'OK' if ok else 'ERRO'} — {detalhe}")
    for erro in erros_configuracao:
        logger.error(f"Configuração: {erro}")
    if not EQUIPE:
        logger.warning("EQUIPE_WHATSAPP vazio: as fichas só vão aparecer no log e no /painel")
    yield


app = FastAPI(title="Agente de locação — WhatsApp", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def health_check():
    """Saúde do serviço. Responde 200 mesmo com erro, para dar para ler o diagnóstico."""
    if erros_configuracao:
        return {"status": "erro", "servico": "agente-locacao", "detalhe": erros_configuracao}
    return {
        "status": "ok" if estado_provedor["ok"] else "degradado",
        "servico": "agente-locacao",
        "empresa": empresa.nome,
        "agente": empresa.nome_agente,
        "provedor": provedor.__class__.__name__,
        "conexao": estado_provedor,
    }


@app.get("/webhook")
async def webhook_verificacao(request: Request):
    """Verificação GET do webhook. A Meta pede; para a Zernio não faz nada."""
    if provedor is None:
        raise HTTPException(status_code=503, detail="Provedor não configurado")

    resposta = await provedor.validar_webhook(request)
    if resposta is not None:
        return PlainTextResponse(resposta)
    # A Meta pede 403 quando o verify_token não bate; 200 faria ela achar que deu certo
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorreto")
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, tarefas: BackgroundTasks):
    """
    Recebe os eventos do WhatsApp. Responde 200 NA HORA e trabalha em segundo plano.

    Não é detalhe de estilo: os provedores esperam um 2xx em uns 5 segundos e, se não
    recebem, reenviam o mesmo evento até 7 vezes. Chamar o Claude demora mais que isso.
    """
    if provedor is None or empresa is None:
        raise HTTPException(status_code=503, detail="; ".join(erros_configuracao) or "Não configurado")

    if not await provedor.verificar_assinatura(request):
        raise HTTPException(status_code=401, detail="Assinatura do webhook inválida")

    try:
        mensagens = await provedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001 — payload estranho não pode virar reenvio infinito
        logger.error(f"Não deu para ler o webhook: {e}")
        return {"status": "ignorado"}

    enfileiradas = 0
    for msg in mensagens:
        texto = msg.texto.strip()
        evento_id = msg.contexto.get("evento_id") or msg.mensagem_id

        # A entrega é "pelo menos uma vez": o mesmo evento pode chegar duas vezes
        if evento_id and not await memory.marcar_evento_processado(evento_id):
            logger.info(f"Evento repetido, ignorado: {evento_id}")
            continue

        if msg.tipo == HUMANO:
            tarefas.add_task(equipe_respondeu, msg)
            continue

        if msg.tipo != CLIENTE or not texto or not msg.telefone:
            continue

        msg.telefone = _so_digitos(msg.telefone) or msg.telefone
        await memory.guardar_contexto(msg.telefone, msg.contexto)

        if msg.telefone in EQUIPE and texto.lower().startswith("#bot"):
            tarefas.add_task(comando_da_equipe, msg)
            enfileiradas += 1
            continue

        logger.info(f"Mensagem de {msg.telefone}: {texto}")
        tarefas.add_task(processar_mensagem, msg)
        enfileiradas += 1

    return {"status": "ok", "enfileiradas": enfileiradas}


# ── Atendimento ──────────────────────────────────────────────────────────


async def processar_mensagem(msg: MensagemRecebida):
    """Gera a resposta e manda de volta. Roda fora do ciclo do webhook."""
    evento_id = msg.contexto.get("evento_id") or msg.mensagem_id

    async with _cadeados[msg.telefone]:
        try:
            if await memory.agente_pausado(msg.telefone):
                # A equipe está com o cliente: o agente só guarda a mensagem para ter contexto
                await memory.guardar_mensagem(msg.telefone, "user", msg.texto)
                logger.info(f"{msg.telefone} está com a equipe; o agente não responde")
                return

            # O histórico é lido ANTES de guardar a mensagem atual (o brain coloca ela no fim)
            historico = await memory.obter_historico(msg.telefone)
            pedido = await memory.obter_pedido(msg.telefone)
            resposta = await gerar_resposta(empresa, msg.texto, historico, pedido)

            enviado = await provedor.enviar_mensagem(msg.telefone, resposta.texto, msg.contexto)
            if not enviado:
                # O evento foi marcado ANTES de chegar aqui (para duas entregas simultâneas não
                # duplicarem). Se o envio falhou, soltamos: senão o reenvio do provedor seria
                # descartado como repetido e o cliente ficaria sem resposta para sempre.
                logger.error(f"Não deu para responder {msg.telefone}; evento liberado")
                await memory.liberar_evento(evento_id)
                return

            for url, legenda in resposta.fotos:
                if not await provedor.enviar_imagem(msg.telefone, url, legenda, msg.contexto):
                    await provedor.enviar_mensagem(msg.telefone, f"📸 {legenda}: {url}", msg.contexto)

            # Só entra no histórico o que é conversa. Avisos técnicos ficariam poluindo o
            # contexto de todas as próximas mensagens.
            if resposta.conversa_real:
                await memory.guardar_mensagem(msg.telefone, "user", msg.texto)
                await memory.guardar_mensagem(msg.telefone, "assistant", resposta.texto)
            else:
                await memory.guardar_mensagem(msg.telefone, "user", msg.texto)
            await memory.guardar_pedido(msg.telefone, resposta.pedido)

            logger.info(f"Resposta para {msg.telefone}: {resposta.texto}")

            if resposta.passar_para_equipe:
                await passar_para_equipe(msg.telefone, resposta.pedido, resposta.motivo, resposta.resumo)

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Erro atendendo {msg.telefone}: {e}")
            await memory.liberar_evento(evento_id)


async def passar_para_equipe(telefone: str, pedido: dict, motivo: str, resumo: str):
    """Pausa o agente com o cliente e manda a ficha para a equipe."""
    await memory.pausar_agente(telefone, PAUSA_HORAS, motivo or "passado para a equipe")
    await memory.marcar_pedido_enviado(telefone, motivo)

    ficha = montar_ficha(empresa, telefone, pedido, motivo, resumo)
    logger.info("FICHA PARA A EQUIPE\n" + ficha)
    await avisar_equipe(ficha)


async def avisar_equipe(texto: str):
    """
    Manda um texto para cada número da equipe.

    Pelo WhatsApp só dá para mandar texto livre para quem escreveu nas últimas 24 horas (e,
    na Zernio, para quem já tem conversa com o número). Se falhar, a ficha continua no log e
    no /painel.
    """
    for numero in EQUIPE:
        contexto = await memory.obter_contexto(numero)
        contexto.pop("evento_id", None)  # sem chave de idempotência: cada aviso é novo
        if not await provedor.enviar_mensagem(numero, texto, contexto):
            logger.warning(f"Não deu para avisar {numero} pelo WhatsApp: ficha no /painel")


async def equipe_respondeu(msg: MensagemRecebida):
    """Alguém da equipe respondeu pelo celular: o agente sai de cena com esse cliente."""
    telefone = await memory.telefone_da_conversa(msg.contexto.get("conversation_id", ""))
    if not telefone:
        return
    texto = msg.texto.strip()

    if texto.lower().startswith("#bot voltar"):
        await memory.retomar_agente(telefone)
        logger.info(f"A equipe devolveu {telefone} para o agente")
        return

    await memory.pausar_agente(telefone, PAUSA_HORAS, "a equipe respondeu pelo celular")
    if texto:
        await memory.guardar_mensagem(telefone, "assistant", texto)
    logger.info(f"A equipe assumiu {telefone}; agente pausado por {PAUSA_HORAS:g} h")


async def comando_da_equipe(msg: MensagemRecebida):
    """
    Comandos que a equipe manda do próprio celular para o número da empresa:
      #bot voltar 5516999998888   o agente volta a atender esse cliente
      #bot pausar 5516999998888   o agente para de atender esse cliente
      #bot status                 lista os clientes que estão com a equipe
    """
    partes = msg.texto.strip().split()
    acao = partes[1].lower() if len(partes) > 1 else "ajuda"
    alvo = _so_digitos(partes[2]) if len(partes) > 2 else ""

    if acao == "voltar" and alvo:
        ok = await memory.retomar_agente(alvo)
        resposta = f"✅ O agente voltou a atender {alvo}." if ok else f"Não achei o cliente {alvo}."
    elif acao == "pausar" and alvo:
        await memory.pausar_agente(alvo, PAUSA_HORAS, "pausado pela equipe")
        resposta = f"⏸️ Agente pausado com {alvo} por {PAUSA_HORAS:g} h."
    elif acao == "status":
        pausadas = await memory.listar_conversas_pausadas()
        if pausadas:
            resposta = "Com a equipe agora:\n" + "\n".join(
                f"- {p['telefone']} ({p['motivo']})" for p in pausadas
            )
        else:
            resposta = "Nenhum cliente com a equipe agora. O agente está atendendo todo mundo."
    else:
        resposta = "Comandos: #bot voltar <telefone> | #bot pausar <telefone> | #bot status"

    await provedor.enviar_mensagem(msg.telefone, resposta, msg.contexto)


# ── Painel da equipe ─────────────────────────────────────────────────────


def _conferir_token(token: str):
    if not PAINEL_TOKEN:
        raise HTTPException(status_code=404, detail="Painel desligado (configure PAINEL_TOKEN)")
    if token != PAINEL_TOKEN:
        raise HTTPException(status_code=403, detail="Token inválido")


@app.get("/painel", response_class=HTMLResponse)
async def painel(token: str = ""):
    """Página simples com as fichas e os clientes que estão com a equipe."""
    _conferir_token(token)
    pedidos = await memory.listar_pedidos()
    pausadas = {p["telefone"]: p for p in await memory.listar_conversas_pausadas()}
    e = html.escape

    linhas = []
    for p in pedidos:
        ficha = montar_ficha(empresa, p["telefone"], p["dados"], p["motivo"], "")
        pausada = pausadas.get(p["telefone"])
        botao = (
            f'<form method="post" action="/painel/voltar?token={e(token)}&telefone={e(p["telefone"])}">'
            '<button>Devolver para o agente</button></form>'
            if pausada
            else "<small>com o agente</small>"
        )
        linhas.append(f"<article><pre>{e(ficha)}</pre>{botao}</article>")

    corpo = "".join(linhas) or "<p>Nenhuma ficha ainda.</p>"
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel — {e(empresa.nome)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0 auto;max-width:760px;padding:16px;background:#fafafa;color:#222}}
article{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:12px;margin:12px 0}}
pre{{white-space:pre-wrap;margin:0 0 8px;font:14px/1.45 ui-monospace,monospace}}
button{{padding:8px 12px;border-radius:8px;border:1px solid #888;background:#fff;cursor:pointer}}
</style></head><body><h1>🎈 {e(empresa.nome)}: fichas</h1>{corpo}</body></html>"""


@app.post("/painel/voltar")
async def painel_voltar(token: str = "", telefone: str = ""):
    """Botão do painel: o agente volta a atender o cliente."""
    _conferir_token(token)
    await memory.retomar_agente(_so_digitos(telefone))
    return RedirectResponse(f"/painel?token={token}", status_code=303)
