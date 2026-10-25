# tests/rodar_conversas.py — Roda a bateria de conversas com a API de verdade

"""
Roda cada cenário de tests/conversas.yaml com o Claude de verdade e confere as respostas
pelo código. Gasta API (poucos centavos por rodada com o modelo padrão).

    python tests/rodar_conversas.py              # todos os cenários
    python tests/rodar_conversas.py piscina      # só os que têm "piscina" no nome

Salva a transcrição de cada rodada em resultados/ (fora do git).
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from agent.brain import FUSO, gerar_resposta, problemas_do_texto  # noqa: E402
from agent.empresa import empresa_atual  # noqa: E402


async def rodar_cenario(empresa, cenario: dict) -> tuple[bool, list[str], list[dict]]:
    historico: list[dict] = []
    pedido: dict = {}
    falhas: list[str] = []
    transcricao: list[dict] = []
    passou_para_equipe = False

    for mensagem in cenario["mensagens"]:
        if passou_para_equipe:
            break  # depois da passagem, quem responde é a equipe
        r = await gerar_resposta(empresa, mensagem, historico, pedido, agora=datetime.now(FUSO))
        transcricao.append(
            {"cliente": mensagem, "agente": r.texto, "ferramentas": r.ferramentas, "fotos": [f[1] for f in r.fotos]}
        )
        for problema in problemas_do_texto(r.texto, empresa):
            falhas.append(f"resposta com problema: {problema}")
        historico += [{"role": "user", "content": mensagem}]
        if r.conversa_real:
            historico += [{"role": "assistant", "content": r.texto}]
        pedido = r.pedido
        passou_para_equipe = r.passar_para_equipe
        if r.passar_para_equipe and not r.conversa_real:
            falhas.append(f"caiu na mensagem de erro (motivo: {r.motivo})")

    esperado = cenario.get("deve_passar_para_equipe")
    if esperado is not None and passou_para_equipe != esperado:
        falhas.append(f"passar_para_equipe esperado {esperado}, veio {passou_para_equipe}")

    for campo in cenario.get("ficha_tem") or []:
        if not pedido.get(campo):
            falhas.append(f"ficha sem o campo '{campo}'")

    for item_id in cenario.get("brinquedos_proibidos") or []:
        if item_id in pedido.get("brinquedos", []):
            falhas.append(f"anotou brinquedo que não serve: {item_id}")
        nome = (empresa.item(item_id) or {}).get("nome", "").split(" (")[0].lower()
        if nome and any(nome in t["agente"].lower() for t in transcricao):
            falhas.append(f"sugeriu brinquedo que não serve: {nome}")

    palavras = cenario.get("texto_contem_algum")
    if palavras:
        tudo = " ".join(t["agente"].lower() for t in transcricao)
        if not any(p.lower() in tudo for p in palavras):
            falhas.append(f"nenhuma resposta menciona: {palavras}")

    transcricao.append({"ficha_final": pedido, "passou_para_equipe": passou_para_equipe})
    return not falhas, falhas, transcricao


async def main():
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Falta ANTHROPIC_API_KEY no .env: esta bateria usa a API de verdade.")
        sys.exit(1)

    empresa = empresa_atual()
    cenarios = yaml.safe_load(open(RAIZ / "tests" / "conversas.yaml", encoding="utf-8"))["cenarios"]
    filtro = " ".join(sys.argv[1:]).lower()
    if filtro:
        cenarios = [c for c in cenarios if filtro in c["nome"].lower()]

    pasta = RAIZ / "resultados"
    pasta.mkdir(exist_ok=True)
    relatorio = {}
    aprovados = 0

    for cenario in cenarios:
        ok, falhas, transcricao = await rodar_cenario(empresa, cenario)
        aprovados += ok
        relatorio[cenario["nome"]] = {"ok": ok, "falhas": falhas, "conversa": transcricao}
        print(f"{'✅' if ok else '❌'} {cenario['nome']}")
        for falha in falhas:
            print(f"     - {falha}")

    arquivo = pasta / f"conversas-{datetime.now():%Y%m%d-%H%M%S}.json"
    arquivo.write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{aprovados}/{len(cenarios)} cenários aprovados. Transcrição em {arquivo}")
    sys.exit(0 if aprovados == len(cenarios) else 1)


if __name__ == "__main__":
    asyncio.run(main())
