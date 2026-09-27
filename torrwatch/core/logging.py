"""Runtime logging with an administrator-controlled, secret-safe debug file."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie|password|token|"
    r"api[_-]?key|secret|passkey)\b\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bBearer\s+)([^\s]+)")


def redact_log_text(value: str) -> str:
    """Mask common secret-bearing key/value and authorization patterns in text."""
    value = _BEARER_PATTERN.sub(r"\1[REDACTED]", value)
    return _SECRET_PATTERN.sub(r"\1[REDACTED]", value)


class SecretFilter(logging.Filter):
    def __init__(self, redact_enabled: Callable[[], bool]) -> None:
        super().__init__()
        self._redact_enabled = redact_enabled

    def filter(self, record: logging.LogRecord) -> bool:
        if self._redact_enabled():
            record.msg = redact_log_text(str(record.getMessage()))
            record.args = ()
        return True


class SecretFormatter(logging.Formatter):
    def __init__(self, redact_enabled: Callable[[], bool]) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redact_enabled = redact_enabled

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        return redact_log_text(rendered) if self._redact_enabled() else rendered


def configure_debug_logging(
    data_dir: Path,
    component: str,
    level: int,
    redact_enabled: Callable[[], bool],
) -> None:
    """Attach rotating file and container-stream handlers to the process root logger.

    The stream handler is intentional: ``docker compose logs`` is the primary
    operational view.  The root level controls verbosity (INFO in normal mode,
    DEBUG when diagnostics are enabled), while both handlers share redaction.
    """
    log_dir = data_dir / "logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log_dir / f"{component}.debug.log"
    root = logging.getLogger()
    marker = f"torrwatch-debug:{component}"
    if any(getattr(handler, "_torrwatch_marker", None) == marker for handler in root.handlers):
        root.setLevel(level)
        return
    formatter = SecretFormatter(redact_enabled)
    secret_filter = SecretFilter(redact_enabled)

    file_handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler._torrwatch_marker = marker  # type: ignore[attr-defined]
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(secret_filter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler._torrwatch_marker = f"{marker}:stdout"  # type: ignore[attr-defined]
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(secret_filter)
    root.addHandler(stream_handler)
    root.setLevel(level)
    # Uvicorn intentionally disables propagation on its loggers.  Re-enable it
    # so access/startup/errors are also present in the same container stream and
    # rotating file as application logs.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.propagate = True
        uvicorn_logger.setLevel(level)
    with suppress(OSError):
        path.chmod(0o600)


def read_debug_logs(data_dir: Path, redact_enabled: bool) -> str:
    """Read current and rotated logs, applying a final safety redaction."""
    log_dir = data_dir / "logs"
    paths = sorted(log_dir.glob("*.debug.log*")) if log_dir.exists() else []
    chunks: list[str] = []
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(redact_log_text(content) if redact_enabled else content)
    return "\n".join(chunks)
