# agent/empresa.py — Carrega o perfil da empresa (empresas/<slug>/)

"""
Cada empresa é uma pasta em empresas/ com quatro arquivos:
  perfil.yaml    quem é o agente e como ele fala
  catalogo.yaml  os brinquedos
  regras.yaml    o que pode e não pode, checklist do orçamento, pagamento
  faq.md         perguntas frequentes

A variável EMPRESA do .env escolhe qual pasta usar. Para cadastrar uma empresa nova, copie
empresas/_modelo/ e preencha.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

PASTA_EMPRESAS = Path(__file__).resolve().parent.parent / "empresas"


class ErroDePerfil(ValueError):
    """O perfil da empresa está faltando ou com erro."""


@dataclass
class Empresa:
    slug: str
    perfil: dict
    catalogo: list[dict]
    regras: dict
    faq: str
    _por_id: dict = field(default_factory=dict, repr=False)

    # ── Atalhos usados pelo resto do código ───────────────────────────────

    @property
    def nome(self) -> str:
        return self.perfil["empresa"]["nome"]

    @property
    def nome_agente(self) -> str:
        return self.perfil["agente"]["nome"]

    @property
    def mostrar_precos(self) -> bool:
        return bool(self.regras.get("atendimento", {}).get("mostrar_precos", False))

    @property
    def tem_endereco(self) -> bool:
        return bool(self.regras.get("endereco", {}).get("tem_endereco", False))

    @property
    def checklist(self) -> list[dict]:
        return self.regras.get("checklist_orcamento", [])

    @property
    def campos_obrigatorios(self) -> list[str]:
        return [c["campo"] for c in self.checklist if c.get("obrigatorio")]

    def item(self, item_id: str) -> dict | None:
        return self._por_id.get(item_id)

    def itens_ofertaveis(self) -> list[dict]:
        """Itens que o agente pode sugerir: ativos e que não são sob consulta."""
        return [i for i in self.catalogo if i.get("ativo", True) and not i.get("sob_consulta")]

    def itens_sob_consulta(self) -> list[dict]:
        return [i for i in self.catalogo if i.get("ativo", True) and i.get("sob_consulta")]


def _ler_yaml(caminho: Path) -> dict:
    try:
        with open(caminho, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError as e:
        raise ErroDePerfil(f"Arquivo não encontrado: {caminho}") from e
    except yaml.YAMLError as e:
        raise ErroDePerfil(f"Erro de formato em {caminho.name}: {e}") from e


def carregar_empresa(slug: str) -> Empresa:
    """Lê e valida a pasta da empresa. Levanta ErroDePerfil com uma mensagem clara."""
    pasta = PASTA_EMPRESAS / slug
    if not slug or slug.startswith("_") or not pasta.is_dir():
        disponiveis = sorted(
            p.name for p in PASTA_EMPRESAS.iterdir() if p.is_dir() and not p.name.startswith("_")
        )
        raise ErroDePerfil(
            f"Empresa '{slug}' não encontrada em empresas/. Disponíveis: {', '.join(disponiveis)}"
        )

    perfil = _ler_yaml(pasta / "perfil.yaml")
    regras = _ler_yaml(pasta / "regras.yaml")
    catalogo = _ler_yaml(pasta / "catalogo.yaml").get("itens") or []
    caminho_faq = pasta / "faq.md"
    faq = caminho_faq.read_text(encoding="utf-8") if caminho_faq.exists() else ""

    for chave in ("empresa", "agente", "mensagem_passagem"):
        if chave not in perfil:
            raise ErroDePerfil(f"perfil.yaml de '{slug}' sem a chave '{chave}'")
    for chave in ("nome",):
        if not perfil["empresa"].get(chave) or not perfil["agente"].get(chave):
            raise ErroDePerfil(f"perfil.yaml de '{slug}' sem '{chave}' em empresa ou agente")

    por_id: dict[str, dict] = {}
    for item in catalogo:
        item_id = item.get("id")
        if not item_id or not item.get("nome"):
            raise ErroDePerfil(f"catalogo.yaml de '{slug}': item sem id ou nome: {item}")
        if item_id in por_id:
            raise ErroDePerfil(f"catalogo.yaml de '{slug}': id repetido '{item_id}'")
        por_id[item_id] = item

    return Empresa(
        slug=slug, perfil=perfil, catalogo=catalogo, regras=regras, faq=faq, _por_id=por_id
    )


@lru_cache(maxsize=1)
def empresa_atual() -> Empresa:
    """A empresa configurada em EMPRESA (lida uma vez por processo)."""
    return carregar_empresa((os.getenv("EMPRESA") or "").strip())
