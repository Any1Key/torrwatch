"""System-level configuration for TorrWatch."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration intentionally limited to bootstrap and runtime settings."""

    model_config = SettingsConfigDict(env_prefix="TORRWATCH_", case_sensitive=False)

    data_dir: Path = Path("/data")
    log_level: str = "INFO"
    bind: str = "0.0.0.0"
    port: int = 8080
    master_key_file: Path | None = None
    database_url: str | None = None
    admin_username: str | None = None
    admin_password: SecretStr | None = None
    session_https_only: bool = False
    session_max_age_seconds: int = Field(default=43_200, ge=300)
    worker_heartbeat_seconds: int = Field(default=30, ge=5)
    worker_job_lease_seconds: int = Field(default=180, ge=30)
    worker_lease_renewal_seconds: int = Field(default=60, ge=5)
    http_connect_timeout_seconds: float = Field(default=10.0, gt=0)
    http_read_timeout_seconds: float = Field(default=30.0, gt=0)
    http_write_timeout_seconds: float = Field(default=30.0, gt=0)
    http_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    http_max_attempts: int = Field(default=3, ge=1, le=10)
    http_user_agent: str = "TorrWatch/0.1"
    torrent_retention_count: int = Field(default=5, ge=1, le=100)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_master_key_file(self) -> Path:
        return self.master_key_file or self.data_dir / "master.key"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{self.data_dir / 'torrwatch.db'}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_plugins_dir(self) -> Path:
        return self.data_dir / "plugins"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_torrents_dir(self) -> Path:
        return self.data_dir / "torrents"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide production settings instance."""

    return Settings()
