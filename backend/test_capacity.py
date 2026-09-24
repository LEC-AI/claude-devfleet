"""
Standalone runnable check for Track 4 (Capacity Manager) — no framework/fixtures.

Run directly:
    cd backend && python3 test_capacity.py

Uses its own throwaway SQLite file (set before importing db/capacity) so it
never touches a real devfleet.db.
"""

import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timezone

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DEVFLEET_DB"] = _tmp_db.name

# capacity.available_slots() does `from sdk_engine import running_tasks`, matching
# the pattern in autoloop.py/mission_watcher.py. The real sdk_engine.py pulls in
# claude-code-sdk, which this standalone check has no need to depend on — inject a
# stand-in module so the import resolves without it (only if sdk_engine hasn't
# already been imported for real elsewhere in this process).
if "sdk_engine" not in sys.modules:
    _fake_sdk_engine = types.ModuleType("sdk_engine")
    _fake_sdk_engine.running_tasks = {}
    sys.modules["sdk_engine"] = _fake_sdk_engine

import db
import capacity
import sdk_engine


async def test_is_within_window_wrap():
    window = {"start_time": "23:00", "end_time": "07:00"}
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 2, 0)) is True
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 14, 0)) is False
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 23, 30)) is True
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 6, 59)) is True
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 7, 0)) is False


async def test_is_within_window_no_wrap():
    window = {"start_time": "09:00", "end_time": "17:00"}
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 12, 0)) is True
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 8, 0)) is False
    assert capacity.is_within_window(window, datetime(2026, 1, 1, 18, 0)) is False


async def test_get_limit_no_config_falls_back_to_env():
    limit = await capacity.get_limit("no-such-project")
    assert limit == int(os.environ.get("DEVFLEET_MAX_AGENTS", "3")), limit


async def test_get_limit_day_vs_night():
    await capacity.set_capacity_config("proj-1", day_limit=2, night_limit=8, window_id="win-1")

    # No real night_windows row exists (Track 2 not merged on this branch) —
    # monkeypatch the window fetch + the Track-2 predicate exactly like the
    # acceptance criteria describes ("a mocked 'currently in night window' from Track 2").
    real_fetch_window = capacity._fetch_window
    real_is_within_window = capacity.is_within_window
    capacity._fetch_window = lambda window_id: _async_return({"start_time": "23:00", "end_time": "07:00"})

    try:
        capacity.is_within_window = lambda window, now: True
        limit = await capacity.get_limit("proj-1")
        assert limit == 8, f"expected night_limit=8, got {limit}"

        capacity.is_within_window = lambda window, now: False
        limit = await capacity.get_limit("proj-1")
        assert limit == 2, f"expected day_limit=2, got {limit}"
    finally:
        capacity._fetch_window = real_fetch_window
        capacity.is_within_window = real_is_within_window


async def _async_return(value):
    return value


async def test_available_slots():
    await capacity.set_capacity_config("proj-2", day_limit=2, night_limit=8, window_id=None)

    class FakeTask:
        def __init__(self, done_):
            self._done = done_

        def done(self):
            return self._done

    sdk_engine.running_tasks.clear()
    for i in range(5):
        sdk_engine.running_tasks[f"session-{i}"] = FakeTask(False)
    sdk_engine.running_tasks["finished"] = FakeTask(True)  # should not count

    real_get_limit = capacity.get_limit
    capacity.get_limit = lambda project_id: _async_return(8)
    try:
        slots = await capacity.available_slots("proj-2")
        assert slots == 3, f"expected 3, got {slots}"
    finally:
        capacity.get_limit = real_get_limit
        sdk_engine.running_tasks.clear()


async def _seed_projects():
    conn = await db.get_db()
    try:
        for pid in ("proj-1", "proj-2"):
            await conn.execute(
                "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
                (pid, pid, f"/tmp/{pid}"),
            )
        await conn.commit()
    finally:
        await conn.close()


async def main():
    await db.init_db()
    await _seed_projects()
    await test_is_within_window_wrap()
    await test_is_within_window_no_wrap()
    await test_get_limit_no_config_falls_back_to_env()
    await test_get_limit_day_vs_night()
    await test_available_slots()
    os.unlink(_tmp_db.name)
    print("test_capacity.py: all assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
