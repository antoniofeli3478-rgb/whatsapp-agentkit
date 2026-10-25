# tests/test_brain.py — Ciclo de ferramentas e travas, com um Claude falso

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent import brain
from agent.brain import gerar_resposta, problemas_do_texto
from agent.empresa import carregar_empresa
from tests.conftest import ClaudeFalso, ferramenta, resposta, texto

AGORA = datetime(2026, 10, 25, 15, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


@pytest.fixture(scope="module")
def empresa():
    return carregar_empresa("anima-kids")


# ── Travas ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "frase",
    [
        "Prontinho, sua data está confirmada! 🎉",
        "Reservei o Big Shark para você.",
        "O sábado está livre, pode ficar tranquila!",
        "Temos disponibilidade no dia 15.",
        "A reserva foi confirmada.",
    ],
)
def test_trava_pega_promessa(empresa, frase):
    assert problemas_do_texto(frase, empresa)


@pytest.mark.parametrize(
    "frase",
    [
        "A data ainda não está confirmada: quem confirma é a equipe 😉",
        "Vou passar para a equipe confirmar a agenda e te mandar o orçamento.",
        "O Big Shark é perfeito para 30 crianças de 5 a 10 anos!",
        "Pra festa na piscina, o Super Golfinho fica lindo. Quer ver a foto?",
    ],
)
def test_trava_deixa_passar_texto_honesto(empresa, frase):
    assert problemas_do_texto(frase, empresa) == []


def test_trava_pega_preco_e_endereco(empresa):
    assert problemas_do_texto("Fica R$ 450 a diária.", empresa)
    assert problemas_do_texto("Sai por 300 reais.", empresa)
    assert problemas_do_texto("Nossa loja fica na Rua Garibaldi, 1200.", empresa)
    assert problemas_do_texto("O CEP é 14090-000.", empresa)


# ── Ciclo de ferramentas ─────────────────────────────────────────────────


async def test_ciclo_com_ferramenta_e_resposta(empresa):
    falso = ClaudeFalso(
        resposta(
            ferramenta("anotar_pedido", {"data": "15/11/2026", "idades": "3 a 6 anos"}),
            ferramenta("enviar_foto", {"id": "big-shark"}, id_="tu_2"),
        ),
        resposta(texto("Anotado! Olha o Big Shark aí 🦈 Quantas crianças, mais ou menos?")),
    )
    brain.definir_cliente(falso)

    r = await gerar_resposta(empresa, "Festa dia 15/11, crianças de 3 a 6", [], {}, agora=AGORA)

    assert r.texto.startswith("Anotado!")
    assert r.pedido["data"] == "15/11/2026"
    assert len(r.fotos) == 1 and r.passar_para_equipe is False
    assert r.ferramentas == ["anotar_pedido", "enviar_foto"]

    # 1ª chamada: system com cache, contexto com a data de hoje e mensagem do cliente
    primeira = falso.chamadas[0]
    assert primeira["system"][0]["cache_control"] == {"type": "ephemeral"}
    conteudo = primeira["messages"][-1]["content"]
    assert "25/10/2026" in conteudo[0]["text"] and conteudo[1]["text"].startswith("Festa dia")
    # 2ª chamada: os dois resultados das ferramentas voltam juntos numa mensagem só
    resultados = falso.chamadas[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in resultados] == ["tu_1", "tu_2"]


async def test_passar_para_equipe_usa_texto_fixo(empresa):
    brain.definir_cliente(
        ClaudeFalso(
            resposta(
                texto("Vou te passar pra equipe agora mesmo e tá tudo reservado!"),
                ferramenta("passar_para_equipe", {"motivo": "pediu preço", "resumo": "Quer Big Shark."}),
            )
        )
    )
    r = await gerar_resposta(empresa, "Quanto custa o Big Shark?", [], {}, agora=AGORA)
    assert r.passar_para_equipe
    assert r.motivo == "pediu preço"
    # o texto do modelo (com promessa) foi descartado: vai o texto fixo do perfil
    assert r.texto == empresa.perfil["mensagem_passagem"].strip()


async def test_segunda_chance_conserta_promessa(empresa):
    falso = ClaudeFalso(
        resposta(texto("Show! Sua data está confirmada 🎉")),
        resposta(texto("Show! Vou deixar anotado e a equipe confirma a agenda com você 🎉")),
    )
    brain.definir_cliente(falso)
    r = await gerar_resposta(empresa, "Quero dia 15/11", [], {}, agora=AGORA)
    assert r.texto.startswith("Show! Vou deixar")
    assert not r.passar_para_equipe
    aviso = falso.chamadas[1]["messages"][-1]["content"]
    assert "NÃO foi enviada" in aviso


async def test_promessa_repetida_vai_para_a_equipe(empresa):
    brain.definir_cliente(
        ClaudeFalso(
            resposta(texto("Fica R$ 500, beleza?")),
            resposta(texto("Então, sai por 500 reais.")),
        )
    )
    r = await gerar_resposta(empresa, "Quanto é?", [], {}, agora=AGORA)
    assert r.passar_para_equipe and not r.conversa_real
    assert r.texto == empresa.perfil["mensagem_erro"]
    assert "trava" in r.motivo


async def test_erro_da_api_vira_mensagem_de_erro(empresa):
    import anthropic
    import httpx

    class Quebrado:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))

    brain.definir_cliente(Quebrado())
    r = await gerar_resposta(empresa, "oi", [], {}, agora=AGORA)
    assert r.passar_para_equipe and not r.conversa_real


async def test_recusa_do_modelo(empresa):
    brain.definir_cliente(ClaudeFalso(resposta(parada="refusal")))
    r = await gerar_resposta(empresa, "oi", [], {}, agora=AGORA)
    assert r.passar_para_equipe
