"""Standalone Track 3 checks: python backend/test_goals.py (no test framework)."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

import db
import goals


async def expect_error(error_type, operation):
    try:
        await operation
    except error_type:
        return
    raise AssertionError(f"Expected {error_type.__name__}")


async def check_registry():
    # Simulate an existing installation before the goals migration.
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO projects (id, name, path) VALUES ('p', 'Existing', '/tmp')")
        conn.execute("INSERT INTO projects (id, name, path) VALUES ('other', 'Other', '/tmp')")
        conn.execute("INSERT INTO missions (id, project_id, title, detailed_prompt) VALUES ('m', 'p', 'Keep', 'Keep')")
    await db.init_db()
    await db.init_db()  # Migration is repeatable and preserves existing rows.
    assert await goals.get_active_goal("p") is None
    assert await goals.list_goals("p") == []
    first = await goals.create_goal("p", "  Add JWT auth  ")
    initial = await goals.get_goal(first)
    assert initial["goal_text"] == "Add JWT auth"
    assert initial["status"] == "active"
    assert initial["max_iterations"] == 20 and initial["current_iteration"] == 0
    assert initial["created_at"] == initial["updated_at"]

    # Separate connections must not lose updates when callers run concurrently.
    await asyncio.gather(*(goals.increment_iteration(first) for _ in range(20)))
    advanced = await goals.get_goal(first)
    assert advanced["current_iteration"] == 20 and advanced["status"] == "active"
    assert advanced["created_at"] == initial["created_at"]
    assert advanced["updated_at"] > initial["updated_at"]
    second = await goals.create_goal("p", "A second goal", 40)
    assert (await goals.get_active_goal("p"))["id"] == second
    await goals.increment_iteration(first)
    assert (await goals.get_active_goal("p"))["id"] == second
    await goals.update_goal_status(second, "paused", "Waiting for review")
    assert (await goals.get_goal(second))["stopped_reason"] == "Waiting for review"
    assert (await goals.get_active_goal("p"))["id"] == first
    await goals.update_goal_status(second, "active")
    assert (await goals.get_goal(second))["stopped_reason"] is None
    await goals.update_goal_status(second, "stopped", "Cancelled by owner")
    await goals.update_goal_status(first, "complete", "Acceptance criteria met")
    assert await goals.get_active_goal("p") is None
    assert [g["id"] for g in await goals.list_goals("p")] == [second, first]
    assert await goals.list_goals("other") == []
    assert await goals.get_goal("missing") is None
    await expect_error(LookupError, goals.create_goal("missing", "No project"))
    await expect_error(LookupError, goals.update_goal_status("missing", "paused"))
    await expect_error(LookupError, goals.increment_iteration("missing"))
    await expect_error(ValueError, goals.create_goal("p", " \n"))
    for invalid in (0, -1, True, 1.5, "20"):
        await expect_error(ValueError, goals.create_goal("p", "Invalid", invalid))
    await expect_error(ValueError, goals.update_goal_status(first, "invalid"))
    await db.init_db()
    assert (await goals.get_goal(first))["current_iteration"] == 21
    with sqlite3.connect(db.DB_PATH) as conn:
        assert conn.execute("SELECT title FROM missions WHERE id = 'm'").fetchone() == ("Keep",)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM projects WHERE id = 'p'")
    assert await goals.list_goals("p") == []
    print("PASS: migrations, CRUD, concurrent increments, validation, and cascade")


async def check_routes():
    import httpx
    from fastapi import FastAPI
    from routes_goals import router

    # A stub app exercises real HTTP validation without starting dispatch services.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/projects/other/goals"
        response = await client.get(url)
        assert response.status_code == 200 and response.json() == []
        response = await client.post(url, json={"goal_text": "Ship auth", "max_iterations": 7})
        assert response.status_code == 201, response.text
        goal = response.json()
        assert goal["max_iterations"] == 7 and goal["status"] == "active"
        for status in ("paused", "active", "stopped"):
            response = await client.patch(f"/api/goals/{goal['id']}", json={"status": status, "reason": "Manual"})
            assert response.status_code == 200, response.text
            assert response.json()["status"] == status
            assert response.json()["stopped_reason"] == (None if status == "active" else "Manual")
        assert len((await client.get(url)).json()) == 1
        for body in ({}, {"goal_text": " "}, {"goal_text": "x", "max_iterations": 0},
                     {"goal_text": "x", "max_iterations": True}, {"goal_text": "x", "max_iterations": 2.5},
                     {"goal_text": "x", "unexpected": 1}):
            assert (await client.post(url, json=body)).status_code == 422
        for body in ({}, {"status": "invalid"}, {"status": "complete"}, {"status": "paused", "reason": 3}):
            assert (await client.patch(f"/api/goals/{goal['id']}", json=body)).status_code == 422
        assert (await client.get("/api/projects/missing/goals")).status_code == 404
        assert (await client.post("/api/projects/missing/goals", json={"goal_text": "x"})).status_code == 404
        assert (await client.patch("/api/goals/missing", json={"status": "paused"})).status_code == 404
    print("PASS: POST/GET/PATCH API, status/reason handling, 404 and 422 responses")


async def writer():
    await db.init_db()
    conn = await db.get_db()
    try:
        await conn.execute("INSERT INTO projects (id, name, path) VALUES ('restart', 'Restart', '/tmp')")
        await conn.commit()
    finally:
        await conn.close()
    goal_id = await goals.create_goal("restart", "Survive a process kill", 30)
    for _ in range(3):
        await goals.increment_iteration(goal_id)
    print(json.dumps(await goals.get_active_goal("restart")), flush=True)
    await asyncio.Event().wait()  # Parent kills this process after commit.


async def reader():
    await db.init_db()
    print(json.dumps(await goals.get_active_goal("restart")), flush=True)


async def check_restart():
    command = [sys.executable, str(Path(__file__).resolve())]
    process = await asyncio.create_subprocess_exec(
        *command, "--writer", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
        assert line, (await process.stderr.read()).decode()
        before = json.loads(line)
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    assert process.returncode != 0
    result = subprocess.run([*command, "--reader"], capture_output=True, text=True, timeout=30, check=True)
    after = json.loads(result.stdout)
    assert after == before
    assert after["status"] == "active" and after["current_iteration"] == 3
    assert after["goal_text"] == "Survive a process kill" and after["max_iterations"] == 30
    print("PASS: killed writer and fresh reader preserve the entire active goal and iteration")


async def main():
    await check_registry()
    await check_routes()
    await check_restart()


if __name__ == "__main__":
    if sys.argv[1:] == ["--writer"]:
        asyncio.run(writer())
    elif sys.argv[1:] == ["--reader"]:
        asyncio.run(reader())
    else:
        # Always override DB_PATH: never open a developer's or production database.
        with tempfile.TemporaryDirectory(prefix="devfleet-goals-") as directory:
            db.DB_PATH = os.path.join(directory, "goals.db")
            os.environ["DEVFLEET_DB"] = db.DB_PATH
            asyncio.run(main())
