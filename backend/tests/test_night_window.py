"""
Standalone verification for night_window.py (Track 2).

Run from repo root:  pytest backend/tests -q

No server, no Docker, no production DB. DB-backed tests point DEVFLEET_DB at
a temp file before importing db, so the real data/devfleet.db is never opened.
"""

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

# ── Isolate the DB before any backend import ──────────────────────────────
_TMP_DIR = tempfile.mkdtemp(prefix="devfleet-nw-test-")
os.environ["DEVFLEET_DB"] = os.path.join(_TMP_DIR, "test.db")

BACKEND = os.path.join(os.path.dirname(__file__), "..")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import db  # noqa: E402
import night_window as nw  # noqa: E402
from night_window import is_within_window, is_project_in_window, get_active_window  # noqa: E402

LONDON = ZoneInfo("Europe/London")
NIGHT = {"start_time": "23:00", "end_time": "07:00"}


def london(hour, minute=0, month=1, day=15):
    """Aware London-local datetime on a GMT date by default (Jan)."""
    return datetime(2026, month, day, hour, minute, tzinfo=LONDON)


# ═══════════════════════════════════════════════════════════════════════════
# Acceptance criteria (from the task brief)
# ═══════════════════════════════════════════════════════════════════════════

def test_acceptance_wrap_window_at_2am_is_true():
    assert is_within_window(NIGHT, london(2)) is True


def test_acceptance_wrap_window_at_2pm_is_false():
    assert is_within_window(NIGHT, london(14)) is False


def test_acceptance_no_row_means_unrestricted(fresh_db):
    # Project exists but has no night_windows row -> always True
    assert asyncio.run(is_project_in_window("proj-no-window")) is True
    # Even a project id that doesn't exist at all is unrestricted
    assert asyncio.run(is_project_in_window("does-not-exist")) is True


# ═══════════════════════════════════════════════════════════════════════════
# Overnight wrapping
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("hour,expected", [
    (22, False), (23, True), (0, True), (3, True), (6, True), (7, False), (12, False),
])
def test_wrap_window_hours(hour, expected):
    assert is_within_window(NIGHT, london(hour)) is expected


def test_same_day_window():
    day = {"start_time": "09:00", "end_time": "17:00"}
    assert is_within_window(day, london(9)) is True
    assert is_within_window(day, london(12)) is True
    assert is_within_window(day, london(16, 59)) is True
    assert is_within_window(day, london(17)) is False
    assert is_within_window(day, london(8, 59)) is False
    assert is_within_window(day, london(23)) is False


@pytest.mark.parametrize("t", ["10:00", "00:00", "23:00"])
def test_start_equals_end_is_invalid(t):
    # Ambiguous (empty or 24 h?) so rejected; is_project_in_window fails open on it.
    with pytest.raises(ValueError):
        is_within_window({"start_time": t, "end_time": t}, london(10))


# ═══════════════════════════════════════════════════════════════════════════
# Inclusive / exclusive boundaries: [start, end)
# ═══════════════════════════════════════════════════════════════════════════

def test_start_minute_is_inclusive():
    assert is_within_window(NIGHT, london(23, 0)) is True


def test_minute_before_start_is_outside():
    assert is_within_window(NIGHT, london(22, 59)) is False


def test_end_minute_is_exclusive():
    assert is_within_window(NIGHT, london(7, 0)) is False


def test_minute_before_end_is_inside():
    assert is_within_window(NIGHT, london(6, 59)) is True


def test_seconds_do_not_leak_past_end_boundary():
    # 06:59:59 is still inside; 07:00:00 is out
    assert is_within_window(NIGHT, datetime(2026, 1, 15, 6, 59, 59, tzinfo=LONDON)) is True
    assert is_within_window(NIGHT, datetime(2026, 1, 15, 7, 0, 0, tzinfo=LONDON)) is False


# ═══════════════════════════════════════════════════════════════════════════
# London timezone behaviour (GMT in winter, BST in summer)
# ═══════════════════════════════════════════════════════════════════════════

