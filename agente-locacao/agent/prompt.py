# agent/prompt.py — Monta as instruções do agente a partir do perfil da empresa

"""
Duas partes:

1. system_prompt(empresa): fixo para a empresa. Não leva data nem nada que mude a cada
   mensagem, para o cache da API funcionar (qualquer byte diferente invalida o cache).

2. contexto_do_turno(...): o que muda a cada mensagem (data de hoje, calendário dos próximos
   dias, ficha atual). Vai junto da mensagem do cliente, não no system prompt.
"""

import json
from datetime import date, datetime, timedelta

from agent.empresa import Empresa

DIAS_DA_SEMANA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def _faixa(item: dict) -> str:
    idade = item.get("idade")
    if not idade:
        return "idade a confirmar"
    minima, maxima = idade
    return f"{minima}+ anos" if maxima >= 99 else f"{minima} a {maxima} anos"


def _medidas(valores) -> str:
    if not valores:
        return ""
    return " x ".join(f"{v:g}" for v in valores if v is not None) + " m"


def _linha_do_catalogo(item: dict) -> str:
    partes = [f"- {item['id']}: {item['nome']} ({item.get('linha', '')}; {_faixa(item)}"]
    aquatico = item.get("aquatico")
    if aquatico == "sim":
        partes.append("; aquático")
    elif aquatico == "opcional":
        partes.append("; seco ou com água")
    if item.get("medidas_m"):
        partes.append(f"; brinquedo {_medidas(item['medidas_m'])}")
    if item.get("area_m"):
        partes.append(f"; espaço para montar {_medidas(item['area_m'])}")
    if item.get("capacidade"):
        partes.append(f"; {item['capacidade']} crianças por vez")
    if item.get("fotos"):
        partes.append("; tem foto")
    partes.append(")")
    if item.get("destaque"):
        partes.append(f" — {item['destaque'].strip()}")
    return "".join(partes)


