import asyncio
from unittest.mock import AsyncMock

import pytest

from torrwatch.db.database import Database
from torrwatch.db.models import Base, MonitorItem
from torrwatch.notifications.service import NotificationRepository
from torrwatch.services.flaresolverr import FlareSolverrError
from torrwatch.worker import main


@pytest.mark.parametrize(
    "failures,code,expected",
    [
        (1, "TRACKER_NETWORK_ERROR", 0),
        (2, "TRACKER_UNAVAILABLE", 0),
        (3, "TRACKER_NETWORK_ERROR", 1),
        (1, "AUTH_REQUIRED", 1),
        (0, "TORRENT_CHANGE_DETECTED", 1),
    ],
)
def test_notification_threshold(tmp_path, monkeypatch, failures, code, expected):
    database = Database(f"sqlite:///{tmp_path}/test.db")
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        monitor = MonitorItem(
            name="Test",
            original_url="https://nnmclub.to",
            canonical_url="https://nnmclub.to",
            check_interval_seconds=60,
            consecutive_failures=failures,
        )
        session.add(monitor)
        session.flush()
        monitor_id = monitor.id
    repository = NotificationRepository(database)
    sent = []
    monkeypatch.setattr(repository, "enqueue_for_event", lambda *args: sent.append(args) or 1)
    assert repository.enqueue_for_monitor_event(1, code, monitor_id, "test") == expected
    assert len(sent) == expected
    database.dispose()


@pytest.mark.parametrize("recover", [True, False])
def test_session_refresh_retry_is_bounded_and_other_trackers_continue(
    tmp_path, monkeypatch, recover
):
    database = Database(f"sqlite:///{tmp_path}/refresh.db")
    Base.metadata.create_all(database.engine)
    nnm = AsyncMock(
        side_effect=[
            FlareSolverrError("timeout"),
            None if recover else FlareSolverrError("timeout"),
        ]
    )
    kinozal = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(main, "refresh_nnmclub", nnm)
    monkeypatch.setattr(main, "refresh_kinozal", kinozal)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    asyncio.run(
        main.refresh_tracker_sessions(
            database,
            None,
            {
                "url": "http://solver",
                "username": "test",
                "password": "test",
                "kinozal_username": "test",
                "kinozal_password": "test",
                "rutracker_username": "",
                "rutracker_password": "",
            },
        )
    )
    assert nnm.await_count == 2
    kinozal.assert_awaited_once()
    sleep.assert_awaited_once_with(30)
    database.dispose()
