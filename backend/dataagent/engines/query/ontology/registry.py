"""版本发布、分析绑定及权威语义执行记录。所有写操作在调用方事务内完成。"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID

import yaml
from sqlalchemy import BigInteger, DateTime, Index, String, Text, func, select, text
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from dataagent.engines.query.ontology.recurrence import handler_digest

KEY = "manufacturing_service"
CONFIG = (
    Path(__file__).resolve().parents[4]
    / "conf"
    / "ontology"
    / "manufacturing_service.yaml"
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


class OntologyBase(DeclarativeBase):
    """本体定义、绑定及执行记录统一存入元数据库。"""


class OntologyDefinition(OntologyBase):
    __tablename__ = "ontology_definitions"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str]
    revoked_reason: Mapped[str | None]
    content_hash: Mapped[str]
    definition: Mapped[dict] = mapped_column(JSONB)
    actor: Mapped[str]
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        Index(
            "uq_ontology_active",
            "key",
            unique=True,
            postgresql_where=text("status='published'"),
        ),
    )


class AnalysisOntology(OntologyBase):
    __tablename__ = "analysis_ontology_bindings"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(primary_key=True)
    analysis_id: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    revision: Mapped[int]
    content_hash: Mapped[str]

    dependency_hash: Mapped[str]


class OntologyExecution(OntologyBase):
    __tablename__ = "ontology_executions"
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    conversation_id: Mapped[UUID]
    analysis_id: Mapped[str]
    manifest: Mapped[dict] = mapped_column(JSONB)
    evidence: Mapped[dict] = mapped_column(JSONB)
    sql: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OntologyExperience(OntologyBase):
    __tablename__ = "ontology_query_experiences"
    role_name: Mapped[str] = mapped_column(primary_key=True)
    access_hash: Mapped[str] = mapped_column(primary_key=True)
    operation: Mapped[str] = mapped_column(primary_key=True)
    contract_hash: Mapped[str] = mapped_column(primary_key=True)
    template_hash: Mapped[str] = mapped_column(primary_key=True)
    execution_id: Mapped[UUID]
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


def load_definition():
    document = yaml.safe_load(CONFIG.read_text("utf8"))
    return {
        **document,
        "handler_hash": handler_digest(),
        "products": json.loads(
            CONFIG.with_name("neware_products.json").read_text("utf8")
        ),
    }


async def publish(session, definition, actor):
    """V1 处理器绑定已实现的映射。变动处理器或映射必须重新测试并增加 revision。"""
    reference = load_definition()
    revision = definition.get("revision")
    if type(revision) is not int or revision < 1 or not actor.strip():
        raise ValueError("发布缺少版本或操作者")
    if {**definition, "revision": reference["revision"]} != reference:
        raise ValueError("定义与已注册处理器、映射或分类版本不一致")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": KEY}
    )
    fingerprint = digest(definition)
    old = await session.get(OntologyDefinition, (KEY, revision))
    if old:
        if (
            old.status == "published"
            and not old.revoked_reason
            and old.content_hash == fingerprint
        ):
            return old
        raise ValueError("已使用的版本不可覆盖或重新发布")
    current = await session.scalar(
        select(OntologyDefinition)
        .where(OntologyDefinition.key == KEY)
        .order_by(OntologyDefinition.revision.desc())
        .limit(1)
    )
    if current:
        if revision <= current.revision:
            raise ValueError("revision 必须递增")
        current.status = "retired"
        await session.flush()
    row = OntologyDefinition(
        key=KEY,
        revision=revision,
        status="published",
        content_hash=fingerprint,
        definition=definition,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def revoke(session, revision, reason):
    if not reason.strip():
        raise ValueError("撤销需提供原因")
    row = await session.get(OntologyDefinition, (KEY, revision), with_for_update=True)
    if not row:
        raise ValueError("找不到版本")
    row.revoked_reason = reason
    row.status = "retired"


async def binding(session, user_id, conversation_id, analysis_id, dependency_hash):
    ids = (user_id, conversation_id, analysis_id, KEY)
    bound = await session.get(AnalysisOntology, ids)
    if bound is None:
        current = await session.scalar(
            select(OntologyDefinition)
            .where(
                OntologyDefinition.key == KEY, OntologyDefinition.status == "published"
            )
            .with_for_update(read=True)
        )
        if not current or current.revoked_reason:
            raise ValueError("当前没有可用的已发布规则")
        await session.execute(
            insert(AnalysisOntology)
            .values(
                user_id=user_id,
                conversation_id=conversation_id,
                analysis_id=analysis_id,
                key=KEY,
                revision=current.revision,
                content_hash=current.content_hash,
                dependency_hash=dependency_hash,
            )
            .on_conflict_do_nothing()
        )
        bound = await session.get(AnalysisOntology, ids)
    definition = await session.get(
        OntologyDefinition, (KEY, bound.revision), with_for_update={"read": True}
    )
    if (
        not definition
        or bound.dependency_hash != dependency_hash
        or definition.revoked_reason
        or definition.content_hash != bound.content_hash
        or digest(definition.definition) != bound.content_hash
        or definition.definition["handler_hash"] != handler_digest()
    ):
        raise ValueError("固定规则已撤销或依赖已变化，请重新规划")
    return definition
