# tests/test_webhook.py — Webhook de ponta a ponta (Zernio), com Claude e envio falsos

import pytest
from fastapi.testclient import TestClient

from agent import brain, memory
from tests.conftest import ClaudeFalso, ferramenta, resposta, texto

CLIENTE = "5516988887777"
EQUIPE = "5516900000001"


def recebida(evento_id: str, telefone: str, texto_: str, conversa: str = "conv_1") -> dict:
    return {
        "id": evento_id,
        "event": "message.received",
        "account": {"id": "acc_1"},
        "message": {
            "id": f"msg_{evento_id}",
            "platform": "whatsapp",
            "direction": "incoming",
            "text": texto_,
            "conversationId": conversa,
            "sender": {"phoneNumber": "+" + telefone},
        },
    }


def enviada_por_humano(evento_id: str, texto_: str, conversa: str = "conv_1") -> dict:
    return {
        "id": evento_id,
        "event": "message.sent",
        "account": {"id": "acc_1"},
        "message": {
            "id": f"msg_{evento_id}",
            "platform": "whatsapp",
            "text": texto_,
            "conversationId": conversa,
            "sentVia": "human",
        },
    }


@pytest.fixture()
def app(monkeypatch):
    from agent import main

    enviados: list[tuple] = []

    async def enviar_mensagem(telefone, mensagem, contexto=None):
        enviados.append(("texto", telefone, mensagem, dict(contexto or {})))
        return True

    async def enviar_imagem(telefone, url, legenda="", contexto=None):
        enviados.append(("foto", telefone, url, dict(contexto or {})))
        return True

    monkeypatch.setattr(main.provedor, "enviar_mensagem", enviar_mensagem)
    monkeypatch.setattr(main.provedor, "enviar_imagem", enviar_imagem)
    with TestClient(main.app) as cliente:
        cliente.enviados = enviados
        yield cliente


def test_health_check(app):
    corpo = app.get("/").json()
    assert corpo["empresa"] == "Anima Kids Park"
    assert corpo["agente"] == "Anima Kids Park Bot"


def test_fluxo_completo(app):
    brain.definir_cliente(
        ClaudeFalso(
            # 1) cliente pergunta: o agente anota e manda foto
            resposta(
                ferramenta("anotar_pedido", {"idades": "5 a 9 anos", "brinquedos": ["big-shark", "pipoca"]}),
                ferramenta("enviar_foto", {"id": "big-shark"}, id_="tu_2"),
            ),
            resposta(texto("Que demais! O Big Shark é a cara dessa festa 🦈 Qual a data?")),
            # 2) cliente pede preço: o agente passa para a equipe
            resposta(ferramenta("passar_para_equipe", {"motivo": "pediu preço", "resumo": "Festa 5 a 9 anos."})),
        )
    )

    r = app.post("/webhook", json=recebida("ev1", CLIENTE, "Oi! Festa de 5 a 9 anos, tem pipoca?"))
    assert r.status_code == 200 and r.json()["enfileiradas"] == 1
    tipos = [e[0] for e in app.enviados]
    assert tipos == ["texto", "foto"]
    assert app.enviados[0][3]["conversation_id"] == "conv_1"

    # o mesmo evento de novo: ignorado (entrega "pelo menos uma vez")
    app.post("/webhook", json=recebida("ev1", CLIENTE, "Oi! Festa de 5 a 9 anos, tem pipoca?"))
    assert len(app.enviados) == 2

    app.enviados.clear()
    app.post("/webhook", json=recebida("ev2", CLIENTE, "Quanto fica?"))
    textos = [e[2] for e in app.enviados if e[0] == "texto"]
    assert any("passei para a nossa equipe" in t for t in textos)

    # o cliente escreve de novo: a equipe está com ele, o agente não responde
    app.enviados.clear()
    app.post("/webhook", json=recebida("ev3", CLIENTE, "Oi? Alguém?"))
    assert app.enviados == []

    # a equipe manda o comando de outro celular: o agente volta
    app.post("/webhook", json=recebida("ev4", EQUIPE, f"#bot voltar {CLIENTE}", conversa="conv_equipe"))
    assert "voltou a atender" in app.enviados[-1][2]

    # a ficha aparece no painel
    pagina = app.get("/painel?token=segredo").text
    assert "Big Shark" in pagina and "Sob consulta" in pagina
    assert app.get("/painel?token=errado").status_code == 403


def test_equipe_respondendo_pelo_celular_pausa_o_agente(app):
    tel = "5516977776666"
    brain.definir_cliente(ClaudeFalso(resposta(texto("Oi! Bora montar essa festa? 🎈"))))
    app.post("/webhook", json=recebida("ev10", tel, "oi", conversa="conv_10"))
    assert len(app.enviados) == 1

    app.post("/webhook", json=enviada_por_humano("ev11", "Oi, aqui é a Ana da Anima Kids!", conversa="conv_10"))
    app.enviados.clear()
    app.post("/webhook", json=recebida("ev12", tel, "Oi Ana!", conversa="conv_10"))
    assert app.enviados == [], "com a equipe na conversa, o agente fica quieto"


def test_assinatura_invalida(monkeypatch, app):
    from agent import main

    monkeypatch.setattr(main.provedor, "webhook_secret", "segredo-zernio")
    r = app.post("/webhook", json=recebida("ev20", CLIENTE, "oi"), headers={"X-Zernio-Signature": "abc"})
    assert r.status_code == 401


async def test_pausa_vence_sozinha():
    await memory.inicializar_db()
    await memory.pausar_agente("5516911112222", horas=-1, motivo="teste")
    assert await memory.agente_pausado("5516911112222") is False