def test_utc_input_is_converted_to_london_gmt_winter():
    # January: London == UTC. 22:30 UTC -> 22:30 London -> outside.
    assert is_within_window(NIGHT, datetime(2026, 1, 15, 22, 30, tzinfo=timezone.utc)) is False


def test_utc_input_is_converted_to_london_bst_summer():
    # July: London == UTC+1. 22:30 UTC -> 23:30 London -> inside.
    assert is_within_window(NIGHT, datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)) is True


def test_bst_end_boundary_shifts_in_utc():
    # 06:00 UTC in July is 07:00 BST -> outside. 05:59 UTC is 06:59 BST -> inside.
    assert is_within_window(NIGHT, datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)) is False
    assert is_within_window(NIGHT, datetime(2026, 7, 15, 5, 59, tzinfo=timezone.utc)) is True


def test_naive_datetime_is_treated_as_utc():
    # Same as the BST test but naive: must behave like UTC, not local machine time.
    assert is_within_window(NIGHT, datetime(2026, 7, 15, 22, 30)) is True
    assert is_within_window(NIGHT, datetime(2026, 1, 15, 22, 30)) is False


def test_missing_timezone_defaults_to_london():
    assert "timezone" not in NIGHT
    assert nw.DEFAULT_TIMEZONE == "Europe/London"
    # 22:30 UTC in July -> inside only if London default applied
    assert is_within_window(NIGHT, datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)) is True


def test_explicit_other_timezone_is_honoured():
    tokyo = {**NIGHT, "timezone": "Asia/Tokyo"}  # UTC+9
    # 15:00 UTC -> 00:00 Tokyo -> inside
    assert is_within_window(tokyo, datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)) is True
    # Same instant in London is 15:00 -> outside
    assert is_within_window(NIGHT, datetime(2026, 1, 15, 15, 0, tzinfo=timezone.utc)) is False


# ═══════════════════════════════════════════════════════════════════════════
# Disabled windows and validation errors
# ═══════════════════════════════════════════════════════════════════════════

def test_disabled_flag_makes_pure_function_false():
    assert is_within_window({**NIGHT, "enabled": 0}, london(2)) is False
    assert is_within_window({**NIGHT, "enabled": 1}, london(2)) is True


@pytest.mark.parametrize("bad", [
    "7", "07", "7:0:0", "25:00", "07:60", "", "seven", None, 7,
    # From review: non-padded, signed, non-ASCII digits, whitespace
    "7:5", "7:00", "07:5", "+7:00", "-1:00", "\u0660\u0667:\u0660\u0660", " 07:00", "07:00 ", "07:00\n",
])
def test_malformed_time_raises(bad):
    with pytest.raises(ValueError):
        is_within_window({"start_time": bad, "end_time": "07:00"}, london(2))


def test_timezone_names_are_case_insensitive():
    # 22:30 UTC in July is 23:30 BST -> inside, regardless of spelling
    t = datetime(2026, 7, 15, 22, 30, tzinfo=timezone.utc)
    for name in ("Europe/London", "europe/london", "EUROPE/LONDON"):
        assert is_within_window({**NIGHT, "timezone": name}, t) is True


@pytest.mark.parametrize("given,canonical", [
    ("utc", "UTC"), ("UTC", "UTC"), ("europe/london", "Europe/London"),
    ("asia/tokyo", "Asia/Tokyo"), (None, "Europe/London"),
])
def test_canonical_timezone(given, canonical):
    assert nw.canonical_timezone(given) == canonical


@pytest.mark.parametrize("bad", ["Mars/Base", "", "../etc/passwd", "Europe/../UTC", 5])
def test_canonical_timezone_rejects_unknown(bad):
    with pytest.raises(ValueError):
        nw.canonical_timezone(bad)


