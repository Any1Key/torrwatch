from pathlib import Path

from torrwatch.core.logging import configure_debug_logging, read_debug_logs, redact_log_text


def test_debug_text_redaction_masks_common_credentials() -> None:
    text = "Authorization: Bearer abc Cookie=session=secret password=hunter2"
    result = redact_log_text(text)
    assert "abc" not in result
    assert "secret" not in result
    assert "hunter2" not in result
    assert result.count("[REDACTED]") >= 3


def test_debug_log_rotation_file_is_read_with_optional_redaction(tmp_path: Path) -> None:
    configure_debug_logging(tmp_path, "test", 10, lambda: True)
    import logging

    logging.getLogger("test").warning("token=topsecret")
    safe = read_debug_logs(tmp_path, True)
    assert "topsecret" not in safe
