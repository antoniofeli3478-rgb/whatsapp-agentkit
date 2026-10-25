# agent/tools.py — Ferramentas do agente (o código calcula, a IA conversa)

"""
Diferente do molde do AgentKit, aqui as ferramentas SÃO chamadas pelo Claude (tool use).

Cinco ferramentas:
  sugerir_brinquedos   filtra o catálogo por idade, espaço e água
  ver_brinquedo        ficha de um brinquedo
  enviar_foto          coloca a foto na fila (o main.py envia depois do texto)
  anotar_pedido        salva os dados do evento na ficha e diz o que falta
  passar_para_equipe   encerra a parte da IA: a ficha vai para a equipe e o agente pausa

Nenhuma ferramenta reserva, confirma ou cobra: isso é da equipe.
"""

import json
from dataclasses import dataclass, field
from datetime import date, datetime

from agent.empresa import Empresa

MAX_FOTOS_POR_MENSAGEM = 3
MAX_SUGESTOES = 5

# Campos de texto que a ficha aceita (os da checklist + alguns extras)
CAMPOS_TEXTO = (
    "horario",
    "cidade_bairro",
    "tipo_local",
    "idades",
    "espaco",
    "piso",
    "coberto",
    "energia_agua",
    "acesso",
    "nome",
)

DEFINICOES = [
    {
        "name": "sugerir_brinquedos",
        "description": (
            "Filtra o catálogo e devolve os brinquedos que combinam com a festa, com o motivo. "
            "Use antes de recomendar qualquer brinquedo. Passe só o que o cliente já contou; "
            "o que não souber, deixe de fora."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "idade_min": {"type": "integer", "description": "Idade da criança mais nova"},
                "idade_max": {"type": "integer", "description": "Idade da criança mais velha"},
                "comprimento_m": {"type": "number", "description": "Comprimento livre do espaço, em metros"},
                "largura_m": {"type": "number", "description": "Largura livre do espaço, em metros"},
                "coberto": {"type": "boolean", "description": "O local é coberto?"},
                "altura_teto_m": {"type": "number", "description": "Altura do teto, se coberto"},
                "quer_agua": {
                    "type": "string",
                    "enum": ["sim", "nao", "tanto_faz"],
                    "description": "O cliente quer brinquedo com água?",
                },
                "tema": {"type": "string", "description": "Tema da festa, se houver (ex.: dinossauro, fazenda)"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "ver_brinquedo",
        "description": "Devolve a ficha completa de um brinquedo do catálogo pelo id.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "id do brinquedo no catálogo"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "enviar_foto",
        "description": (
            "Manda a foto real de um brinquedo para o cliente, logo depois da sua mensagem. "
            "Use quando o cliente quiser ver o brinquedo. Não cole links na sua mensagem."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "id do brinquedo no catálogo"}},
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "anotar_pedido",
        "description": (
            "Salva na ficha os dados do evento que o cliente informou. Chame sempre que surgir "
            "um dado novo, mesmo que seja um só. Devolve a ficha e o que ainda falta perguntar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "Data do evento no formato DD/MM/AAAA"},
                "horario": {"type": "string", "description": "Horário de início e fim, ex.: 14h às 18h"},
                "duracao_horas": {"type": "number"},
                "cidade_bairro": {"type": "string"},
                "tipo_local": {"type": "string", "description": "casa, condomínio, chácara, salão, escola..."},
                "convidados_criancas": {"type": "integer", "description": "Número aproximado de crianças"},
                "idades": {"type": "string", "description": "Faixa de idade, ex.: 3 a 8 anos"},
                "espaco": {"type": "string", "description": "Medidas ou descrição do espaço"},
                "piso": {"type": "string"},
                "coberto": {"type": "string", "description": "sim/não e altura do teto"},
                "energia_agua": {"type": "string"},
                "acesso": {"type": "string"},
                "nome": {"type": "string", "description": "Nome do cliente"},
                "brinquedos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ids do catálogo que o cliente quer ou gostou",
                },
                "pedidos_sob_consulta": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "O que o cliente pediu que é sob consulta (ex.: pipoca)",
                },
                "observacoes": {"type": "string", "description": "Qualquer detalhe útil para a equipe"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "passar_para_equipe",
        "description": (
            "Passa a conversa para a equipe humana, com a ficha do pedido. Use quando a ficha "
            "estiver completa ou o cliente pedir preço, desconto, reserva, uma pessoa, reclamar, "
            "desconfiar de golpe ou pedir algo sob consulta. Depois disso o sistema manda uma "
            "mensagem padrão ao cliente e você para de responder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Por que está passando (ex.: ficha completa, pediu preço, pediu pipoca)",
                },
                "resumo": {
                    "type": "string",
                    "description": "2 ou 3 linhas para a equipe: o que o cliente quer e o clima da conversa",
                },
            },
            "required": ["motivo", "resumo"],
            "additionalProperties": False,
        },
    },
]


