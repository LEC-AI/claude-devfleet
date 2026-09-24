import asyncio
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nightly_summary
from nightly_summary import (build_nightly_summary, last_closed_window,
                             list_nightly_runs, maybe_run_nightly_summary)

UTC = timezone.utc
# Arbitrary synthetic night window, 22:00 → 06:00 UTC
SINCE = datetime(2030, 1, 1, 22, 0, tzinfo=UTC)
UNTIL = SINCE + timedelta(hours=8)

run = asyncio.run


def night(hours, minutes=0, seconds=0, microseconds=0):
    """A moment `hours` after the window opens."""
    return SINCE + timedelta(hours=hours, minutes=minutes, seconds=seconds,
                             microseconds=microseconds)


def sql_ts(dt):
    """SQLite datetime('now') storage format: 'YYYY-MM-DD HH:MM:SS'."""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def iso_ts(dt, utc_offset_hours=0):
    """Python isoformat() storage format, optionally rendered in a non-UTC offset."""
    return dt.astimezone(timezone(timedelta(hours=utc_offset_hours))).isoformat()


class Seed:
    """Inserts fixture rows directly, with explicit timestamps in whatever format the test needs."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.mission_count = 0

    def project(self, name="Alpha"):
        pid = str(uuid.uuid4())
        self.conn.execute("INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
                          (pid, name, f"/tmp/{name}"))
        return pid

    def mission(self, pid, title):
        mid = str(uuid.uuid4())
        self.mission_count += 1
        self.conn.execute(
            """INSERT INTO missions (id, project_id, title, detailed_prompt, mission_number)
               VALUES (?, ?, ?, ?, ?)""",
            (mid, pid, title, f"Do: {title}", self.mission_count),
        )
        return mid

    def session(self, mid, status="completed", ended_at=None, cost=0.0, tokens=0):
        sid = str(uuid.uuid4())
        self.conn.execute(
            """INSERT INTO agent_sessions
               (id, mission_id, status, started_at, ended_at, total_cost_usd, total_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (sid, mid, status, sql_ts(SINCE - timedelta(hours=1)), ended_at, cost, tokens),
        )
        return sid

    def report(self, sid, mid, what_done, created_at):
        self.conn.execute(
            "INSERT INTO reports (id, session_id, mission_id, what_done, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, mid, what_done, created_at),
        )

    def close(self):
        self.conn.close()


@pytest.fixture
def seed(temp_db):
    s = Seed(temp_db)
    yield s
    s.close()


# ── build_nightly_summary ──

def test_three_completed_sessions_sum_count_and_digest(seed):
    """Acceptance: 3 completed sessions with known cost → correct sum, count, and digest."""
    pid = seed.project("Alpha")
    fixtures = [
        ("Add login page", 1.25, 1000, "Built the login form with validation"),
        ("Fix flaky test", 2.50, 2000, "Stabilised the websocket reconnect test"),
        ("Write API docs", 0.75, 3000, "Documented every /api/projects endpoint"),
    ]
    for hour, (title, cost, tokens, what_done) in enumerate(fixtures, start=3):
        mid = seed.mission(pid, title)
        sid = seed.session(mid, "completed", iso_ts(night(hour, microseconds=123456)), cost, tokens)
        seed.report(sid, mid, what_done, sql_ts(night(hour, seconds=1)))

    result = run(build_nightly_summary(pid, SINCE, UNTIL))

    assert result["missions_completed"] == 3
    assert result["missions_failed"] == 0
    assert result["sessions_total"] == 3
    assert result["total_cost_usd"] == pytest.approx(4.50)
    assert result["total_tokens"] == 6000
    for title, _, _, what_done in fixtures:
        assert title in result["summary_text"]
        assert what_done in result["summary_text"]

    stored = run(list_nightly_runs(pid))
    assert len(stored) == 1
    assert stored[0]["id"] == result["id"]
    assert stored[0]["missions_completed"] == 3
    assert stored[0]["total_cost_usd"] == pytest.approx(4.50)
    assert stored[0]["window_start"] == SINCE.isoformat()
    assert stored[0]["window_end"] == UNTIL.isoformat()
    assert stored[0]["summary_text"] == result["summary_text"]


