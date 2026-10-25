# agent/memory.py — Memória do agente
# Adaptado do AgentKit (CLAUDE.md §3.9), com as tabelas da locadora

"""
Guarda, por cliente:
  - o histórico da conversa
  - a ficha do pedido (o que a IA já anotou do evento)
  - o estado da conversa: o agente está atendendo ou a equipe assumiu?
  - o contexto do provedor (para conseguir mandar mensagem de volta)
E também quais eventos de webhook já foram atendidos (deduplicação).

SQLite no computador, PostgreSQL em produção.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite+aiosqlite:///./agentkit.db"

# A Railway entrega a URL do PostgreSQL como "postgresql://" (ou "postgres://").
# O SQLAlchemy assíncrono precisa do driver explícito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# Em produção o disco do contêiner é apagado a cada novo deploy: com SQLite, o agente
# esqueceria todos os clientes. Ele sobe mesmo assim, então o aviso precisa ser alto.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Produção com SQLite: o histórico vai ser apagado a cada deploy. "
        "Adicione PostgreSQL e configure DATABASE_URL."
    )

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Estados da conversa
COM_O_AGENTE = "agente"
COM_A_EQUIPE = "equipe"


def agora() -> datetime:
    """Hora atual em UTC, com fuso."""
    return datetime.now(timezone.utc)


def _com_fuso(momento: datetime | None) -> datetime | None:
    """O SQLite devolve datas sem fuso; aqui elas voltam a ser UTC."""
    if momento is not None and momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento


class Base(DeclarativeBase):
    pass


class Mensagem(Base):
    """Uma mensagem do histórico."""

    __tablename__ = "mensagens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefone: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" ou "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class EventoProcessado(Base):
    """
    Eventos de webhook já atendidos. Os provedores entregam "pelo menos uma vez": sem esta
    tabela, o cliente receberia a mesma resposta repetida.
    """

    __tablename__ = "eventos_processados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    criado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora, index=True)


class Conversa(Base):
    """Estado de cada cliente: quem está atendendo e como responder a ele."""

    __tablename__ = "conversas"

    telefone: Mapped[str] = mapped_column(String(64), primary_key=True)
    estado: Mapped[str] = mapped_column(String(20), default=COM_O_AGENTE)
    pausado_ate: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    motivo_pausa: Mapped[str] = mapped_column(Text, default="")
    # contexto do provedor em JSON (conversation_id, account_id) para mandar mensagem depois
    contexto: Mapped[str] = mapped_column(Text, default="{}")
    conversation_id: Mapped[str] = mapped_column(String(200), default="", index=True)
    atualizado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


class Pedido(Base):
    """A ficha do evento que a IA vai montando durante a conversa."""

    __tablename__ = "pedidos"

    telefone: Mapped[str] = mapped_column(String(64), primary_key=True)
    dados: Mapped[str] = mapped_column(Text, default="{}")
    enviado_para_equipe_em: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    motivo: Mapped[str] = mapped_column(Text, default="")
    atualizado_em: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=agora)


async def inicializar_db():
    """Cria as tabelas que ainda não existem."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── Deduplicação ─────────────────────────────────────────────────────────


async def marcar_evento_processado(evento_id: str) -> bool:
    """
    Registra um evento. True se é novo, False se já tinha sido processado.
    Quem garante a unicidade é a chave primária, então dois webhooks simultâneos não passam.
    """
    if not evento_id:
        return True  # sem id não dá para deduplicar: processa

    async with async_session() as session:
        session.add(EventoProcessado(evento_id=evento_id, criado_em=agora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Apaga a marca do evento para que o reenvio do provedor SEJA processado.
    Usado quando a resposta falhou depois do evento já marcado.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcessado).where(EventoProcessado.evento_id == evento_id))
        await session.commit()


async def limpar_eventos_antigos(dias: int = 7):
    """Apaga eventos com mais de N dias para a tabela não crescer sem fim."""
    limite = agora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcessado).where(EventoProcessado.criado_em < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"{resultado.rowcount} eventos com mais de {dias} dias apagados")


# ── Histórico ────────────────────────────────────────────────────────────


async def guardar_mensagem(telefone: str, role: str, content: str):
    """Guarda uma mensagem no histórico do cliente."""
    async with async_session() as session:
        session.add(Mensagem(telefone=telefone, role=role, content=content, timestamp=agora()))
        await session.commit()


