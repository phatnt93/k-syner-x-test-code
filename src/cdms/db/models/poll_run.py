"""One scan of the inventory Products API by the poller (decision D8, docs/database.md)."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cdms.db.base import Base


class PollRun(Base):
    __tablename__ = "poll_run"
    __table_args__ = (
        CheckConstraint("trigger IN ('SCHEDULE', 'MANUAL')", name="trigger"),
        CheckConstraint("status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'ABORTED')", name="status"),
        Index(None, "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    trigger: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'RUNNING'"))

    # Progress, committed page by page together with the changes of that page.
    pages: Mapped[int] = mapped_column(server_default=text("0"))
    items: Mapped[int] = mapped_column(server_default=text("0"))
    created: Mapped[int] = mapped_column(server_default=text("0"))
    updated: Mapped[int] = mapped_column(server_default=text("0"))
    unchanged: Mapped[int] = mapped_column(server_default=text("0"))
    stale: Mapped[int] = mapped_column(server_default=text("0"))
    invalid: Mapped[int] = mapped_column(server_default=text("0"))

    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"PollRun(id={self.id}, {self.trigger}, {self.status})"
