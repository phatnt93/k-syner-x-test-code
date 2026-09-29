"""PostgreSQL-backed job queue (decision D7): no Redis / Kafka, the queue commits with the data it refers to.

A worker claims jobs with `FOR UPDATE SKIP LOCKED` and takes a lease (`locked_until`); a worker that dies
simply lets the lease expire and another one re-claims the job. `attempts` is counted at claim time, so a job
that crashes its worker every time still ends up `DEAD` instead of looping forever.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cdms.db.base import Base


class Job(Base):
    __tablename__ = "job"
    __table_args__ = (
        UniqueConstraint("kind", "ref_id"),  # one job per inbox event / manual poll request
        CheckConstraint("kind IN ('webhook_event', 'poll_now')", name="kind"),
        CheckConstraint("status IN ('PENDING', 'RUNNING', 'DONE', 'DEAD')", name="status"),
        # The claim query only looks at unfinished jobs.
        Index(None, "run_after", postgresql_where=text("status IN ('PENDING', 'RUNNING')")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text)
    ref_id: Mapped[str] = mapped_column(Text)  # inbox_event.id, or a request id for poll_now
    status: Mapped[str] = mapped_column(Text, server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    max_attempts: Mapped[int]
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())  # backoff
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # lease
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"Job(id={self.id}, {self.kind} {self.ref_id!r}, {self.status})"
