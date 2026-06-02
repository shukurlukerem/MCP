from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, Enum, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.automation_run import AutomationRun


class MCPServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    transport: Mapped[str] = mapped_column(
        Enum("http", "stdio", name="transport_enum"), nullable=False
    )
    url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auth_type: Mapped[str] = mapped_column(String(50), nullable=False, default="none")
    auth_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)

    runs: Mapped[List["AutomationRun"]] = relationship(
        "AutomationRun", back_populates="mcp_server", lazy="select"
    )