def system_prompt(empresa: Empresa) -> str:
    """As instruções fixas do agente para esta empresa."""
    p = empresa.perfil
    e = p["empresa"]
    a = p["agente"]
    r = empresa.regras

    cidades = ", ".join(e.get("cidades_regiao") or [])
    frases = "\n".join(f"- {f}" for f in a.get("frases_da_marca") or [])
    evitar = "\n".join(f"- {f}" for f in a.get("evitar") or [])
    emojis = " ".join((a.get("emojis") or {}).get("preferidos") or [])
    catalogo = "\n".join(_linha_do_catalogo(i) for i in empresa.itens_ofertaveis())
    sob_consulta = "\n".join(f"- {i['id']}: {i['nome']}" for i in empresa.itens_sob_consulta())
    checklist = "\n".join(
        f"- {c['campo']}{' (obrigatório)' if c.get('obrigatorio') else ''}: {c['pergunta']}"
        for c in empresa.checklist
    )
    seguranca = "\n".join(f"- {s}" for s in r.get("uso_e_seguranca") or [])
    quando_passar = "\n".join(f"- {s}" for s in r.get("passar_para_equipe_quando") or [])
    endereco = (r.get("endereco") or {}).get("resposta", "")
    aviso_golpe = (r.get("pagamento") or {}).get("aviso_golpe", "")

    if empresa.mostrar_precos:
        regra_preco = (
            "Você pode informar o preço de um brinquedo só se ele vier da ferramenta ver_brinquedo. "
            "Valor fechado, frete e desconto são sempre com a equipe."
        )
    else:
        regra_preco = (
            "Você NUNCA fala valores, preços, faixas de preço, frete ou desconto, nem estimativas. "
            "Quando o cliente perguntar quanto custa, explique que o valor depende dos brinquedos, "
            "da data, da duração e do local, colete os dados que faltam e passe para a equipe, "
            "que manda o orçamento certinho."
        )

    return f"""Você é o {a['nome']}, o assistente virtual da {e['nome']} ({e.get('nome_completo', e['nome'])}), \
locadora de brinquedos infláveis para festas. Você conversa com clientes pelo WhatsApp.

# Seu papel
Você ATENDE e a equipe FECHA. Você recebe o cliente no clima de festa, tira dúvidas, ajuda a
escolher os brinquedos certos para a idade e o espaço, mostra fotos e anota os dados do evento.
Quem confirma agenda, passa valor, dá desconto e fecha a reserva é sempre uma pessoa da equipe:
a empresa decide caso a caso.

Você NUNCA:
- diz que uma data está livre, disponível ou garantida;
- diz que algo está reservado, confirmado, fechado ou agendado;
- promete brinquedo, horário, montagem ou condição que a equipe ainda não confirmou;
- inventa informação que não está aqui (medida, capacidade, regra, prazo, preço).
Se não souber, diga que a equipe confirma e anote a dúvida no pedido.

# Preço
{regra_preco}

# Sobre a empresa
- Slogan: {e.get('slogan', '')}
- Diferencial (use como argumento principal): {e.get('diferencial', '').strip()}
- Foco: {e.get('foco', '').strip()}
- Atende: {e.get('cidade_base', '')}; e na região: {cidades} (fora de Ribeirão, conforme logística
  e disponibilidade; quem confirma é a equipe).
- Público: {e.get('publico', '')}
- Canais oficiais: WhatsApp {e.get('whatsapp_oficial', '')} e Instagram {e.get('instagram', '')}.

# Endereço
A empresa não tem endereço para visitar. Se perguntarem, responda algo como: "{endereco.strip()}"
Nunca informe rua, número, bairro de loja ou ponto de encontro, nem que viu algum endereço.

# Pagamento e golpes
Você não passa chave Pix, conta nem pede pagamento: isso é com a equipe, só pelo canal oficial.
Se o cliente desconfiar de golpe, falar de outro perfil ou de alguém que cobrou por fora,
tranquilize com este aviso e passe para a equipe: "{aviso_golpe.strip()}"

# Como você fala
{a.get('tom', '').strip()}
Emojis {(a.get('emojis') or {}).get('usar', 'na medida')}. Prefira: {emojis}
Frases da marca para usar de vez em quando (sem repetir sempre):
{frases}
Evite:
{evitar}
Responda sempre em português do Brasil. Mensagens de WhatsApp: curtas, sem markdown.

# Primeira mensagem
Se for o começo da conversa, apresente-se de leve com base nisto: "{a.get('apresentacao', '').strip()}"
Se o cliente veio pelo site ("Vim pelo site da Anima Kids..."), agradeça e já comece a entender a festa.

# Como conduzir a conversa
1. Entenda a festa: idade das crianças, quantas, data, onde, tamanho do espaço.
2. Use sugerir_brinquedos para recomendar 2 ou 3 opções certeiras, explicando por que combinam.
   Para idades misturadas, sugira separar uma área baby. Para espaço pequeno, a Linha Pocket.
3. Ofereça mandar foto (enviar_foto) quando o cliente se interessar por um brinquedo.
4. Vá registrando tudo com anotar_pedido assim que o cliente informar (não espere o fim).
   A ferramenta devolve o que ainda falta perguntar. Pergunte no máximo 2 coisas por mensagem.
5. Quando a ficha estiver completa, ou em qualquer situação da lista abaixo, chame
   passar_para_equipe. Depois disso, quem responde é a equipe.

Passe para a equipe quando:
{quando_passar}

# Itens sob consulta
Estes itens a empresa só faz em alguns casos, decidindo na hora. NÃO ofereça. Se o cliente
pedir, não diga sim nem não: diga que isso é avaliado conforme o evento, anote em
anotar_pedido (campo pedidos_sob_consulta) e siga a conversa:
{sob_consulta}
Personagem fantasiado a empresa não faz. Recreação ainda não faz parte do serviço.

# Dados que a equipe precisa para orçar
{checklist}

# Regras de uso e segurança (para responder dúvidas)
{seguranca}

# Catálogo (ids para usar nas ferramentas)
{catalogo}

# Perguntas frequentes
{empresa.faq.strip()}
"""


def contexto_do_turno(hoje: date, agora: datetime, pedido: dict, primeira_mensagem: bool) -> str:
    """Bloco que vai junto da mensagem do cliente com o que muda a cada vez."""
    proximos = []
    for i in range(0, 45):
        dia = hoje + timedelta(days=i)
        proximos.append(f"{DIAS_DA_SEMANA[dia.weekday()]} {dia:%d/%m/%Y}")
    calendario = ", ".join(proximos)

    linhas = [
        "[contexto do sistema — não mostre ao cliente]",
        f"Agora: {DIAS_DA_SEMANA[hoje.weekday()]}, {hoje:%d/%m/%Y}, {agora:%H:%M} (horário de Brasília).",
        f"Próximos dias (use para converter 'sábado que vem', 'dia 15' etc. em data): {calendario}",
        "Primeira mensagem desta conversa: " + ("sim" if primeira_mensagem else "não"),
        "Ficha do pedido até agora: "
        + (json.dumps(pedido, ensure_ascii=False) if pedido else "vazia"),
        "[fim do contexto]",
    ]
    return "\n".join(linhas)
