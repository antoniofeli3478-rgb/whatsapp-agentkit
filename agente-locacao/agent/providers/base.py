# agent/providers/base.py — Interface comum dos provedores de WhatsApp
# Adaptado do AgentKit (CLAUDE.md §3.3), traduzido para pt-BR

"""
Define o formato que todo provedor de WhatsApp entrega e recebe.
Graças a isso, o main.py não sabe nem se importa com qual provedor está ligado.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fastapi import Request

# Tipos de mensagem que o webhook pode trazer
CLIENTE = "cliente"  # alguém escreveu para a empresa
HUMANO = "humano"  # alguém da equipe respondeu pelo celular ou pelo painel do provedor


@dataclass
class MensagemRecebida:
    """Mensagem normalizada: o mesmo formato, seja qual for o provedor."""

    telefone: str  # número do cliente, só dígitos, sem "+"
    texto: str
    mensagem_id: str
    tipo: str = CLIENTE
    contexto: dict = field(default_factory=dict)
    # "contexto" leva o que cada provedor precisa para responder:
    #   evento_id       -> id único do evento, para não processar duas vezes
    #   conversation_id -> Zernio: em qual conversa responder
    #   account_id      -> Zernio: qual conta de WhatsApp recebeu a mensagem


class ProvedorWhatsApp(ABC):
    """Interface que cada provedor de WhatsApp implementa."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensagemRecebida]:
        """Extrai e normaliza as mensagens do webhook."""
        ...

    @abstractmethod
    async def enviar_mensagem(
        self, telefone: str, mensagem: str, contexto: dict | None = None
    ) -> bool:
        """Envia um texto. Retorna True se deu certo."""
        ...

    async def enviar_imagem(
        self, telefone: str, url: str, legenda: str = "", contexto: dict | None = None
    ) -> bool:
        """
        Envia uma foto pelo link público. Quem não implementar retorna False,
        e o main.py manda o link como texto no lugar.
        """
        return False

    async def verificar_assinatura(self, request: Request) -> bool:
        """Confirma que o webhook veio mesmo do provedor. Por padrão aceita tudo."""
        return True

    async def validar_webhook(self, request: Request) -> str | None:
        """Verificação GET do webhook. Só a Meta usa. Retorna a resposta ou None."""
        return None

    async def verificar_conexao(self) -> tuple[bool, str]:
        """Confere se as credenciais servem. Retorna (ok, mensagem legível)."""
        return True, "Este provedor não tem verificação de conexão"