def test_window_bounds_and_mixed_timestamp_formats(seed):
    pid = seed.project("Alpha")
    other_pid = seed.project("Other")
    included = [
        (sql_ts(SINCE), 1.0),                              # exactly at since, SQLite format
        (iso_ts(night(7, 30)), 2.0),                       # 'T' sorts after the SQLite-format until string
        (iso_ts(night(7, 30), utc_offset_hours=2), 4.0),   # same instant via a +02:00 offset
        (iso_ts(UNTIL - timedelta(microseconds=1)), 8.0),  # last instant inside the window
    ]
    excluded = [
        (sql_ts(SINCE - timedelta(seconds=1)), 100.0),     # just before since
        (iso_ts(UNTIL), 200.0),                            # exactly at until (half-open)
        (None, 400.0),                                     # still running
    ]
    for i, (ended_at, cost) in enumerate(included + excluded):
        seed.session(seed.mission(pid, f"Mission {i}"), "completed", ended_at, cost, 10)
    seed.session(seed.mission(other_pid, "Other project work"), "completed",
                 sql_ts(night(3)), 800.0, 10)

    result = run(build_nightly_summary(pid, SINCE, UNTIL))

    assert result["total_cost_usd"] == pytest.approx(15.0)
    assert result["missions_completed"] == 4
    assert result["sessions_total"] == 4
    assert result["total_tokens"] == 40


def test_mission_outcome_is_latest_session_and_cost_covers_every_run(seed):
    pid = seed.project("Alpha")
    retried = seed.mission(pid, "Retried mission")
    seed.session(retried, "failed", sql_ts(night(1)), 0.50, 100)
    ok = seed.session(retried, "completed", sql_ts(night(3)), 1.00, 200)
    seed.report(ok, retried, "Worked on the second attempt", sql_ts(night(3, seconds=1)))

    failed = seed.mission(pid, "Broken mission")
    failed_sid = seed.session(failed, "failed", sql_ts(night(4)), 0.25, 50)
    seed.report(failed_sid, failed, "None", sql_ts(night(4, seconds=1)))  # blank-ish reports are skipped

    cancelled = seed.mission(pid, "Cancelled mission")
    seed.session(cancelled, "cancelled", sql_ts(night(5)), 0.10, 10)

    result = run(build_nightly_summary(pid, SINCE, UNTIL))

    assert result["missions_completed"] == 1
    assert result["missions_failed"] == 1
    assert result["missions_other"] == 1
    assert result["sessions_total"] == 4
    assert result["sessions_completed"] == 1
    assert result["sessions_failed"] == 2
    assert result["total_cost_usd"] == pytest.approx(1.85)
    assert result["total_tokens"] == 360

    by_title = {m["title"]: m for m in result["missions"]}
    assert by_title["Retried mission"]["status"] == "completed"
    assert by_title["Retried mission"]["cost_usd"] == pytest.approx(1.50)
    assert by_title["Broken mission"]["what_done"] == []
    assert "[failed] #2 Broken mission ($0.2500)\n  - (no report)" in result["summary_text"]
    assert "[cancelled] #3 Cancelled mission" in result["summary_text"]


def test_empty_window_still_produces_a_row(seed):
    pid = seed.project("Quiet")
    result = run(build_nightly_summary(pid, SINCE, UNTIL))
    assert result["missions_completed"] == 0
    assert result["total_cost_usd"] == 0
    assert "No agent activity in this window." in result["summary_text"]
    assert len(run(list_nightly_runs(pid))) == 1


def test_invalid_window_and_unknown_project(seed):
    pid = seed.project("Alpha")
    with pytest.raises(ValueError):
        run(build_nightly_summary(pid, UNTIL, SINCE))
    with pytest.raises(LookupError):
        run(build_nightly_summary("no-such-project", SINCE, UNTIL))


# ── last_closed_window ──

@pytest.mark.parametrize("now, expected", [
    (datetime(2030, 1, 2, 7, 0, tzinfo=UTC),
     (datetime(2030, 1, 1, 22, 0, tzinfo=UTC), datetime(2030, 1, 2, 6, 0, tzinfo=UTC))),
    (datetime(2030, 1, 1, 5, 0, tzinfo=UTC),  # current window still open → the one before, across the year boundary
     (datetime(2029, 12, 30, 22, 0, tzinfo=UTC), datetime(2029, 12, 31, 6, 0, tzinfo=UTC))),
    (datetime(2030, 1, 2, 6, 0, tzinfo=UTC),  # the moment the window closes
     (datetime(2030, 1, 1, 22, 0, tzinfo=UTC), datetime(2030, 1, 2, 6, 0, tzinfo=UTC))),
    (datetime(2030, 1, 2, 7, 0),  # naive → treated as UTC
     (datetime(2030, 1, 1, 22, 0, tzinfo=UTC), datetime(2030, 1, 2, 6, 0, tzinfo=UTC))),
])
def test_last_closed_window_overnight(now, expected):
    assert last_closed_window(now, "22:00", "06:00") == expected