def test_unknown_timezone_raises():
    with pytest.raises(ValueError):
        is_within_window({**NIGHT, "timezone": "Mars/Olympus_Mons"}, london(2))


# ═══════════════════════════════════════════════════════════════════════════
# DB-backed: get_active_window / is_project_in_window
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def fresh_db():
    """Recreate the temp DB with one project and no windows."""
    path = os.environ["DEVFLEET_DB"]
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass

    async def _setup():
        await db.init_db()
        conn = await db.get_db()
        try:
            await conn.execute(
                "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
                ("proj-no-window", "No Window", "/tmp/x"),
            )
            await conn.execute(
                "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
                ("proj-night", "Night", "/tmp/y"),
            )
            await conn.commit()
        finally:
            await conn.close()

    asyncio.run(_setup())
    yield path


def _insert_window(project_id, start="23:00", end="07:00", tz="Europe/London", enabled=1):
    async def _do():
        conn = await db.get_db()
        try:
            await conn.execute(
                "INSERT INTO night_windows (id, project_id, start_time, end_time, timezone, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (f"nw-{project_id}", project_id, start, end, tz, enabled),
            )
            await conn.commit()
        finally:
            await conn.close()
    asyncio.run(_do())


def test_schema_creates_night_windows_table(fresh_db):
    async def _cols():
        conn = await db.get_db()
        try:
            rows = await conn.execute_fetchall("PRAGMA table_info(night_windows)")
            return {r["name"] for r in rows}
        finally:
            await conn.close()
    cols = asyncio.run(_cols())
    assert {"id", "project_id", "start_time", "end_time", "timezone", "enabled"} <= cols


def test_one_window_per_project_enforced(fresh_db):
    _insert_window("proj-night")
    with pytest.raises(Exception):  # sqlite3.IntegrityError via aiosqlite
        _insert_window("proj-night")


def test_get_active_window_returns_none_without_row(fresh_db):
    assert asyncio.run(get_active_window("proj-no-window")) is None


def test_get_active_window_returns_row(fresh_db):
    _insert_window("proj-night")
    row = asyncio.run(get_active_window("proj-night"))
    assert row is not None
    assert row["start_time"] == "23:00"
    assert row["end_time"] == "07:00"
    assert row["timezone"] == "Europe/London"
    assert row["enabled"] == 1


def test_disabled_row_is_treated_as_no_window(fresh_db):
    _insert_window("proj-night", enabled=0)
    assert asyncio.run(get_active_window("proj-night")) is None
    assert asyncio.run(is_project_in_window("proj-night")) is True


def test_enabled_row_gates_dispatch(fresh_db, monkeypatch):
    _insert_window("proj-night")

    class FixedDatetime(datetime):
        _fixed = None

        @classmethod
        def now(cls, tz=None):
            return cls._fixed.astimezone(tz) if tz else cls._fixed

    monkeypatch.setattr(nw, "datetime", FixedDatetime)

    FixedDatetime._fixed = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)   # 02:00 London
    assert asyncio.run(is_project_in_window("proj-night")) is True

    FixedDatetime._fixed = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)  # 14:00 London
    assert asyncio.run(is_project_in_window("proj-night")) is False

    # Unrelated project still unrestricted at 14:00
    assert asyncio.run(is_project_in_window("proj-no-window")) is True


def test_invalid_row_fails_open(fresh_db):
    _insert_window("proj-night", tz="Not/AZone")
    assert asyncio.run(is_project_in_window("proj-night")) is True
    _insert_window("proj-no-window", start="25:99")
    assert asyncio.run(is_project_in_window("proj-no-window")) is True


def test_start_equals_end_row_fails_open_not_blocks(fresh_db):
    # Review issue 2: such a row must never block dispatch forever.
    _insert_window("proj-night", start="23:00", end="23:00")
    assert asyncio.run(is_project_in_window("proj-night")) is True


def test_db_failure_fails_open(fresh_db, monkeypatch):
    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_db", boom)
    assert asyncio.run(is_project_in_window("proj-night")) is True


