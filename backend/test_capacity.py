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


async def test_is_within_window_timezone_conversion():
    # Europe/London window: 23:00-07:00 local time. During BST (UTC+1), 22:30 UTC
    # is 23:30 London time — inside the window. Comparing raw UTC clock time
    # against the window (the pre-fix behavior) would wrongly say False here.
    window = {"start_time": "23:00", "end_time": "07:00", "timezone": "Europe/London"}
    bst_instant = datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)
    assert capacity.is_within_window(window, bst_instant) is True

    # Same clock time in winter (GMT, UTC+0) — 22:30 UTC is 22:30 London, still
    # before the 23:00 start either way. Confirms the fix isn't just always True.
    gmt_instant = datetime(2026, 1, 15, 22, 30, tzinfo=timezone.utc)
    assert capacity.is_within_window(window, gmt_instant) is False


async def test_get_limit_fails_open_on_bad_window():
    await capacity.set_capacity_config("proj-1", day_limit=2, night_limit=8, window_id="win-bad")

    real_fetch_window = capacity._fetch_window
    capacity._fetch_window = lambda window_id: _async_return({"start_time": "not-a-time", "end_time": "07:00"})
    try:
        limit = await capacity.get_limit("proj-1")
        assert limit == 2, f"expected fail-open to day_limit=2, got {limit}"
    finally:
        capacity._fetch_window = real_fetch_window


async def test_available_slots_clamped_to_zero():
    await capacity.set_capacity_config("proj-2", day_limit=2, night_limit=8, window_id=None)

    class FakeTask:
        def done(self):
            return False

    sdk_engine.running_tasks.clear()
    for i in range(10):
        sdk_engine.running_tasks[f"session-{i}"] = FakeTask()

    real_get_limit = capacity.get_limit
    capacity.get_limit = lambda project_id: _async_return(2)
    try:
        slots = await capacity.available_slots("proj-2")
        assert slots == 0, f"expected clamped 0 (not negative), got {slots}"
    finally:
        capacity.get_limit = real_get_limit
        sdk_engine.running_tasks.clear()


async def test_available_slots_respects_cli_engine():
    # dispatcher.py has no claude-code-sdk dependency — import the real module
    # rather than faking it, to prove the engine switch actually works.
    import dispatcher

    class FakeTask:
        def done(self):
            return False

    dispatcher.running_tasks.clear()
    sdk_engine.running_tasks.clear()
    dispatcher.running_tasks["cli-session"] = FakeTask()

    real_get_limit = capacity.get_limit
    capacity.get_limit = lambda project_id: _async_return(8)
    prev_engine = os.environ.get("DEVFLEET_ENGINE")
    os.environ["DEVFLEET_ENGINE"] = "cli"
    try:
        slots = await capacity.available_slots("proj-2")
        assert slots == 7, f"expected 8-1=7 counted from dispatcher.running_tasks, got {slots}"
        assert len(sdk_engine.running_tasks) == 0, "sdk_engine.running_tasks must not be touched under DEVFLEET_ENGINE=cli"
    finally:
        capacity.get_limit = real_get_limit
        dispatcher.running_tasks.clear()
        if prev_engine is None:
            os.environ.pop("DEVFLEET_ENGINE", None)
        else:
            os.environ["DEVFLEET_ENGINE"] = prev_engine


async def test_window_id_validated_when_night_windows_exists():
    # night_windows is the real table from Track 2 (db.py's schema) by the time
    # this runs — db.init_db() in main() already created it, project_id NOT NULL
    # included, so no local CREATE TABLE stub is needed here anymore.
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO night_windows (id, project_id, start_time, end_time) "
            "VALUES ('win-real', 'proj-1', '23:00', '07:00')"
        )
        await conn.commit()
    finally:
        await conn.close()

    try:
        # Valid window_id — accepted.
        await capacity.set_capacity_config("proj-1", day_limit=2, night_limit=8, window_id="win-real")

        # Unknown window_id, but night_windows now exists and is checkable — rejected.
        try:
            await capacity.set_capacity_config("proj-1", day_limit=2, night_limit=8, window_id="no-such-window")
            assert False, "expected InvalidCapacityConfig for an unknown window_id"
        except capacity.InvalidCapacityConfig:
            pass
    finally:
        conn = await db.get_db()
        await conn.execute("DROP TABLE night_windows")
        await conn.commit()
        await conn.close()

    # Table gone again (Track 2 not merged, as on this branch normally) — an
    # unvalidatable window_id must not be rejected just because we can't check it.
    await capacity.set_capacity_config("proj-1", day_limit=2, night_limit=8, window_id="unverifiable")


async def test_concurrent_writes_converge_on_one_row():
    """Reproduces the review's finding: 8 concurrent first-writes to the same
    scope must produce exactly 1 row, not 8."""
    conn = await db.get_db()
    try:
        await conn.execute("DELETE FROM capacity_config WHERE project_id IS NULL")
        await conn.commit()
    finally:
        await conn.close()

    await asyncio.gather(
        *[capacity.set_capacity_config(None, day_limit=2, night_limit=8) for _ in range(8)]
    )

    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM capacity_config WHERE project_id IS NULL"
        )
    finally:
        await conn.close()
    assert len(rows) == 1, f"expected 1 global row, got {len(rows)}"

    # Same scope, called again — must update the existing row, not add a second one.
    await capacity.set_capacity_config(None, day_limit=3, night_limit=9)
    conn = await db.get_db()
    try:
        rows = await conn.execute_fetchall(
            "SELECT * FROM capacity_config WHERE project_id IS NULL"
        )
    finally:
        await conn.close()
    assert len(rows) == 1, f"expected still 1 global row after update, got {len(rows)}"
    assert rows[0]["day_limit"] == 3


async def test_negative_limits_rejected():
    for kwargs in ({"day_limit": -2, "night_limit": 8}, {"day_limit": 2, "night_limit": -8}):
        try:
            await capacity.set_capacity_config("proj-1", **kwargs)
            assert False, f"expected InvalidCapacityConfig for {kwargs}"
        except capacity.InvalidCapacityConfig:
            pass


async def test_empty_string_project_id_rejected():
    try:
        await capacity.set_capacity_config("", day_limit=2, night_limit=8)
        assert False, "expected InvalidCapacityConfig for project_id=''"
    except capacity.InvalidCapacityConfig:
        pass

    try:
        await capacity.get_capacity_config("")
        assert False, "expected InvalidCapacityConfig for project_id=''"
    except capacity.InvalidCapacityConfig:
        pass


async def test_nonexistent_project_id_rejected():
    try:
        await capacity.set_capacity_config("no-such-project-id", day_limit=2, night_limit=8)
        assert False, "expected ProjectNotFound for a project_id with no matching row"
    except capacity.ProjectNotFound:
        pass


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
    await test_is_within_window_timezone_conversion()
    await test_concurrent_writes_converge_on_one_row()
    await test_negative_limits_rejected()
    await test_empty_string_project_id_rejected()
    await test_nonexistent_project_id_rejected()
    await test_window_id_validated_when_night_windows_exists()
    await test_get_limit_no_config_falls_back_to_env()
    await test_get_limit_day_vs_night()
    await test_get_limit_fails_open_on_bad_window()
    await test_available_slots()
    await test_available_slots_clamped_to_zero()
    await test_available_slots_respects_cli_engine()
    os.unlink(_tmp_db.name)
    print("test_capacity.py: all assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