def _resultado(**dados) -> str:
    return json.dumps(dados, ensure_ascii=False)


def _erro(mensagem: str) -> str:
    return json.dumps({"erro": mensagem}, ensure_ascii=False)


def _cabe(medidas: list | None, comprimento: float | None, largura: float | None) -> bool | None:
    """True/False se dá para saber se cabe; None se falta medida do brinquedo ou do espaço."""
    if not medidas or comprimento is None or largura is None:
        return None
    c, l = medidas[0], medidas[1]
    if c is None or l is None:
        return None
    return (c <= comprimento and l <= largura) or (c <= largura and l <= comprimento)


@dataclass
class Ferramentas:
    """Executa as ferramentas de um turno, guardando o que o main.py precisa fazer depois."""

    empresa: Empresa
    pedido: dict
    hoje: date
    fotos: list[tuple[str, str]] = field(default_factory=list)  # (url, legenda)
    passar_para_equipe: bool = False
    motivo_passagem: str = ""
    resumo_passagem: str = ""
    usadas: list[str] = field(default_factory=list)

    def executar(self, nome: str, entrada: dict) -> tuple[str, bool]:
        """Roda uma ferramenta. Retorna (resultado em texto, é_erro)."""
        self.usadas.append(nome)
        metodo = getattr(self, f"_ferramenta_{nome}", None)
        if metodo is None:
            return _erro(f"Ferramenta desconhecida: {nome}"), True
        try:
            resultado = metodo(entrada or {})
        except (TypeError, ValueError) as e:
            return _erro(f"Entrada inválida: {e}"), True
        return resultado, resultado.startswith('{"erro"')

    # ── sugerir_brinquedos ───────────────────────────────────────────────

    def _ferramenta_sugerir_brinquedos(self, e: dict) -> str:
        idade_min = e.get("idade_min")
        idade_max = e.get("idade_max", idade_min)
        if idade_min is None and idade_max is not None:
            idade_min = idade_max
        comprimento = e.get("comprimento_m")
        largura = e.get("largura_m")
        teto = e.get("altura_teto_m") if e.get("coberto") else None
        quer_agua = e.get("quer_agua", "tanto_faz")
        tema = (e.get("tema") or "").lower().strip()

        candidatos = []
        for item in self.empresa.itens_ofertaveis():
            motivos, pontos = [], 0

            faixa = item.get("idade")
            if idade_min is not None and faixa:
                if faixa[0] > idade_max or faixa[1] < idade_min:
                    continue  # nenhuma criança da festa está na faixa do brinquedo
                cobre_todas = faixa[0] <= idade_min and faixa[1] >= idade_max
                pontos += 3 if cobre_todas else 1
                faixa_txt = f"{faixa[0]}+" if faixa[1] >= 99 else f"{faixa[0]} a {faixa[1]}"
                motivos.append(
                    f"faixa sugerida {faixa_txt} anos"
                    + ("" if cobre_todas else " (serve para parte das crianças)")
                )

            aquatico = item.get("aquatico", "nao")
            if quer_agua == "sim":
                if aquatico == "nao":
                    continue
                pontos += 2
                motivos.append("tem opção com água")
            elif quer_agua == "nao" and aquatico == "sim":
                continue

            medidas_montagem = item.get("area_m") or item.get("medidas_m")
            cabe = _cabe(medidas_montagem, comprimento, largura)
            if cabe is False:
                continue
            if cabe:
                pontos += 2
                motivos.append("cabe nas medidas informadas")
            elif comprimento is not None:
                motivos.append("a equipe confirma se cabe no espaço")

            altura = (item.get("area_m") or item.get("medidas_m") or [None, None, None])[2]
            if teto is not None and altura is not None and altura > teto:
                continue

            if tema and (tema in item["nome"].lower() or tema in (item.get("destaque") or "").lower()):
                pontos += 4
                motivos.append(f"combina com o tema {tema}")

            candidatos.append((pontos, item, motivos))

        candidatos.sort(key=lambda c: (-c[0], c[1]["nome"]))
        sugestoes = [
            {
                "id": item["id"],
                "nome": item["nome"],
                "destaque": (item.get("destaque") or "").strip(),
                "por_que": "; ".join(motivos) or "opção do catálogo",
                "tem_foto": bool(item.get("fotos")),
            }
            for _, item, motivos in candidatos[:MAX_SUGESTOES]
        ]
        if not sugestoes:
            return _resultado(
                sugestoes=[],
                aviso="Nada no catálogo bate com esses filtros. Anote o pedido e passe para a equipe avaliar.",
            )
        return _resultado(
            sugestoes=sugestoes,
            lembrete="Recomende 2 ou 3, explique o porquê e lembre que a equipe confirma espaço e agenda.",
        )

    # ── ver_brinquedo ────────────────────────────────────────────────────

    def _ferramenta_ver_brinquedo(self, e: dict) -> str:
        item = self.empresa.item(str(e.get("id", "")))
        if item is None or not item.get("ativo", True):
            return _erro("Esse id não existe no catálogo. Use um id da lista do catálogo.")
        if item.get("sob_consulta"):
            return _resultado(
                id=item["id"],
                nome=item["nome"],
                sob_consulta=True,
                instrucao="Não ofereça. Anote em pedidos_sob_consulta e diga que a equipe avalia.",
            )
        ficha = {
            chave: item.get(chave)
            for chave in ("id", "nome", "linha", "idade", "aquatico", "medidas_m", "area_m", "capacidade", "destaque")
            if item.get(chave) not in (None, "", [])
        }
        ficha["tem_foto"] = bool(item.get("fotos"))
        if self.empresa.mostrar_precos and item.get("preco") is not None:
            ficha["preco"] = item["preco"]
        ficha["observacao"] = "O que não está aqui, a equipe confirma. Não invente medidas nem preço."
        return json.dumps(ficha, ensure_ascii=False)

    # ── enviar_foto ──────────────────────────────────────────────────────

    def _ferramenta_enviar_foto(self, e: dict) -> str:
        item = self.empresa.item(str(e.get("id", "")))
        if item is None or not item.get("ativo", True) or item.get("sob_consulta"):
            return _erro("Esse id não existe no catálogo.")
        fotos = item.get("fotos") or []
        if not fotos:
            return _resultado(
                enviada=False,
                aviso="Esse brinquedo ainda não tem foto cadastrada. Diga que a equipe pode mandar.",
            )
        if len(self.fotos) >= MAX_FOTOS_POR_MENSAGEM:
            return _resultado(enviada=False, aviso="Limite de fotos desta mensagem atingido.")
        self.fotos.append((fotos[0], item["nome"]))
        return _resultado(
            enviada=True,
            aviso="A foto vai logo depois da sua mensagem. Não cole o link no texto.",
        )

    # ── anotar_pedido ────────────────────────────────────────────────────

    def _ferramenta_anotar_pedido(self, e: dict) -> str:
        avisos = []

        if e.get("data"):
            try:
                dia = datetime.strptime(str(e["data"]).strip(), "%d/%m/%Y").date()
            except ValueError:
                return _erro("Data inválida. Use DD/MM/AAAA, convertendo pelo calendário do contexto.")
            if dia < self.hoje:
                return _erro(f"A data {dia:%d/%m/%Y} já passou. Confirme a data com o cliente.")
            self.pedido["data"] = f"{dia:%d/%m/%Y}"

        for campo in CAMPOS_TEXTO:
            valor = e.get(campo)
            if valor not in (None, ""):
                self.pedido[campo] = str(valor).strip()

        if e.get("duracao_horas") is not None:
            self.pedido["duracao_horas"] = float(e["duracao_horas"])
        if e.get("convidados_criancas") is not None:
            self.pedido["convidados_criancas"] = int(e["convidados_criancas"])

        if e.get("brinquedos"):
            atuais = list(self.pedido.get("brinquedos", []))
            for item_id in e["brinquedos"]:
                item = self.empresa.item(str(item_id))
                if item is None or not item.get("ativo", True):
                    avisos.append(f"'{item_id}' não existe no catálogo, não foi anotado")
                    continue
                if item.get("sob_consulta"):
                    sob = self.pedido.setdefault("pedidos_sob_consulta", [])
                    if item["nome"] not in sob:
                        sob.append(item["nome"])
                    continue
                if item_id not in atuais:
                    atuais.append(item_id)
            self.pedido["brinquedos"] = atuais

        if e.get("pedidos_sob_consulta"):
            sob = self.pedido.setdefault("pedidos_sob_consulta", [])
            for pedido in e["pedidos_sob_consulta"]:
                if pedido and pedido not in sob:
                    sob.append(str(pedido))

        if e.get("observacoes"):
            anteriores = self.pedido.get("observacoes", "")
            nova = str(e["observacoes"]).strip()
            if nova not in anteriores:
                self.pedido["observacoes"] = f"{anteriores}; {nova}".strip("; ")

        return _resultado(
            ficha=self.pedido,
            falta_perguntar=self.faltando(),
            avisos=avisos,
            lembrete="Pergunte no máximo 2 coisas por vez. Ficha completa: chame passar_para_equipe.",
        )

    def faltando(self) -> list[str]:
        """Perguntas obrigatórias da checklist que ainda não têm resposta."""
        return [
            c["pergunta"]
            for c in self.empresa.checklist
            if c.get("obrigatorio") and not self.pedido.get(c["campo"])
        ]

    # ── passar_para_equipe ───────────────────────────────────────────────

    def _ferramenta_passar_para_equipe(self, e: dict) -> str:
        self.passar_para_equipe = True
        self.motivo_passagem = str(e.get("motivo", "")).strip()
        self.resumo_passagem = str(e.get("resumo", "")).strip()
        return _resultado(
            ok=True,
            aviso="Feito. O sistema manda a mensagem padrão ao cliente. Não escreva mais nada.",
        )