def test_last_closed_window_same_day():
    assert last_closed_window(datetime(2030, 1, 2, 12, 0, tzinfo=UTC), "01:00", "05:00") == (
        datetime(2030, 1, 2, 1, 0, tzinfo=UTC), datetime(2030, 1, 2, 5, 0, tzinfo=UTC))
    assert last_closed_window(datetime(2030, 1, 2, 3, 0, tzinfo=UTC), "01:00", "05:00") == (
        datetime(2030, 1, 1, 1, 0, tzinfo=UTC), datetime(2030, 1, 1, 5, 0, tzinfo=UTC))


# ── maybe_run_nightly_summary ──

def test_maybe_run_summarizes_active_projects_once(seed, monkeypatch):
    monkeypatch.setattr(nightly_summary, "NIGHT_WINDOW_START", "22:00")
    monkeypatch.setattr(nightly_summary, "NIGHT_WINDOW_END", "06:00")
    alpha, beta, idle = seed.project("Alpha"), seed.project("Beta"), seed.project("Idle")
    seed.session(seed.mission(alpha, "A1"), "completed", sql_ts(night(3)), 1.0, 10)
    seed.session(seed.mission(beta, "B1"), "failed", iso_ts(night(4)), 2.0, 20)
    seed.session(seed.mission(idle, "I1"), "completed", sql_ts(SINCE - timedelta(days=1)), 3.0, 30)

    # No-arg form derives the 22:00–06:00 window that closed an hour before `now`
    first = run(maybe_run_nightly_summary(now=UNTIL + timedelta(hours=1)))
    assert {r["project_id"] for r in first} == {alpha, beta}

    # Same window again — explicit bounds this time — is a no-op
    assert run(maybe_run_nightly_summary(since=SINCE, until=UNTIL)) == []
    assert len(run(list_nightly_runs(alpha))) == 1
    assert len(run(list_nightly_runs(beta))) == 1
    assert run(list_nightly_runs(idle)) == []


def test_maybe_run_requires_both_bounds(temp_db):
    with pytest.raises(ValueError):
        run(maybe_run_nightly_summary(since=SINCE))


# ── API ──

@pytest.fixture
def client(temp_db):
    app = FastAPI()
    app.include_router(nightly_summary.router)
    with TestClient(app) as c:
        yield c


def test_api_run_then_list(seed, client):
    pid = seed.project("Alpha")
    mid = seed.mission(pid, "Ship it")
    sid = seed.session(mid, "completed", sql_ts(night(5)), 1.5, 500)
    seed.report(sid, mid, "Shipped the feature", sql_ts(night(5, seconds=1)))

    resp = client.post(f"/api/projects/{pid}/nightly-runs/run",
                       json={"since": SINCE.isoformat(), "until": UNTIL.isoformat()})
    assert resp.status_code == 201
    body = resp.json()
    assert body["missions_completed"] == 1
    assert body["total_cost_usd"] == pytest.approx(1.5)
    assert "Shipped the feature" in body["summary_text"]

    resp = client.get(f"/api/projects/{pid}/nightly-runs")
    assert resp.status_code == 200
    runs = resp.json()
    assert [r["id"] for r in runs] == [body["id"]]


def test_api_run_defaults_to_last_24_hours(seed, client):
    pid = seed.project("Alpha")
    resp = client.post(f"/api/projects/{pid}/nightly-runs/run")
    assert resp.status_code == 201
    body = resp.json()
    start = datetime.fromisoformat(body["window_start"])
    end = datetime.fromisoformat(body["window_end"])
    assert end - start == timedelta(hours=24)
    assert abs(datetime.now(UTC) - end) < timedelta(minutes=1)


def test_api_errors(seed, client):
    pid = seed.project("Alpha")
    assert client.get("/api/projects/nope/nightly-runs").status_code == 404
    assert client.post("/api/projects/nope/nightly-runs/run").status_code == 404
    resp = client.post(f"/api/projects/{pid}/nightly-runs/run",
                       json={"since": UNTIL.isoformat(), "until": SINCE.isoformat()})
    assert resp.status_code == 400
