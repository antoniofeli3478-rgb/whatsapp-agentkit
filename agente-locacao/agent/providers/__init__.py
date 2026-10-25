# agent/providers/__init__.py — Escolhe o provedor de WhatsApp
# Adaptado do AgentKit (CLAUDE.md §3.4)

import os

from agent.providers.base import CLIENTE, HUMANO, MensagemRecebida, ProvedorWhatsApp

PROVEDORES_SUPORTADOS = ("zernio", "meta")


def obter_provedor() -> ProvedorWhatsApp:
    """
    Retorna o provedor configurado no .env (WHATSAPP_PROVIDER).

    Não roda no import: se a configuração estiver errada, o servidor sobe mesmo assim e
    conta o problema no health check, em vez de morrer e deixar a Railway reiniciando às cegas.
    """
    provedor = os.getenv("WHATSAPP_PROVIDER", "").strip().lower()

    if not provedor:
        raise ValueError(
            "WHATSAPP_PROVIDER não está configurado no .env. "
            f"Valores válidos: {' | '.join(PROVEDORES_SUPORTADOS)}"
        )

    if provedor == "zernio":
        from agent.providers.zernio import ProvedorZernio

        return ProvedorZernio()

    if provedor == "meta":
        from agent.providers.meta import ProvedorMeta

        return ProvedorMeta()

    raise ValueError(
        f"Provedor não suportado: '{provedor}'. "
        f"Valores válidos: {' | '.join(PROVEDORES_SUPORTADOS)}"
    )


__all__ = [
    "CLIENTE",
    "HUMANO",
    "MensagemRecebida",
    "ProvedorWhatsApp",
    "PROVEDORES_SUPORTADOS",
    "obter_provedor",
]
