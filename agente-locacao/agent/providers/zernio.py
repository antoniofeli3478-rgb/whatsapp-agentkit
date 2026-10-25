# agent/providers/zernio.py — Adaptador da Zernio
# Adaptado do AgentKit (CLAUDE.md §3.5), traduzido para pt-BR

"""
A Zernio roda sobre a WhatsApp Cloud API da Meta: resolve a conexão da conta do WhatsApp
Business, a caixa de entrada e os webhooks assinados.

Documentação: https://docs.zernio.com/platforms/whatsapp

Dois eventos importam aqui:
  message.received  um cliente escreveu                    -> o agente responde
  message.sent      saiu uma mensagem da conta da empresa  -> se foi uma PESSOA da equipe
                    (sentVia "human", ou mandada pelo app do WhatsApp Business no celular),
                    o agente fica quieto nessa conversa para não atropelar a equipe.
"""

import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.providers.base import CLIENTE, HUMANO, MensagemRecebida, ProvedorWhatsApp

logger = logging.getLogger("agentkit")

URL_BASE_PADRAO = "https://zernio.com/api/v1"


class ProvedorZernio(ProvedorWhatsApp):
    """Provedor de WhatsApp usando a Zernio."""

    def __init__(self):
        self.api_key = os.getenv("ZERNIO_API_KEY", "")
        self.webhook_secret = os.getenv("ZERNIO_WEBHOOK_SECRET", "")
        # Opcional: só serve para o teste de conexão ao subir.
        # Para responder, o account_id vem do próprio webhook.
        self.account_id = os.getenv("ZERNIO_ACCOUNT_ID", "")
        # "or" e não o padrão do os.getenv: uma variável declarada vazia no .env devolve ""
        # e o padrão nunca seria usado.
        self.url_base = (os.getenv("ZERNIO_BASE_URL") or URL_BASE_PADRAO).rstrip("/")

        if not self.api_key:
            logger.warning("ZERNIO_API_KEY não configurada: o agente não vai conseguir responder")
        if not self.webhook_secret:
            logger.warning(
                "ZERNIO_WEBHOOK_SECRET não configurado: os webhooks NÃO são verificados. "
                "Serve para testar, mas não deixe assim em produção."
            )

    # ── Receber ──────────────────────────────────────────────────────────

    async def verificar_assinatura(self, request: Request) -> bool:
        """Compara o header X-Zernio-Signature com o HMAC-SHA256 do corpo cru."""
        if not self.webhook_secret:
            return True  # modo de teste, já avisado ao subir

        assinatura = request.headers.get("X-Zernio-Signature") or request.headers.get(
            "X-Late-Signature"
        )
        if not assinatura:
            logger.warning("Webhook sem assinatura X-Zernio-Signature: rejeitado")
            return False

        corpo = await request.body()
        esperada = hmac.new(self.webhook_secret.encode("utf-8"), corpo, hashlib.sha256).hexdigest()

        # compare_digest com str exige ASCII puro. Um header com bytes estranhos daria
        # TypeError, viraria um 500 e o provedor reenviaria o evento sete vezes.
        try:
            iguais = hmac.compare_digest(esperada, assinatura)
        except TypeError:
            logger.warning("Assinatura do webhook com caracteres inválidos: rejeitado")
            return False

        if not iguais:
            logger.warning("Assinatura do webhook inválida: rejeitado")
        return iguais

    async def parsear_webhook(self, request: Request) -> list[MensagemRecebida]:
        """Normaliza os eventos message.received e message.sent da Zernio."""
        payload = await request.json()
        evento = payload.get("event")
        mensagem = payload.get("message") or {}
        conta = payload.get("account") or {}

        if mensagem.get("platform") not in (None, "whatsapp"):
            return []

        contexto = {
            "evento_id": payload.get("id", ""),
            "conversation_id": mensagem.get("conversationId", ""),
            "account_id": conta.get("id", ""),
        }

        if evento == "message.sent":
            # Só interessa quando foi uma pessoa. As mensagens do próprio agente saem pela API
            # (sentVia "api") e não podem pausar a conversa.
            humano = mensagem.get("sentVia") == "human" or mensagem.get("source") == (
                "whatsapp_business_app"
            )
            if not humano:
                return []
            # O message.sent não traz o telefone do cliente com certeza: o main.py descobre
            # pelo conversation_id, que foi guardado quando o cliente escreveu.
            return [
                MensagemRecebida(
                    telefone="",
                    texto=mensagem.get("text") or "",
                    mensagem_id=mensagem.get("id") or "",
                    tipo=HUMANO,
                    contexto=contexto,
                )
            ]

        if evento != "message.received":
            # message.delivered, message.read etc. não pedem resposta
            logger.debug(f"Evento ignorado: {evento}")
            return []

        if mensagem.get("direction", "incoming") != "incoming":
            return []

        remetente = mensagem.get("sender") or {}
        # phoneNumber vem em E.164 com "+". Desde abril de 2026 pode faltar (usuários com
        # nome de usuário no WhatsApp não mostram o número): aí usamos o id que a Meta garante.
        telefone = (remetente.get("phoneNumber") or "").lstrip("+")
        if not telefone:
            telefone = remetente.get("businessScopedUserId") or remetente.get("id") or ""

        return [
            MensagemRecebida(
                telefone=telefone,
                texto=mensagem.get("text") or "",
                mensagem_id=mensagem.get("platformMessageId") or mensagem.get("id") or "",
                tipo=CLIENTE,
                contexto=contexto,
            )
        ]

    # ── Enviar ───────────────────────────────────────────────────────────

    async def _postar(self, corpo: dict, contexto: dict | None, sufixo_idempotencia: str) -> bool:
        contexto = contexto or {}
        conversation_id = contexto.get("conversation_id")
        account_id = contexto.get("account_id") or self.account_id

        if not self.api_key:
            logger.error("Não dá para enviar: falta ZERNIO_API_KEY")
            return False
        if not conversation_id or not account_id:
            logger.error("Não dá para enviar: falta conversation_id ou account_id")
            return False

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        # Se o webhook for reenviado, esta chave evita que a Zernio mande a resposta duas vezes
        evento_id = contexto.get("evento_id")
        if evento_id:
            headers["Idempotency-Key"] = f"agentkit-{evento_id}-{sufixo_idempotencia}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    f"{self.url_base}/inbox/conversations/{conversation_id}/messages",
                    json={"accountId": account_id, **corpo},
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.error(f"Erro de rede falando com a Zernio: {e}")
            return False

        if r.status_code == 200:
            return True

        # A Zernio responde {"error": ..., "type": ..., "code": ...}: logar os três poupa tempo
        detalhe = r.text[:500]
        try:
            erro = r.json()
            detalhe = f"{erro.get('error')} (type={erro.get('type')}, code={erro.get('code')})"
        except ValueError:
            pass
        logger.error(f"A Zernio recusou o envio [{r.status_code}]: {detalhe}")
        return False

    async def enviar_mensagem(
        self, telefone: str, mensagem: str, contexto: dict | None = None
    ) -> bool:
        """Responde dentro da conversa que o cliente abriu."""
        return await self._postar({"message": mensagem}, contexto, "texto")

    async def enviar_imagem(
        self, telefone: str, url: str, legenda: str = "", contexto: dict | None = None
    ) -> bool:
        """Manda a foto pelo link público (attachmentUrl + attachmentType image)."""
        corpo = {"attachmentUrl": url, "attachmentType": "image"}
        if legenda:
            corpo["message"] = legenda
        # O sufixo usa o fim da URL para que duas fotos do mesmo evento não colidam
        return await self._postar(corpo, contexto, "foto-" + url.rsplit("/", 1)[-1][:60])

    # ── Diagnóstico ──────────────────────────────────────────────────────

    async def verificar_conexao(self) -> tuple[bool, str]:
        """Pergunta à Zernio o estado real do número na Meta."""
        if not self.api_key:
            return False, "Falta ZERNIO_API_KEY"
        if not self.account_id:
            return True, "ZERNIO_ACCOUNT_ID não configurado: teste do número pulado"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"{self.url_base}/whatsapp/number-info",
                    params={"accountId": self.account_id},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
        except httpx.HTTPError as e:
            return False, f"Não foi possível falar com a Zernio: {e}"

        if r.status_code != 200:
            return False, f"A Zernio respondeu {r.status_code}: {r.text[:200]}"

        numero = r.json().get("phone") or {}
        return True, (
            f"Número {numero.get('display_phone_number', '?')} conectado "
            f"(qualidade: {numero.get('quality_rating', '?')})"
        )
