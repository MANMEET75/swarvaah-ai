from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def now() -> datetime:
    return datetime.now(timezone.utc)


def uid() -> str:
    return str(uuid4())


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    external_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(20))
    reminder_at: Mapped[str] = mapped_column(String(40))
    reminder_label: Mapped[str] = mapped_column(String(240))
    language: Mapped[str] = mapped_column(String(12), default="hi-IN")
    consent_source: Mapped[str] = mapped_column(String(240))
    consent_at: Mapped[str] = mapped_column(String(40))
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), default="draft")
    language: Mapped[str] = mapped_column(String(12), default="hi-IN")
    voice: Mapped[str] = mapped_column(String(40), default="priya")
    script: Mapped[str] = mapped_column(Text, default="service_reminder_v1")
    script_version: Mapped[int] = mapped_column(Integer, default=1)
    start_hour: Mapped[int] = mapped_column(Integer, default=9)
    end_hour: Mapped[int] = mapped_column(Integer, default=18)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=5)
    retry_limit: Mapped[int] = mapped_column(Integer, default=0)
    budget_inr: Mapped[float] = mapped_column(Float, default=100.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    __table_args__ = (UniqueConstraint("campaign_id", "contact_id", "attempt_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    reason: Mapped[str | None] = mapped_column(String(240), nullable=True)
    provider_sid: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    cost_inr: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class CallSession(Base):
    __tablename__ = "call_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    attempt_id: Mapped[str | None] = mapped_column(ForeignKey("call_attempts.id"), nullable=True, unique=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    direction: Mapped[str] = mapped_column(String(12), default="outbound")
    status: Mapped[str] = mapped_column(String(24), default="connected")
    workflow_state: Mapped[str] = mapped_column(String(32), default="intro")
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    language: Mapped[str] = mapped_column(String(12), default="hi-IN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CallEvent(Base):
    __tablename__ = "call_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey("call_sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(80), default="operator")
    action: Mapped[str] = mapped_column(String(100))
    target_id: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
