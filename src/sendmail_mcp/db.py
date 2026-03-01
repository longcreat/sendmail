"""数据库模型与会话管理。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    pass


class EmailJob(Base):
    __tablename__ = "email_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="send")
    status: Mapped[str] = mapped_column(String(32), index=True)

    subject: Mapped[str] = mapped_column(Text)
    template_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    template_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), index=True)

    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    total_recipients: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    recipients: Mapped[list[EmailRecipient]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    events: Mapped[list[EmailEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class EmailRecipient(Base):
    __tablename__ = "email_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("email_jobs.id"), index=True)

    email: Mapped[str] = mapped_column(String(320), index=True)
    recipient_type: Mapped[str] = mapped_column(String(16), default="to")

    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    variables: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    job: Mapped[EmailJob] = relationship(back_populates="recipients")


class EmailEvent(Base):
    __tablename__ = "email_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("email_jobs.id"), index=True)

    recipient_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("email_recipients.id"), nullable=True
    )
    recipient_email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)

    event_type: Mapped[str] = mapped_column(String(64), index=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    job: Mapped[EmailJob] = relationship(back_populates="events")


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    template_id: Mapped[str] = mapped_column(String(128), index=True)
    template_name: Mapped[str] = mapped_column(String(128), index=True)
    version: Mapped[int] = mapped_column(Integer)

    subject_tpl: Mapped[str] = mapped_column(Text)
    html_tpl: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_tpl: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Index("ix_email_jobs_idempotency_hash", EmailJob.idempotency_key, EmailJob.payload_hash)
Index("ix_templates_template_id_version", Template.template_id, Template.version, unique=True)
Index("ix_templates_template_name_version", Template.template_name, Template.version)


class Database:
    """数据库初始化与会话工厂。"""

    def __init__(self, database_url: str):
        connect_args: dict[str, object] = {}
        if database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False

        self.engine = create_engine(database_url, future=True, connect_args=connect_args)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

        if database_url.startswith("sqlite:///"):
            db_path = database_url.removeprefix("sqlite:///")
            db_file = Path(db_path).resolve()
            db_file.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        Base.metadata.create_all(bind=self.engine)
        self._run_migrations()

    def _run_migrations(self) -> None:
        if not str(self.engine.url).startswith("sqlite"):
            return
        self._migrate_sqlite_email_jobs_table()
        self._migrate_sqlite_templates_table()

    def _migrate_sqlite_email_jobs_table(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info(email_jobs)").fetchall()
            if not rows:
                return

            columns = {row[1] for row in rows}
            if "template_name" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE email_jobs ADD COLUMN template_name VARCHAR(128)"
                )

    def _migrate_sqlite_templates_table(self) -> None:
        with self.engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info(templates)").fetchall()
            if not rows:
                return

            columns = {row[1] for row in rows}
            if "template_name" not in columns:
                conn.exec_driver_sql("ALTER TABLE templates ADD COLUMN template_name VARCHAR(128)")
                conn.exec_driver_sql(
                    "UPDATE templates SET template_name = template_id "
                    "WHERE template_name IS NULL OR template_name = ''"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_templates_template_name "
                    "ON templates (template_name)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_templates_template_name_version "
                    "ON templates (template_name, version)"
                )

    @contextmanager
    def session(self) -> Iterator:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