def montar_ficha(empresa: Empresa, telefone: str, pedido: dict, motivo: str, resumo: str) -> str:
    """O texto que a equipe recebe. Escrito pelo código, para sair sempre igual e completo."""
    nomes = []
    for item_id in pedido.get("brinquedos", []):
        item = empresa.item(item_id)
        nomes.append(item["nome"] if item else item_id)

    linhas = [f"🎉 NOVO ATENDIMENTO, {empresa.nome}", f"Cliente: {pedido.get('nome') or 'sem nome'} (+{telefone})"]
    if motivo:
        linhas.append(f"Motivo: {motivo}")
    campos = [
        ("Data", "data"),
        ("Horário", "horario"),
        ("Duração (h)", "duracao_horas"),
        ("Local", "cidade_bairro"),
        ("Tipo de local", "tipo_local"),
        ("Crianças", "convidados_criancas"),
        ("Idades", "idades"),
        ("Espaço", "espaco"),
        ("Piso", "piso"),
        ("Coberto", "coberto"),
        ("Energia/água", "energia_agua"),
        ("Acesso", "acesso"),
    ]
    for rotulo, chave in campos:
        if pedido.get(chave) not in (None, ""):
            linhas.append(f"{rotulo}: {pedido[chave]}")
    if nomes:
        linhas.append(f"Interesse: {', '.join(nomes)}")
    if pedido.get("pedidos_sob_consulta"):
        linhas.append(f"⚠️ Sob consulta: {', '.join(pedido['pedidos_sob_consulta'])}")
    if pedido.get("observacoes"):
        linhas.append(f"Obs.: {pedido['observacoes']}")
    if resumo:
        linhas.append(f"Resumo da IA: {resumo}")
    linhas.append("O agente está pausado com este cliente. Para ele voltar: #bot voltar " + telefone)
    return "\n".join(linhas)
