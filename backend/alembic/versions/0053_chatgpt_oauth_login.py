"""ChatGPT OAuth login transactions and account links.

Revision ID: 0053_chatgpt_oauth_login
Revises: 0052_agent_max_tool_rounds
Create Date: 2026-10-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0053_chatgpt_oauth_login"
down_revision = "0052_agent_max_tool_rounds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chatgpt_login_transactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(128), nullable=False, unique=True),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=False),
        sa.Column("redirect_uri", sa.String(500), nullable=False),
        sa.Column("return_to", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "chatgpt_account_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("issuer", sa.String(200), nullable=False),
        sa.Column("client_id", sa.String(200), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("issuer", "client_id", "subject", name="uq_chatgpt_account_link_identity"),
        sa.UniqueConstraint("user_id", name="uq_chatgpt_account_link_user"),
    )


def downgrade() -> None:
    op.drop_table("chatgpt_account_links")
    op.drop_table("chatgpt_login_transactions")
