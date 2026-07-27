from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.mcp_server import MCPServer


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # SABAH.OS user who triggered the run (set for runs proxied from Django).
    sabah_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Nullable: a run may span every enabled Google server rather than one.
    mcp_server_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mcp_servers.id"), nullable=True
    )
    # Names of all servers the run was given, when it spans more than one.
    server_names: Mapped[list] = mapped_column(JSON, default=list)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    output_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("pending", "success", "error", name="run_status_enum"),
        nullable=False,
        default="pending",
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    mcp_server: Mapped[Optional["MCPServer"]] = relationship(
        "MCPServer", back_populates="runs"
    )

    __table_args__ = (
        Index("ix_automation_runs_user_id", "user_id"),
        Index("ix_automation_runs_user_status", "user_id", "status"),
        Index("ix_automation_runs_sabah_user_id", "sabah_user_id"),
    )
