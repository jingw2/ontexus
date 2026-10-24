import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ChatGptLoginTransaction(Base):
    """One-time PKCE login attempt. The verifier stays server-side."""

    __tablename__ = "chatgpt_login_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    return_to: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChatGptAccountLink(Base):
    """Verified ChatGPT subject bound to one Ontexus user."""

    __tablename__ = "chatgpt_account_links"
    __table_args__ = (
        UniqueConstraint("issuer", "client_id", "subject", name="uq_chatgpt_account_link_identity"),
        UniqueConstraint("user_id", name="uq_chatgpt_account_link_user"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    issuer: Mapped[str] = mapped_column(String(200), nullable=False)
    client_id: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
