import pytest

from torrwatch.core.runtime import ensure_runtime_files
from torrwatch.db.database import Database
from torrwatch.db.migrations import upgrade_database
from torrwatch.db.models import SystemSetting, WorkerHeartbeat
from torrwatch.worker import main


@pytest.mark.asyncio
async def test_worker_runs_empty_queues_and_stops_cleanly(settings, monkeypatch):
    ensure_runtime_files(settings)
    upgrade_database(settings)
    db = Database(settings.resolved_database_url)
    with db.session() as session:
        session.add(SystemSetting(key="flaresolverr_enabled", value="false"))
    real_notifications = main.notification_cycle
    calls = []

    async def notifications(*args, **kwargs):
        result = await real_notifications(*args, **kwargs)
        calls.append(result)
        kwargs["stop_event"].set()
        return result

    monkeypatch.setattr(main, "notification_cycle", notifications)
    monkeypatch.setattr(main, "configure_debug_logging", lambda *args: None)
    await main.run_worker(settings)
    assert calls == [0]
    with db.session() as session:
        assert session.query(WorkerHeartbeat).count() == 1
    db.dispose()
