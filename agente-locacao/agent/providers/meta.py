# agent/providers/meta.py — Adaptador da Meta WhatsApp Cloud API
# Adaptado do AgentKit (CLAUDE.md §3.6), traduzido para pt-BR

"""
Conexão direta com a API oficial da Meta.
Documentação: https://developers.facebook.com/docs/whatsapp/cloud-api

Diferença importante para a Zernio: a Meta não avisa quando alguém da equipe responde pelo
celular, então a pausa automática não acontece. A equipe pausa e retoma o agente pelo painel
(/painel) ou pelo comando "#bot pausar <telefone>".
"""

import hashlib
import hmac
import logging
import os

import httpx
from fastapi import Request

from agent.providers.base import CLIENTE, MensagemRecebida, ProvedorWhatsApp

logger = logging.getLogger("agentkit")


class ProvedorMeta(ProvedorWhatsApp):
    """Provedor de WhatsApp usando a API oficial da Meta (Cloud API)."""

    def __init__(self):
        self.access_token = os.getenv("META_ACCESS_TOKEN", "")
        self.phone_number_id = os.getenv("META_PHONE_NUMBER_ID", "")
        # Mesmo cuidado do zernio.py: variável vazia no .env devolve "", então "or"
        self.verify_token = os.getenv("META_VERIFY_TOKEN") or "agentkit-verify"
        self.app_secret = os.getenv("META_APP_SECRET", "")
        self.api_version = os.getenv("META_API_VERSION") or "v25.0"

        if not self.access_token or not self.phone_number_id:
            logger.warning(
                "Faltam META_ACCESS_TOKEN ou META_PHONE_NUMBER_ID: o agente não vai conseguir responder"
            )
        if not self.app_secret:
            logger.warning(
                "META_APP_SECRET não configurado: os webhooks NÃO são verificados. "
                "Serve para testar, mas não deixe assim em produção."
            )

    # ── Receber ──────────────────────────────────────────────────────────

    async def validar_webhook(self, request: Request) -> str | None:
        """A Meta faz um GET com hub.challenge na primeira vez; devolvemos o challenge."""
        params = request.query_params
        if (
            params.get("hub.mode") == "subscribe"
            and params.get("hub.verify_token") == self.verify_token
        ):
            return params.get("hub.challenge") or ""
        return None

    async def verificar_assinatura(self, request: Request) -> bool:
        """Compara o header X-Hub-Signature-256 com o HMAC-SHA256 do corpo cru."""
        if not self.app_secret:
            return True  # modo de teste, já avisado ao subir

        cabecalho = request.headers.get("X-Hub-Signature-256", "")
        if not cabecalho.startswith("sha256="):
            logger.warning("Webhook sem assinatura X-Hub-Signature-256: rejeitado")
            return False

        corpo = await request.body()
        esperada = hmac.new(self.app_secret.encode("utf-8"), corpo, hashlib.sha256).hexdigest()

        try:
            iguais = hmac.compare_digest(esperada, cabecalho.removeprefix("sha256="))
        except TypeError:
            logger.warning("Assinatura do webhook com caracteres inválidos: rejeitado")
            return False

        if not iguais:
            logger.warning("Assinatura do webhook inválida: rejeitado")
        return iguais

    async def parsear_webhook(self, request: Request) -> list[MensagemRecebida]:
        """Percorre o payload aninhado da Cloud API."""
        corpo = await request.json()
        mensagens: list[MensagemRecebida] = []

        for entry in corpo.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                for msg in value.get("messages", []):
                    if msg.get("type") != "text":
                        continue  # por enquanto, só texto
                    mensagens.append(
                        MensagemRecebida(
                            telefone=msg.get("from", ""),
                            texto=(msg.get("text") or {}).get("body", ""),
                            mensagem_id=msg.get("id", ""),
                            tipo=CLIENTE,
                            contexto={"evento_id": msg.get("id", "")},
                        )
                    )
        return mensagens

    # ── Enviar ───────────────────────────────────────────────────────────

    async def _postar(self, corpo: dict) -> bool:
        if not self.access_token or not self.phone_number_id:
            logger.error("Não dá para enviar: faltam META_ACCESS_TOKEN ou META_PHONE_NUMBER_ID")
            return False

        try:
            async with httpx.AsyncClient(timeout=30.0) as cliente:
                r = await cliente.post(
                    f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages",
                    json={"messaging_product": "whatsapp", **corpo},
                    headers={
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as e:
            logger.error(f"Erro de rede falando com a Meta: {e}")
            return False

        if r.status_code == 200:
            return True
        logger.error(f"A Meta recusou o envio [{r.status_code}]: {r.text[:500]}")
        return False

    async def enviar_mensagem(
        self, telefone: str, mensagem: str, contexto: dict | None = None
    ) -> bool:
        """Envia um texto. A Meta não precisa do contexto."""
        return await self._postar({"to": telefone, "type": "text", "text": {"body": mensagem}})

    async def enviar_imagem(
        self, telefone: str, url: str, legenda: str = "", contexto: dict | None = None
    ) -> bool:
        """Envia a foto pelo link público."""
        imagem = {"link": url}
        if legenda:
            imagem["caption"] = legenda
        return await self._postar({"to": telefone, "type": "image", "image": imagem})

    # ── Diagnóstico ──────────────────────────────────────────────────────

    async def verificar_conexao(self) -> tuple[bool, str]:
        """Lê o número na Graph API para confirmar que o token serve."""
        if not self.access_token or not self.phone_number_id:
            return False, "Faltam META_ACCESS_TOKEN ou META_PHONE_NUMBER_ID"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}",
                    params={"fields": "display_phone_number,verified_name,quality_rating"},
                    headers={"Authorization": f"Bearer {self.access_token}"},
                )
        except httpx.HTTPError as e:
            return False, f"Não foi possível falar com a Meta: {e}"

        if r.status_code != 200:
            return False, f"A Meta respondeu {r.status_code}: {r.text[:200]}"

        dados = r.json()
        return True, (
            f"Número {dados.get('display_phone_number', '?')} conectado "
            f"(qualidade: {dados.get('quality_rating', '?')})"
        )
