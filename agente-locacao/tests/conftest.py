# tests/conftest.py — Configuração comum dos testes (sem API de verdade)

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# O ambiente precisa estar pronto ANTES de importar o agente (main.py lê no import)
_PASTA_TEMP = tempfile.mkdtemp(prefix="agente-locacao-testes-")
os.environ.update(
    {
        "EMPRESA": "anima-kids",
        "WHATSAPP_PROVIDER": "zernio",
        "ZERNIO_API_KEY": "sk_teste",
        "ZERNIO_WEBHOOK_SECRET": "",
        "ZERNIO_ACCOUNT_ID": "",
        "ANTHROPIC_API_KEY": "sk-ant-teste",
        "ENVIRONMENT": "test",
        "EQUIPE_WHATSAPP": "5516900000001",
        "PAINEL_TOKEN": "segredo",
        "DATABASE_URL": f"sqlite+aiosqlite:///{_PASTA_TEMP}/teste.db",
    }
)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Claude falso ─────────────────────────────────────────────────────────


def texto(t: str):
    return SimpleNamespace(type="text", text=t)


def ferramenta(nome: str, entrada: dict, id_: str = "tu_1"):
    return SimpleNamespace(type="tool_use", name=nome, input=entrada, id=id_)


def resposta(*blocos, parada: str | None = None):
    if parada is None:
        parada = "tool_use" if any(b.type == "tool_use" for b in blocos) else "end_turn"
    return SimpleNamespace(
        content=list(blocos),
        stop_reason=parada,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


class ClaudeFalso:
    """Devolve as respostas na ordem e guarda as chamadas recebidas."""

    def __init__(self, *respostas):
        self.fila = list(respostas)
        self.chamadas: list[dict] = []
        self.messages = self

    async def create(self, **kwargs):
        # cópia da lista: o brain continua acrescentando mensagens depois da chamada
        self.chamadas.append({**kwargs, "messages": list(kwargs["messages"])})
        if not self.fila:
            raise AssertionError("O Claude falso ficou sem respostas")
        return self.fila.pop(0)
