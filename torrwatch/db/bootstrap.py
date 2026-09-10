"""Initial administrator bootstrap."""

from __future__ import annotations

from sqlalchemy import select

from torrwatch.core.config import Settings
from torrwatch.core.security import hash_password
from torrwatch.db.database import Database
from torrwatch.db.models import User

PLACEHOLDER_PASSWORD = "change-this-before-first-start"


def bootstrap_admin(database: Database, settings: Settings) -> None:
    """Create the single administrator once, without persisting plaintext."""
    with database.session() as database_session:
        if database_session.scalar(select(User).limit(1)) is not None:
            return
        password = settings.admin_password.get_secret_value() if settings.admin_password else None
        if not settings.admin_username or not password or password == PLACEHOLDER_PASSWORD:
            raise RuntimeError(
                "No administrator exists. Set TORRWATCH_ADMIN_USERNAME and a non-placeholder "
                "TORRWATCH_ADMIN_PASSWORD before first startup."
            )
        if len(password) < 12:
            raise RuntimeError("TORRWATCH_ADMIN_PASSWORD must contain at least 12 characters.")
        database_session.add(
            User(username=settings.admin_username, password_hash=hash_password(password))
        )