# ═══════════════════════════════════════════════════════════════════════════
# Routes: GET / PUT /projects/{pid}/window (router mounted on a bare app so
# the test does not import app.py and its SDK/MCP dependencies)
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routes_night_window import router  # noqa: E402


@pytest.fixture
def client(fresh_db):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_window_unknown_project_404(client):
    assert client.get("/api/projects/nope/window").status_code == 404


def test_get_window_unconfigured(client):
    r = client.get("/api/projects/proj-no-window/window")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "project_id": "proj-no-window"}


def test_put_then_get_window(client):
    r = client.put("/api/projects/proj-night/window",
                   json={"start_time": "23:00", "end_time": "07:00"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["project_id"] == "proj-night"
    assert body["start_time"] == "23:00"
    assert body["end_time"] == "07:00"
    assert body["timezone"] == "Europe/London"   # default applied
    assert body["enabled"] is True

    g = client.get("/api/projects/proj-night/window").json()
    assert g["start_time"] == "23:00" and g["enabled"] is True


def test_put_is_upsert_single_row(client):
    client.put("/api/projects/proj-night/window", json={"start_time": "23:00", "end_time": "07:00"})
    r = client.put("/api/projects/proj-night/window",
                   json={"start_time": "22:00", "end_time": "06:00",
                         "timezone": "Europe/Paris", "enabled": False})
    assert r.status_code == 200
    body = r.json()
    assert body["start_time"] == "22:00"
    assert body["timezone"] == "Europe/Paris"
    assert body["enabled"] is False

    async def _count():
        conn = await db.get_db()
        try:
            rows = await conn.execute_fetchall(
                "SELECT COUNT(*) AS n FROM night_windows WHERE project_id=?", ("proj-night",))
            return rows[0]["n"]
        finally:
            await conn.close()
    assert asyncio.run(_count()) == 1


def test_put_disabled_window_unrestricts_dispatch(client):
    client.put("/api/projects/proj-night/window",
               json={"start_time": "23:00", "end_time": "07:00", "enabled": False})
    assert asyncio.run(get_active_window("proj-night")) is None
    assert asyncio.run(is_project_in_window("proj-night")) is True


def test_put_unknown_project_404(client):
    r = client.put("/api/projects/nope/window", json={"start_time": "23:00", "end_time": "07:00"})
    assert r.status_code == 404


@pytest.mark.parametrize("payload", [
    {"start_time": "25:00", "end_time": "07:00"},
    {"start_time": "23:00", "end_time": "7"},
    {"start_time": "23:00", "end_time": "07:00", "timezone": "Mars/Base"},
    {"end_time": "07:00"},
    # From review
    {"start_time": "23:00", "end_time": "23:00"},
    {"start_time": "00:00", "end_time": "00:00"},
    {"start_time": "7:5", "end_time": "23:00"},
    {"start_time": "+7:00", "end_time": "23:00"},
    {"start_time": "\u0660\u0667:\u0660\u0660", "end_time": "23:00"},
])
def test_put_rejects_invalid_payload(client, payload):
    r = client.put("/api/projects/proj-night/window", json=payload)
    assert r.status_code == 422, r.text


def test_routes_are_under_api_prefix(client):
    # Review issue 1: nginx and the Vite proxy only forward /api/.
    assert client.get("/api/projects/proj-no-window/window").status_code == 200
    assert client.get("/projects/proj-no-window/window").status_code == 404


def test_put_stores_canonical_timezone(client):
    r = client.put("/api/projects/proj-night/window",
                   json={"start_time": "23:00", "end_time": "07:00", "timezone": "utc"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "UTC"
    assert client.get("/api/projects/proj-night/window").json()["timezone"] == "UTC"


def test_put_explicit_null_timezone_defaults_to_london(client):
    r = client.put("/api/projects/proj-night/window",
                   json={"start_time": "23:00", "end_time": "07:00", "timezone": None})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "Europe/London"
