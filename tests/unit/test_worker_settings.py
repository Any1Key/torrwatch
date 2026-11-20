import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.core.secrets import SecretBox
from torrwatch.db.database import Database
from torrwatch.db.models import Base, SystemSetting
from torrwatch.worker.main import _flaresolverr_config, _setting_enabled


@pytest.mark.parametrize("interval,expected", [("1", 300), ("900", 900), ("invalid", None)])
@pytest.mark.parametrize("valid_secret", [True, False])
def test_refresh_settings_decryption_and_interval_bounds(
    settings, interval, expected, valid_secret
):
    ensure_runtime_files(settings)
    box = SecretBox(settings.resolved_master_key_file)
    db = Database(settings.resolved_database_url)
    Base.metadata.create_all(db.engine)
    secret = box.encrypt("password") if valid_secret else "invalid"
    with db.session() as session:
        session.add_all(
            [
                SystemSetting(key=k, value=v)
                for k, v in {
                    "flaresolverr_refresh_seconds": interval,
                    "flaresolverr_enabled": "false",
                    "nnmclub_username": "user",
                    "nnmclub_password_encrypted": secret,
                    "tracker_rutracker_password_encrypted": secret,
                    "debug_redact_secrets": "false",
                }.items()
            ]
        )
    result = _flaresolverr_config(db, settings, box)
    assert result["refresh_seconds"] == (expected or settings.flaresolverr_refresh_seconds)
    assert result["enabled"] is False
    assert result["username"] == "user"
    assert result["password"] == ("password" if valid_secret else "")
    assert result["rutracker_password"] == ("password" if valid_secret else "")
    assert result["credentials_revision"]
    assert not _setting_enabled(db, "debug_redact_secrets")
    assert _setting_enabled(db, "missing")
    db.dispose()


def test_missing_database_uses_environment_defaults(settings):
    from unittest.mock import Mock

    db = Mock()
    db.session.side_effect = RuntimeError("unavailable")
    result = _flaresolverr_config(db, settings, Mock())
    assert result["refresh_seconds"] == settings.flaresolverr_refresh_seconds
    assert result["credentials_revision"] == ""
    assert _setting_enabled(db, "debug_redact_secrets")
