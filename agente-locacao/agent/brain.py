# agent/brain.py — Cérebro do agente: Claude com ferramentas e travas
# Baseado no brain.py do AgentKit (CLAUDE.md §3.8), com ciclo de tool use

"""
Uma mensagem do cliente = um turno:

1. Histórico + a mensagem nova, com um bloco de contexto (data de hoje, calendário, ficha).
2. Ciclo: Claude responde -> se pediu ferramenta, o código executa e devolve o resultado ->
   até Claude responder em texto (no máximo MAX_PASSOS chamadas).
3. Se Claude chamou passar_para_equipe, o texto para o cliente é o FIXO do perfil.
4. Travas: se o texto final promete o que só a equipe confirma ("está reservado", "a data
   está livre"), fala valor sem permissão ou cita endereço, ele NÃO sai. Claude ganha uma
   segunda chance com um aviso; se errar de novo, vai a mensagem de erro e a equipe assume.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.empresa import Empresa
from agent.prompt import contexto_do_turno, system_prompt
from agent.tools import DEFINICOES, Ferramentas

load_dotenv()
logger = logging.getLogger("agentkit")

# O modelo muda pelo .env, sem mexer no código (padrão do AgentKit: claude-sonnet-5).
# "or" e não o padrão do os.getenv: variável declarada vazia devolve "".
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Bot de respostas curtas: com esforço baixo responde mais rápido e mais barato.
# Deixe vazio no .env para não mandar o parâmetro.
ESFORCO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# O raciocínio interno também conta contra este teto: com margem curta, uma pergunta que
# exige pensar deixa o agente sem espaço para responder.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

MAX_PASSOS = 6
FUSO = ZoneInfo("America/Sao_Paulo")

# Modelos mais antigos não aceitam output_config. Se a primeira chamada falhar por isso,
# tentamos sem e lembramos para as próximas.
_aceita_esforco = True
_cliente: AsyncAnthropic | None = None


def cliente() -> AsyncAnthropic:
    """Cliente da API criado na primeira vez (os testes trocam por um falso)."""
    global _cliente
    if _cliente is None:
        _cliente = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _cliente


def definir_cliente(novo) -> None:
    """Troca o cliente da API (usado nos testes)."""
    global _cliente
    _cliente = novo


# ── Travas ───────────────────────────────────────────────────────────────

_PROMESSAS = [
    r"\b(reservei|confirmei|agendei|fechei|garanti)\b",
    r"\b(est[áa]|t[áa]|fica|ficou|foi|j[áa] est[áa]|j[áa] t[áa])\s+(reservad|confirmad|fechad|garantid|agendad|bloquead)\w*",
    r"\b(reserva|data|agendamento|festa|pedido|hor[áa]rio)\s+(est[áa]\s+|t[áa]\s+|foi\s+)?(confirmad|garantid|reservad|fechad|agendad)\w*",
    r"\b(data|dia|s[áa]bado|domingo|hor[áa]rio)\s+(est[áa]\s+|t[áa]\s+)?(livre|dispon[ií]ve(l|is))",
    r"\btemos\s+(disponibilidade|a data|vaga|o dia)",
    r"\b(est[áa]|t[áa])\s+(livre|dispon[ií]vel)\s+(no|na|nesse|nessa|dia|para|pra)\b",
]
_PRECO = [r"R\$\s*\d", r"\b\d[\d.,]*\s*(reais|conto|pila)\b"]
_ENDERECO = [
    # a palavra em qualquer caixa, seguida de um nome com inicial maiúscula
    r"\b(?i:rua|avenida|alameda|travessa|rodovia|estrada)\s+[A-ZÁÉÍÓÚÂÊÔÃÕ]",
    r"\b(?i:av|r)\.\s*[A-ZÁÉÍÓÚÂÊÔÃÕ]",
    r"\bCEP\b",
    r"\b\d{5}-\d{3}\b",
]
_NEGACAO = re.compile(r"\b(n[ãa]o|nunca|ainda n[ãa]o|sem)\b[^.!?\n]{0,25}$", re.IGNORECASE)


def problemas_do_texto(texto: str, empresa: Empresa) -> list[str]:
    """Lista o que a resposta não pode conter. Vazia = pode enviar."""
    problemas = []
    for padrao in _PROMESSAS:
        for m in re.finditer(padrao, texto, re.IGNORECASE):
            # "ainda não está confirmado" é honesto, não é promessa
            if _NEGACAO.search(texto[max(0, m.start() - 30) : m.start()]):
                continue
            problemas.append(f"promete algo que só a equipe confirma: '{m.group(0)}'")
            break
    if not empresa.mostrar_precos:
        for padrao in _PRECO:
            m = re.search(padrao, texto, re.IGNORECASE)
            if m:
                problemas.append(f"fala de valor: '{m.group(0)}'")
                break
    if not empresa.tem_endereco:
        for padrao in _ENDERECO:
            m = re.search(padrao, texto)
            if m:
                problemas.append(f"parece um endereço: '{m.group(0)}'")
                break
    return problemas


# ── Resposta ─────────────────────────────────────────────────────────────


@dataclass
class Resposta:
    texto: str
    fotos: list[tuple[str, str]] = field(default_factory=list)
    passar_para_equipe: bool = False
    motivo: str = ""
    resumo: str = ""
    pedido: dict = field(default_factory=dict)
    # False quando o texto é um aviso técnico, que não deve entrar no histórico
    conversa_real: bool = True
    ferramentas: list[str] = field(default_factory=list)


def _texto_de(resposta) -> str:
    """
    Junta o texto da resposta. NÃO dá para usar resposta.content[0].text: o primeiro bloco
    pode ser de raciocínio ou de ferramenta. Tem que filtrar por tipo.
    """
    partes = [b.text for b in resposta.content if b.type == "text"]
    return "\n".join(p for p in partes if p).strip()


def _erro_de_esforco(erro: Exception) -> bool:
    """True só se o modelo recusou a chamada POR causa do output_config/effort (400)."""
    if getattr(erro, "status_code", None) != 400:
        return False
    texto = str(erro).lower()
    return "output_config" in texto or "effort" in texto


async def _chamar(system: list, mensagens: list):
    global _aceita_esforco
    extras = {"output_config": {"effort": ESFORCO}} if (_aceita_esforco and ESFORCO) else {}
    try:
        return await cliente().messages.create(
            model=MODELO, max_tokens=MAX_TOKENS, system=system, tools=DEFINICOES,
            messages=mensagens, **extras,
        )
    except anthropic.BadRequestError as e:
        if extras and _erro_de_esforco(e):
            logger.warning(f"O modelo {MODELO} não aceita output_config.effort; seguindo sem.")
            _aceita_esforco = False
            return await cliente().messages.create(
                model=MODELO, max_tokens=MAX_TOKENS, system=system, tools=DEFINICOES,
                messages=mensagens,
            )
        raise


async def gerar_resposta(
    empresa: Empresa,
    mensagem: str,
    historico: list[dict],
    pedido: dict,
    agora: datetime | None = None,
) -> Resposta:
    """
    Gera a resposta para uma mensagem do cliente.

    historico: mensagens anteriores [{"role": "user"|"assistant", "content": "..."}]
    pedido: a ficha atual (é copiada; a versão atualizada volta em Resposta.pedido)
    """
    perfil = empresa.perfil
    if not mensagem or len(mensagem.strip()) < 1:
        return Resposta(texto=perfil.get("mensagem_nao_entendi", "Pode repetir?"), conversa_real=False, pedido=pedido)

    agora = (agora or datetime.now(FUSO)).astimezone(FUSO)
    ferramentas = Ferramentas(empresa=empresa, pedido=dict(pedido), hoje=agora.date())

    system = [{"type": "text", "text": system_prompt(empresa), "cache_control": {"type": "ephemeral"}}]
    contexto = contexto_do_turno(agora.date(), agora, ferramentas.pedido, primeira_mensagem=not historico)
    mensagens = [{"role": m["role"], "content": m["content"]} for m in historico]
    mensagens.append(
        {
            "role": "user",
            "content": [{"type": "text", "text": contexto}, {"type": "text", "text": mensagem}],
        }
    )

    def falha(motivo: str) -> Resposta:
        return Resposta(
            texto=perfil["mensagem_erro"],
            passar_para_equipe=True,
            motivo=motivo,
            resumo=ferramentas.resumo_passagem,
            pedido=ferramentas.pedido,
            conversa_real=False,
            ferramentas=ferramentas.usadas,
        )

    segunda_chance_usada = False
    passos = 0
    try:
        while True:
            passos += 1
            if passos > MAX_PASSOS + 1:
                logger.warning("O agente passou do limite de passos; a equipe assume")
                return falha("o agente se enrolou (limite de passos)")

            resposta = await _chamar(system, mensagens)
            parada = getattr(resposta, "stop_reason", None)

            if parada == "refusal":
                logger.warning("O modelo recusou responder; a equipe assume")
                return falha("o modelo recusou responder")

            if parada == "tool_use":
                mensagens.append({"role": "assistant", "content": resposta.content})
                resultados = []
                for bloco in resposta.content:
                    if bloco.type != "tool_use":
                        continue
                    saida, eh_erro = ferramentas.executar(bloco.name, bloco.input)
                    logger.debug(f"Ferramenta {bloco.name}({bloco.input}) -> {saida[:200]}")
                    resultado = {"type": "tool_result", "tool_use_id": bloco.id, "content": saida}
                    if eh_erro:
                        resultado["is_error"] = True
                    resultados.append(resultado)
                mensagens.append({"role": "user", "content": resultados})

                if ferramentas.passar_para_equipe:
                    break  # o texto para o cliente é o fixo do perfil
                continue

            if parada == "max_tokens":
                logger.warning(f"Resposta cortada no teto de {MAX_TOKENS} tokens")

            texto = _texto_de(resposta)
            if ferramentas.passar_para_equipe:
                break
            if not texto:
                return falha("o modelo respondeu sem texto")

            problemas = problemas_do_texto(texto, empresa)
            if not problemas:
                uso = resposta.usage
                logger.info(
                    f"Resposta com {MODELO}: {uso.input_tokens} in "
                    f"(cache {getattr(uso, 'cache_read_input_tokens', 0) or 0}) / {uso.output_tokens} out"
                )
                return Resposta(
                    texto=texto,
                    fotos=ferramentas.fotos,
                    pedido=ferramentas.pedido,
                    ferramentas=ferramentas.usadas,
                )

            logger.warning(f"Trava acionada: {problemas}")
            if segunda_chance_usada:
                return falha("trava de segurança: " + "; ".join(problemas))
            segunda_chance_usada = True
            mensagens.append({"role": "assistant", "content": resposta.content})
            mensagens.append(
                {
                    "role": "user",
                    "content": (
                        "[aviso do sistema — o cliente não vê isto] A sua última resposta NÃO foi "
                        "enviada porque " + "; ".join(problemas) + ". Reescreva a resposta ao "
                        "cliente sem isso: você não confirma data, reserva nem valor, e não "
                        "informa endereço. Se for o caso, chame passar_para_equipe."
                    ),
                }
            )

    except anthropic.APIStatusError as e:
        logger.error(f"Erro da API do Claude [{e.status_code}]: {e.message}")
        return falha(f"erro da API do Claude ({e.status_code})")
    except anthropic.APIConnectionError as e:
        logger.error(f"Sem conexão com a API do Claude: {e}")
        return falha("sem conexão com a API do Claude")

    # Saiu do ciclo porque o modelo chamou passar_para_equipe
    return Resposta(
        texto=perfil["mensagem_passagem"].strip(),
        fotos=ferramentas.fotos,
        passar_para_equipe=True,
        motivo=ferramentas.motivo_passagem,
        resumo=ferramentas.resumo_passagem,
        pedido=ferramentas.pedido,
        ferramentas=ferramentas.usadas,
    )
