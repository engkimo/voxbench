"""SQLAlchemy models for control-plane tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_VALUE = JSON().with_variant(JSONB(), "postgresql")
BIGINT_PRIMARY_KEY = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Plugin(TimestampMixin, Base):
    __tablename__ = "plugins"
    __table_args__ = (UniqueConstraint("kind", "name", "version", name="uq_plugins_identity"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)


class Config(TimestampMixin, Base):
    __tablename__ = "configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("configs.id"),
        nullable=True,
    )
    spec: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    resolved: Mapped[dict[str, Any] | None] = mapped_column(JSON_VALUE, nullable=True)
    hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    labels: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False, default=dict)


class Run(TimestampMixin, Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    arm_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    config_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    config_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scenario_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    engine: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_config: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    environment_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        default=dict,
    )
    readiness_checklist: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_VALUE,
        nullable=False,
        default=list,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Recording(TimestampMixin, Base):
    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(255), nullable=False)
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    duration_ms: Mapped[float] = mapped_column(nullable=False)


class Span(TimestampMixin, Base):
    __tablename__ = "spans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    trace_id: Mapped[str] = mapped_column(Text, nullable=False)
    span_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_ns: Mapped[int] = mapped_column(nullable=False)
    end_ns: Mapped[int] = mapped_column(nullable=False)
    attrs: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)


class Verification(TimestampMixin, Base):
    __tablename__ = "verifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    stage: Mapped[str] = mapped_column(String(255), nullable=False)
    invariant: Mapped[str] = mapped_column(String(64), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    expected: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(BIGINT_PRIMARY_KEY, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    stage: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[float] = mapped_column(nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SipEvent(Base):
    __tablename__ = "sip_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    summary_alias: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RtpStat(Base):
    __tablename__ = "rtp_stats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("runs.id"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    jitter_ms: Mapped[float | None] = mapped_column(nullable=True)
    loss_pct: Mapped[float | None] = mapped_column(nullable=True)
    mos: Mapped[float | None] = mapped_column(nullable=True)
    direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    rtt_ms: Mapped[float | None] = mapped_column(nullable=True)
