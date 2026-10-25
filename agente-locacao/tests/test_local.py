# tests/test_local.py — Conversa com o agente no terminal (simula o WhatsApp)
# Adaptado do AgentKit (CLAUDE.md §3.11)

"""
Teste o agente sem WhatsApp. Usa a API do Claude de verdade (precisa do ANTHROPIC_API_KEY).

    python tests/test_local.py

Comandos: 'limpar' apaga a conversa, 'ficha' mostra a ficha, 'sair' termina.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent import memory  # noqa: E402
from agent.brain import gerar_resposta  # noqa: E402
from agent.empresa import empresa_atual  # noqa: E402
from agent.tools import montar_ficha  # noqa: E402

TELEFONE_TESTE = "5500000000000"


async def main():
    await memory.inicializar_db()
    empresa = empresa_atual()

    print("\n" + "=" * 60)
    print(f"   {empresa.nome_agente} — teste no terminal")
    print("=" * 60)
    print("  Escreva como se fosse um cliente no WhatsApp.")
    print("  Comandos: 'limpar' | 'ficha' | 'sair'\n" + "-" * 60 + "\n")

    while True:
        try:
            mensagem = input("Você: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTeste encerrado.")
            break

        if not mensagem:
            continue
        if mensagem.lower() == "sair":
            print("\nTeste encerrado.")
            break
        if mensagem.lower() == "limpar":
            await memory.limpar_historico(TELEFONE_TESTE)
            print("[conversa apagada]\n")
            continue
        if mensagem.lower() == "ficha":
            print(json.dumps(await memory.obter_pedido(TELEFONE_TESTE), ensure_ascii=False, indent=2), "\n")
            continue

        if await memory.agente_pausado(TELEFONE_TESTE):
            print("[a equipe está com este cliente; o agente não responde. 'limpar' recomeça]\n")
            continue

        historico = await memory.obter_historico(TELEFONE_TESTE)
        pedido = await memory.obter_pedido(TELEFONE_TESTE)
        resposta = await gerar_resposta(empresa, mensagem, historico, pedido)

        print(f"\n{empresa.nome_agente}: {resposta.texto}")
        for url, legenda in resposta.fotos:
            print(f"   📸 [foto] {legenda}: {url}")
        if resposta.ferramentas:
            print(f"   [ferramentas: {', '.join(resposta.ferramentas)}]")
        print()

        await memory.guardar_mensagem(TELEFONE_TESTE, "user", mensagem)
        if resposta.conversa_real:
            await memory.guardar_mensagem(TELEFONE_TESTE, "assistant", resposta.texto)
        await memory.guardar_pedido(TELEFONE_TESTE, resposta.pedido)

        if resposta.passar_para_equipe:
            await memory.pausar_agente(TELEFONE_TESTE, 12, resposta.motivo)
            print("-" * 60)
            print("FICHA QUE A EQUIPE RECEBERIA:\n")
            print(montar_ficha(empresa, TELEFONE_TESTE, resposta.pedido, resposta.motivo, resposta.resumo))
            print("-" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
