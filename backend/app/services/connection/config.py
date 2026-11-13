"""Shared connection configuration handling.

Connection records keep their credentials inside an encrypted JSON envelope.
Every runtime path that opens a source database goes through this module so a
saved connection and a refresh worker build the same SQLAlchemy URL.
"""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import quote


SQL_KINDS = {"mysql", "postgres", "postgresql"}


class ConnectionConfigError(ValueError):
    """A saved connection could not be converted into runtime configuration."""


def decrypt_connection_config(raw_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a runtime config from either plaintext or an encrypted envelope.

    Plaintext is accepted for compatibility with old fixtures and direct
    connector callers.  Persisted API writes always use the encrypted form.
    """
    raw = dict(raw_config or {})
    encrypted = raw.get("_encrypted")
    if not encrypted:
        return raw
    try:
        from app.services import encryption_service

        decoded = json.loads(encryption_service.decrypt(str(encrypted)))
    except Exception as exc:
        raise ConnectionConfigError("connection configuration could not be decrypted") from exc
    if not isinstance(decoded, dict):
        raise ConnectionConfigError("connection configuration must be a JSON object")
    return decoded


def _sql_url(raw_config: Mapping[str, Any], kind: str) -> str:
    existing = raw_config.get("connection_string")
    if existing:
        return str(existing)

    host = str(raw_config.get("host") or "localhost")
    port = raw_config.get("port") or (3306 if kind == "mysql" else 5432)
    try:
        port_value = int(port)
    except (TypeError, ValueError) as exc:
        raise ConnectionConfigError("database port must be a number") from exc
    if not 1 <= port_value <= 65535:
        raise ConnectionConfigError("database port must be between 1 and 65535")

    user = str(raw_config.get("user") or "")
    password = str(raw_config.get("password") or "")
    database = str(raw_config.get("database") or "")
    if not user or not database:
        raise ConnectionConfigError("database user and database name are required")

    scheme = "mysql+pymysql" if kind == "mysql" else "postgresql+psycopg2"
    # Keep the host as supplied (including bracketed IPv6 literals), while
    # encoding URL components that may contain @, :, /, #, or whitespace.
    return (
        f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port_value}/{quote(database, safe='')}"
    )


def build_connector_config(
    kind: str,
    raw_config: Mapping[str, Any] | None,
    *,
    resource: str | None = None,
) -> dict[str, Any]:
    """Resolve persisted config and add the runtime DSN for SQL sources."""
    normalized_kind = "postgres" if kind == "postgresql" else kind
    config = decrypt_connection_config(raw_config)
    if normalized_kind in SQL_KINDS:
        config["connection_string"] = _sql_url(config, normalized_kind)
    if resource is not None:
        config["resource"] = resource
    return config


def safe_connection_error(exc: BaseException) -> str:
    """Return useful connector detail while redacting common URL secrets."""
    message = str(exc) or exc.__class__.__name__
    # SQLAlchemy normally masks passwords, but drivers occasionally include a
    # raw DSN in their exception.  Avoid leaking password material in API
    # responses and logs when that happens.
    import re

    message = re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", message)
    return message[:500]
