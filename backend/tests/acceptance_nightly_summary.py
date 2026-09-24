"""Framework-free acceptance check for the nightly summary (no pytest needed).

Given 3 completed agent_sessions with known total_cost_usd in a time range,
build_nightly_summary returns the correct sum and count, and the digest text
includes each mission's what_done.

Run from the repo root:  python backend/tests/acceptance_nightly_summary.py
Exit code 0 = pass, 1 = fail. Uses a throwaway database; never touches data/.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import nightly_summary  # noqa: E402

SINCE = datetime(2030, 1, 1, 22, 0, tzinfo=timezone.utc)
UNTIL = SINCE + timedelta(hours=8)
MISSIONS = [  # title, total_cost_usd, total_tokens, what_done
    ("Add login page", 1.25, 1000, "Built the login form with validation"),
    ("Fix flaky test", 2.50, 2000, "Stabilised the websocket reconnect test"),
    ("Write API docs", 0.75, 3000, "Documented every /api/projects endpoint"),
]


def seed(path: str) -> str:
    conn = sqlite3.connect(path, isolation_level=None)
    pid = str(uuid.uuid4())
    conn.execute("INSERT INTO projects (id, name, path) VALUES (?, 'Acceptance', '/tmp/acceptance')", (pid,))
    for number, (title, cost, tokens, what_done) in enumerate(MISSIONS, start=1):
        mid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        ended = SINCE + timedelta(hours=number)
        conn.execute(
            "INSERT INTO missions (id, project_id, title, detailed_prompt, mission_number) VALUES (?, ?, ?, ?, ?)",
            (mid, pid, title, title, number),
        )
        conn.execute(
            """INSERT INTO agent_sessions (id, mission_id, status, ended_at, total_cost_usd, total_tokens)
               VALUES (?, ?, 'completed', ?, ?, ?)""",
            (sid, mid, ended.isoformat(), cost, tokens),
        )
        conn.execute(
            "INSERT INTO reports (id, session_id, mission_id, what_done, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), sid, mid, what_done, ended.strftime("%Y-%m-%d %H:%M:%S")),
        )
    conn.close()
    return pid


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db.DB_PATH = os.path.join(tmp, "acceptance.db")
        await db.init_db()
        pid = seed(db.DB_PATH)

        result = await nightly_summary.build_nightly_summary(pid, SINCE, UNTIL)
        stored = await nightly_summary.list_nightly_runs(pid)

    expected_cost = sum(cost for _, cost, _, _ in MISSIONS)
    expected_tokens = sum(tokens for _, _, tokens, _ in MISSIONS)
    checks = [
        ("missions_completed == 3", result["missions_completed"] == 3),
        ("missions_failed == 0", result["missions_failed"] == 0),
        (f"total_cost_usd == {expected_cost}", abs(result["total_cost_usd"] - expected_cost) < 1e-9),
        (f"total_tokens == {expected_tokens}", result["total_tokens"] == expected_tokens),
        *((f"digest includes: {what_done!r}", what_done in result["summary_text"])
          for _, _, _, what_done in MISSIONS),
        ("stored as one nightly_runs row", len(stored) == 1 and stored[0]["id"] == result["id"]),
    ]
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    passed = all(ok for _, ok in checks)
    print("\nAcceptance check", "PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
