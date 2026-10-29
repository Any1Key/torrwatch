from datetime import UTC, datetime, timedelta

from sqlalchemy import event

from torrwatch.db.database import Database
from torrwatch.db.models import Base, Event, MonitorItem
from torrwatch.services.admin import AdminService
from torrwatch.trackers.registry import PluginRegistry


def test_monitor_list_and_limited_timeline(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(db.engine)
    with db.session() as session:
        monitor = MonitorItem(
            name="test",
            original_url="https://example.org",
            canonical_url="https://example.org",
            check_interval_seconds=1800,
        )
        session.add(monitor)
        session.flush()
        monitor_id = monitor.id
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(20):
            session.add(
                Event(
                    monitor_id=monitor_id,
                    level="WARNING" if i == 7 else "INFO",
                    event_code=str(i),
                    message=str(i),
                    created_at=start + timedelta(seconds=i),
                )
            )
    service = AdminService(db, PluginRegistry())
    statements = []
    event.listen(db.engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    rows = service.monitors()
    assert len(statements) == 1
    assert rows[0] == service.monitor(monitor_id)
    assert rows[0]["last_problem"] == "7"
    full = service.timeline(monitor_id)
    assert len(full) == 20
    assert service.timeline(monitor_id, limit=12) == full[:12]
    db.dispose()