async def obter_historico(telefone: str, limite: int = 30) -> list[dict]:
    """
    Últimas N mensagens, em ordem cronológica, prontas para a API do Claude.

    Ordena por id e não por horário: duas mensagens no mesmo instante teriam a ordem sorteada.
    Mensagens seguidas do mesmo lado (o cliente mandou três "oi" enquanto a equipe atendia)
    são juntadas numa só.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensagem)
            .where(Mensagem.telefone == telefone)
            .order_by(Mensagem.id.desc())
            .limit(limite)
        )
        mensagens = list(resultado.scalars().all())

    mensagens.reverse()

    historico: list[dict] = []
    for m in mensagens:
        if historico and historico[-1]["role"] == m.role:
            historico[-1]["content"] += "\n" + m.content
        else:
            historico.append({"role": m.role, "content": m.content})

    # A API do Claude exige que o histórico comece com o cliente
    while historico and historico[0]["role"] != "user":
        historico.pop(0)
    return historico


async def limpar_historico(telefone: str):
    """Apaga o histórico, a ficha e o estado de um cliente (usado nos testes)."""
    async with async_session() as session:
        await session.execute(delete(Mensagem).where(Mensagem.telefone == telefone))
        await session.execute(delete(Pedido).where(Pedido.telefone == telefone))
        await session.execute(delete(Conversa).where(Conversa.telefone == telefone))
        await session.commit()


# ── Estado da conversa ───────────────────────────────────────────────────


async def _obter_ou_criar_conversa(session: AsyncSession, telefone: str) -> Conversa:
    conversa = await session.get(Conversa, telefone)
    if conversa is None:
        conversa = Conversa(telefone=telefone, estado=COM_O_AGENTE, contexto="{}")
        session.add(conversa)
    return conversa


async def guardar_contexto(telefone: str, contexto: dict):
    """Guarda como responder a este cliente (Zernio precisa de conversation_id e account_id)."""
    util = {k: v for k, v in contexto.items() if k != "evento_id" and v}
    if not util:
        return
    async with async_session() as session:
        conversa = await _obter_ou_criar_conversa(session, telefone)
        conversa.contexto = json.dumps(util, ensure_ascii=False)
        conversa.conversation_id = util.get("conversation_id", "") or conversa.conversation_id or ""
        conversa.atualizado_em = agora()
        await session.commit()


async def obter_contexto(telefone: str) -> dict:
    """O contexto salvo do cliente, ou {} se nunca escreveu."""
    async with async_session() as session:
        conversa = await session.get(Conversa, telefone)
        return json.loads(conversa.contexto or "{}") if conversa else {}


async def telefone_da_conversa(conversation_id: str) -> str:
    """Descobre o cliente pelo conversation_id da Zernio."""
    if not conversation_id:
        return ""
    async with async_session() as session:
        resultado = await session.execute(
            select(Conversa.telefone).where(Conversa.conversation_id == conversation_id)
        )
        return resultado.scalars().first() or ""


async def pausar_agente(telefone: str, horas: float, motivo: str):
    """A equipe assumiu: o agente fica quieto com este cliente por algumas horas."""
    async with async_session() as session:
        conversa = await _obter_ou_criar_conversa(session, telefone)
        conversa.estado = COM_A_EQUIPE
        conversa.pausado_ate = agora() + timedelta(hours=horas)
        conversa.motivo_pausa = motivo
        conversa.atualizado_em = agora()
        await session.commit()


async def retomar_agente(telefone: str) -> bool:
    """O agente volta a atender o cliente. False se o cliente não existe."""
    async with async_session() as session:
        conversa = await session.get(Conversa, telefone)
        if conversa is None:
            return False
        conversa.estado = COM_O_AGENTE
        conversa.pausado_ate = None
        conversa.motivo_pausa = ""
        conversa.atualizado_em = agora()
        await session.commit()
        return True


async def agente_pausado(telefone: str) -> bool:
    """True se a equipe está com o cliente. A pausa vence sozinha no horário marcado."""
    async with async_session() as session:
        conversa = await session.get(Conversa, telefone)
        if conversa is None or conversa.estado != COM_A_EQUIPE:
            return False
        ate = _com_fuso(conversa.pausado_ate)
        if ate is not None and ate <= agora():
            conversa.estado = COM_O_AGENTE
            conversa.pausado_ate = None
            conversa.motivo_pausa = ""
            await session.commit()
            return False
        return True


async def listar_conversas_pausadas() -> list[dict]:
    """Para o painel: clientes que estão com a equipe agora."""
    async with async_session() as session:
        resultado = await session.execute(
            select(Conversa).where(Conversa.estado == COM_A_EQUIPE).order_by(Conversa.atualizado_em.desc())
        )
        return [
            {
                "telefone": c.telefone,
                "pausado_ate": _com_fuso(c.pausado_ate),
                "motivo": c.motivo_pausa,
            }
            for c in resultado.scalars().all()
        ]


# ── Ficha do pedido ──────────────────────────────────────────────────────


async def obter_pedido(telefone: str) -> dict:
    """A ficha do cliente, ou {} se ainda não tem."""
    async with async_session() as session:
        pedido = await session.get(Pedido, telefone)
        return json.loads(pedido.dados or "{}") if pedido else {}


async def guardar_pedido(telefone: str, dados: dict):
    """Substitui a ficha do cliente."""
    async with async_session() as session:
        pedido = await session.get(Pedido, telefone)
        if pedido is None:
            pedido = Pedido(telefone=telefone)
            session.add(pedido)
        pedido.dados = json.dumps(dados, ensure_ascii=False)
        pedido.atualizado_em = agora()
        await session.commit()


async def marcar_pedido_enviado(telefone: str, motivo: str):
    """Registra que a ficha foi passada para a equipe."""
    async with async_session() as session:
        pedido = await session.get(Pedido, telefone)
        if pedido is None:
            pedido = Pedido(telefone=telefone, dados="{}")
            session.add(pedido)
        pedido.enviado_para_equipe_em = agora()
        pedido.motivo = motivo
        pedido.atualizado_em = agora()
        await session.commit()


async def listar_pedidos(limite: int = 50) -> list[dict]:
    """Para o painel: as fichas mais recentes."""
    async with async_session() as session:
        resultado = await session.execute(
            select(Pedido).order_by(Pedido.atualizado_em.desc()).limit(limite)
        )
        return [
            {
                "telefone": p.telefone,
                "dados": json.loads(p.dados or "{}"),
                "enviado_em": _com_fuso(p.enviado_para_equipe_em),
                "motivo": p.motivo,
                "atualizado_em": _com_fuso(p.atualizado_em),
            }
            for p in resultado.scalars().all()
        ]
