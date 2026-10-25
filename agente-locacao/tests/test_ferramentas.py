# tests/test_ferramentas.py — Catálogo, ficha e ferramentas, sem API

import json
from datetime import date

import pytest

from agent.empresa import ErroDePerfil, carregar_empresa
from agent.prompt import system_prompt
from agent.tools import Ferramentas, montar_ficha

HOJE = date(2026, 10, 25)


@pytest.fixture(scope="module")
def empresa():
    return carregar_empresa("anima-kids")


def novas(empresa, pedido=None):
    return Ferramentas(empresa=empresa, pedido=dict(pedido or {}), hoje=HOJE)


def test_perfil_carrega_e_tem_o_essencial(empresa):
    assert empresa.nome == "Anima Kids Park"
    assert empresa.nome_agente == "Anima Kids Park Bot"
    assert empresa.mostrar_precos is False
    assert empresa.tem_endereco is False
    assert "data" in empresa.campos_obrigatorios
    ofertaveis = {i["id"] for i in empresa.itens_ofertaveis()}
    assert "big-shark" in ofertaveis
    # sob consulta não aparece como oferta
    assert "pipoca" not in ofertaveis
    assert {"pipoca", "touro-mecanico"} <= {i["id"] for i in empresa.itens_sob_consulta()}


def test_empresa_inexistente_da_erro_claro():
    with pytest.raises(ErroDePerfil, match="não encontrada"):
        carregar_empresa("nao-existe")


def test_modelo_nao_pode_ser_usado_como_empresa():
    with pytest.raises(ErroDePerfil):
        carregar_empresa("_modelo")


def test_system_prompt_e_estavel_e_sem_data(empresa):
    # O cache da API depende de o texto ser idêntico entre chamadas
    assert system_prompt(empresa) == system_prompt(empresa)
    texto = system_prompt(empresa)
    assert "2026" not in texto
    assert "Anima Kids Park Bot" in texto
    assert "big-shark" in texto
    assert "NUNCA fala valores" in texto


def test_sugestao_para_bebes_nao_traz_brinquedo_de_adulto(empresa):
    f = novas(empresa)
    saida, erro = f.executar("sugerir_brinquedos", {"idade_min": 1, "idade_max": 3})
    assert not erro
    ids = [s["id"] for s in json.loads(saida)["sugestoes"]]
    assert ids
    assert "arena-play" not in ids and "futebol-de-sabao" not in ids


def test_sugestao_com_agua_so_traz_aquaticos(empresa):
    f = novas(empresa)
    saida, _ = f.executar("sugerir_brinquedos", {"idade_min": 6, "idade_max": 10, "quer_agua": "sim"})
    for s in json.loads(saida)["sugestoes"]:
        assert empresa.item(s["id"])["aquatico"] in ("sim", "opcional")


def test_sugestao_respeita_o_espaco(empresa):
    f = novas(empresa)
    # 5x5 não comporta a Aventura na Fazenda (precisa de 8,80 x 8,40)
    saida, _ = f.executar(
        "sugerir_brinquedos", {"idade_min": 2, "idade_max": 5, "comprimento_m": 5, "largura_m": 5}
    )
    assert "aventura-na-fazenda" not in [s["id"] for s in json.loads(saida)["sugestoes"]]


def test_sugestao_pelo_tema(empresa):
    f = novas(empresa)
    saida, _ = f.executar("sugerir_brinquedos", {"idade_min": 6, "idade_max": 9, "tema": "dino"})
    primeiro = json.loads(saida)["sugestoes"][0]
    assert "dino" in (primeiro["nome"] + primeiro["destaque"]).lower()


def test_anotar_pedido_valida_data(empresa):
    f = novas(empresa)
    _, erro = f.executar("anotar_pedido", {"data": "2026-11-15"})
    assert erro
    _, erro = f.executar("anotar_pedido", {"data": "10/10/2026"})
    assert erro, "data no passado tem que ser recusada"
    saida, erro = f.executar("anotar_pedido", {"data": "15/11/2026", "idades": "3 a 8"})
    assert not erro
    assert f.pedido["data"] == "15/11/2026"
    assert "Qual a data do evento?" not in json.loads(saida)["falta_perguntar"]


def test_anotar_pedido_separa_sob_consulta_e_ids_invalidos(empresa):
    f = novas(empresa)
    saida, _ = f.executar("anotar_pedido", {"brinquedos": ["big-shark", "pipoca", "nao-existe"]})
    dados = json.loads(saida)
    assert f.pedido["brinquedos"] == ["big-shark"]
    assert "Pipoca (carrinho com atendente)" in f.pedido["pedidos_sob_consulta"]
    assert any("nao-existe" in a for a in dados["avisos"])


def test_anotar_pedido_acumula(empresa):
    f = novas(empresa, {"brinquedos": ["big-shark"], "observacoes": "festa da Laura"})
    f.executar("anotar_pedido", {"brinquedos": ["arena-play"], "observacoes": "tem piscina"})
    assert f.pedido["brinquedos"] == ["big-shark", "arena-play"]
    assert "festa da Laura" in f.pedido["observacoes"] and "tem piscina" in f.pedido["observacoes"]


def test_ficha_completa_nao_falta_nada(empresa):
    completo = {
        "data": "15/11/2026", "horario": "14h às 18h", "cidade_bairro": "Ribeirão Preto, Jd. Botânico",
        "tipo_local": "condomínio", "convidados_criancas": 30, "idades": "3 a 10",
        "espaco": "10 x 8 m", "brinquedos": ["big-shark"],
    }
    assert novas(empresa, completo).faltando() == []


def test_enviar_foto(empresa):
    f = novas(empresa)
    _, erro = f.executar("enviar_foto", {"id": "big-shark"})
    assert not erro and f.fotos and f.fotos[0][0].startswith("https://")
    saida, _ = f.executar("enviar_foto", {"id": "dual-splash"})  # sem foto cadastrada
    assert json.loads(saida)["enviada"] is False
    _, erro = f.executar("enviar_foto", {"id": "pipoca"})
    assert erro


def test_ver_brinquedo_nao_mostra_preco(empresa):
    f = novas(empresa)
    ficha = json.loads(f.executar("ver_brinquedo", {"id": "aventura-na-fazenda"})[0])
    assert "preco" not in ficha
    assert ficha["capacidade"] == 4


def test_ferramenta_desconhecida(empresa):
    _, erro = novas(empresa).executar("reservar", {})
    assert erro


def test_montar_ficha(empresa):
    pedido = {"data": "15/11/2026", "brinquedos": ["big-shark"], "pedidos_sob_consulta": ["Pipoca"], "nome": "Mariana"}
    ficha = montar_ficha(empresa, "5516988887777", pedido, "pediu preço", "Quer festa na piscina.")
    assert "Mariana" in ficha and "Big Shark" in ficha
    assert "⚠️ Sob consulta: Pipoca" in ficha
    assert "#bot voltar 5516988887777" in ficha
