from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Index, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class GoogleCredential(Base):
    """
    One row per connected Google account.

    ``user_id`` is the Google subject id (stable, never reused). ``sabah_user_id``
    links the row back to the SABAH.OS Django user, so Django can push and read
    credentials over the internal API without knowing Google's identifiers.
    """

    __tablename__ = "google_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # SABAH.OS (Django) user primary key — set when the connection originates there.
    sabah_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    google_account_email: Mapped[str] = mapped_column(String(255), nullable=False)
    # Workspace domain the account belongs to — the corporate gate checks this.
    domain: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Stored as Fernet-encrypted ciphertext — never plaintext
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expiry: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[list] = mapped_column(JSON, default=list)
    # Service keys unlocked by the granted scopes (gmail, drive, calendar, …)
    services: Mapped[list] = mapped_column(JSON, default=list)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("ix_google_credentials_user_id", "user_id"),
        Index("ix_google_credentials_sabah_user_id", "sabah_user_id"),
        Index("ix_google_credentials_email", "google_account_email"),
    )
