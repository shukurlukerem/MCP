"""Corporate Google Workspace support

Adds the SABAH.OS link (sabah_user_id), granted-service tracking and revocation
to credentials; provider/service metadata to the MCP server registry; and
multi-server runs.

Revision ID: 002
Revises: 001
Create Date: 2026-07-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── google_credentials ────────────────────────────────────────────────────
    op.add_column("google_credentials", sa.Column("sabah_user_id", sa.String(length=64), nullable=True))
    op.add_column("google_credentials", sa.Column("domain", sa.String(length=255), nullable=True))
    op.add_column("google_credentials", sa.Column("services", sa.JSON(), nullable=True))
    op.add_column(
        "google_credentials",
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "google_credentials",
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    # One row per Google account — the upsert path relies on it.
    op.create_unique_constraint(
        "uq_google_credentials_user_id", "google_credentials", ["user_id"]
    )
    op.create_index(
        "ix_google_credentials_sabah_user_id", "google_credentials", ["sabah_user_id"]
    )
    op.create_index(
        "ix_google_credentials_email", "google_credentials", ["google_account_email"]
    )
    op.execute(
        "UPDATE google_credentials SET domain = split_part(google_account_email, '@', 2)"
    )

    # ── mcp_servers ───────────────────────────────────────────────────────────
    op.add_column(
        "mcp_servers",
        sa.Column("provider", sa.String(length=50), nullable=False, server_default="custom"),
    )
    op.add_column("mcp_servers", sa.Column("service", sa.String(length=50), nullable=True))
    op.add_column("mcp_servers", sa.Column("description", sa.Text(), nullable=True))

    # ── automation_runs ───────────────────────────────────────────────────────
    op.add_column("automation_runs", sa.Column("sabah_user_id", sa.String(length=64), nullable=True))
    op.add_column("automation_runs", sa.Column("server_names", sa.JSON(), nullable=True))
    # A run may now span several servers, so it is no longer tied to exactly one.
    op.alter_column("automation_runs", "mcp_server_id", existing_type=sa.Integer(), nullable=True)
    op.create_index("ix_automation_runs_sabah_user_id", "automation_runs", ["sabah_user_id"])


def downgrade() -> None:
    op.drop_index("ix_automation_runs_sabah_user_id", table_name="automation_runs")
    op.alter_column("automation_runs", "mcp_server_id", existing_type=sa.Integer(), nullable=False)
    op.drop_column("automation_runs", "server_names")
    op.drop_column("automation_runs", "sabah_user_id")

    op.drop_column("mcp_servers", "description")
    op.drop_column("mcp_servers", "service")
    op.drop_column("mcp_servers", "provider")

    op.drop_index("ix_google_credentials_email", table_name="google_credentials")
    op.drop_index("ix_google_credentials_sabah_user_id", table_name="google_credentials")
    op.drop_constraint("uq_google_credentials_user_id", "google_credentials", type_="unique")
    op.drop_column("google_credentials", "last_used_at")
    op.drop_column("google_credentials", "revoked")
    op.drop_column("google_credentials", "services")
    op.drop_column("google_credentials", "domain")
    op.drop_column("google_credentials", "sabah_user_id")
