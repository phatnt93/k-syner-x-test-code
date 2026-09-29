"""Runtime-tunable sync behavior, a single row (decision D9).

Read by the worker on every tick, so a change takes effect on the next tick; connections and secrets stay in
the environment (`cdms.config`).
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, func, text
from sqlalchemy.orm import Mapped, mapped_column

from cdms.db.base import Base


class SyncConfig(Base):
    __tablename__ = "sync_config"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("poll_interval_seconds BETWEEN 5 AND 86400", name="poll_interval_seconds"),
        CheckConstraint("poll_page_size BETWEEN 1 AND 500", name="poll_page_size"),
        CheckConstraint("http_connect_timeout_ms BETWEEN 100 AND 60000", name="http_connect_timeout_ms"),
        CheckConstraint("http_read_timeout_ms BETWEEN 100 AND 300000", name="http_read_timeout_ms"),
        CheckConstraint("http_max_attempts BETWEEN 1 AND 10", name="http_max_attempts"),
        CheckConstraint("job_max_attempts BETWEEN 1 AND 100", name="job_max_attempts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    poll_enabled: Mapped[bool] = mapped_column(server_default=text("true"))
    poll_interval_seconds: Mapped[int] = mapped_column(server_default=text("30"))
    poll_page_size: Mapped[int] = mapped_column(server_default=text("100"))
    http_connect_timeout_ms: Mapped[int] = mapped_column(server_default=text("3000"))
    http_read_timeout_ms: Mapped[int] = mapped_column(server_default=text("10000"))
    http_max_attempts: Mapped[int] = mapped_column(server_default=text("3"))  # per page request
    job_max_attempts: Mapped[int] = mapped_column(server_default=text("5"))  # webhook / Excel jobs (C-05)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
