"""
Standalone check for Track 1 (swarm planner). No framework needed:

    cd backend && python test_swarm_planner.py

Uses a temp SQLite DB, a canned planner response (no Claude call), and a fake
sdk_engine so mission_watcher's real eligibility + dispatch code runs without
spawning agents.
"""

import asyncio
import json
import os
import sys
import tempfile
import types

_tmp = tempfile.mkdtemp()
os.environ["DEVFLEET_DB"] = os.path.join(_tmp, "test.db")
# Host path /devfleet-host-swarmtest is "mounted" at _tmp, as in Docker.
# Must be set before app is imported (it reads the maps at import time).
_HOST_PATH = "/devfleet-host-swarmtest"
os.environ["DEVFLEET_PATH_MAP_SWARMTEST"] = f"{_HOST_PATH}:{_tmp}"

# Fake sdk_engine: mission_watcher imports dispatch_mission/running_tasks from it
_fake_engine = types.ModuleType("sdk_engine")
_fake_engine.running_tasks = {}
_dispatched: list[str] = []


async def _fake_dispatch(session_id, mission, last_report):
    _dispatched.append(mission["id"])
    await asyncio.sleep(3600)  # stays "running" until cancelled


_fake_engine.dispatch_mission = _fake_dispatch
sys.modules["sdk_engine"] = _fake_engine

import db  # noqa: E402
import mission_watcher  # noqa: E402
import swarm_planner  # noqa: E402
from claude_code_sdk.types import AssistantMessage, ResultMessage, TextBlock  # noqa: E402

_real_call_planner = swarm_planner._call_planner
_planner_cwds: list[str] = []

JWT_PLAN = [
    {"title": "Backend JWT middleware", "detailed_prompt": "Add JWT middleware in backend/auth.py",
     "acceptance_criteria": "- protected routes return 401 without token", "priority": 3,
     "depends_on_index": []},
    {"title": "Frontend login form", "detailed_prompt": "Add Login.jsx that stores the token",
     "acceptance_criteria": "- form posts credentials", "priority": 3, "depends_on_index": []},
    {"title": "Integration tests", "detailed_prompt": "Test login → protected route end to end",
     "acceptance_criteria": "- tests pass", "priority": 2, "depends_on_index": [0, 1]},
]


async def _fake_planner(prompt, cwd):
    _planner_cwds.append(cwd)
    assert "add JWT auth" in prompt
    assert "tests/lint/build" in prompt, "planner prompt must carry the quality gate"
    return "Here is the plan:\n```json\n" + json.dumps(JWT_PLAN) + "\n```"


async def _set_status(mid, status):
    conn = await db.get_db()
    try:
        await conn.execute("UPDATE missions SET status=? WHERE id=?", (status, mid))
        await conn.commit()
    finally:
        await conn.close()


async def _watcher_poll():
    """One iteration of mission_watcher._watch_loop's body."""
    running = sum(1 for t in _fake_engine.running_tasks.values() if not t.done())
    for m in await mission_watcher._find_eligible_missions(limit=mission_watcher.MAX_CONCURRENT_AGENTS - running):
        await mission_watcher._dispatch_eligible(m)
    await asyncio.sleep(0)  # let the dispatched tasks start


def test_validation():
    v = swarm_planner._validate_tasks
    ok = v([{"title": "a", "detailed_prompt": "x"}])
    assert ok[0]["depends_on_index"] == [] and ok[0]["priority"] == 2

    for bad, why in [
        ([], "empty"),
        ([{"title": "a"}], "missing prompt"),
        ([{"title": "a", "detailed_prompt": "x", "depends_on_index": [0]}], "self dep"),
        ([{"title": "a", "detailed_prompt": "x", "depends_on_index": [5]}], "out of range"),
        ([{"title": "a", "detailed_prompt": "x", "depends_on_index": [1]},
          {"title": "b", "detailed_prompt": "y", "depends_on_index": [0]}], "cycle"),
        ([{"title": str(i), "detailed_prompt": "x"} for i in range(21)], "too many"),
    ]:
        try:
            v(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {why}")

    assert swarm_planner._extract_json('noise [{"a": 1}] trailing') == [{"a": 1}]
    print("ok  validation")


def _result(result, is_error=False, subtype="success"):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s", result=result)


async def test_sdk_message_sequence():
    """Real SDK message types through the real _call_planner (only query is faked)."""
    import claude_code_sdk

    plan = json.dumps(JWT_PLAN)
    sequences = {
        "assistant text + identical result": [
            AssistantMessage(content=[TextBlock(text=plan)], model="m"), _result(plan)],
        "result only": [_result(plan)],
        "assistant text, empty result": [
            AssistantMessage(content=[TextBlock(text=plan)], model="m"), _result(None)],
    }
    real_query = claude_code_sdk.query
    try:
        for name, msgs in sequences.items():
            async def _query(prompt, options, _msgs=msgs):
                for m in _msgs:
                    yield m
            claude_code_sdk.query = _query
            out = await _real_call_planner("p", _tmp)
            assert len(swarm_planner._validate_tasks(swarm_planner._extract_json(out))) == 3, name

        async def _error_query(prompt, options):
            yield _result("rate limited", is_error=True, subtype="error_during_execution")
        claude_code_sdk.query = _error_query
        try:
            await _real_call_planner("p", _tmp)
        except ValueError as e:
            assert "error_during_execution" in str(e), e
        else:
            raise AssertionError("expected ValueError on error ResultMessage")
    finally:
        claude_code_sdk.query = real_query
    print("ok  SDK sequence: duplicated assistant+result parses once, error result raises")


async def test_launch_and_watcher():
    await db.init_db()
    conn = await db.get_db()
    try:
        await conn.execute("INSERT INTO projects (id, name, path) VALUES ('p1', 'demo', ?)", (_tmp,))
        await conn.execute("INSERT INTO projects (id, name, path) VALUES ('p2', 'mapped', ?)", (_HOST_PATH,))
        await conn.execute("INSERT INTO projects (id, name, path) VALUES ('p3', 'gone', '/no/such/dir')")
        await conn.commit()
    finally:
        await conn.close()

    swarm_planner._call_planner = _fake_planner
    root_id = await swarm_planner.launch_swarm(
        "p1", "add JWT auth: backend middleware, frontend login form, integration tests", 3)

    conn = await db.get_db()
    try:
        root = dict((await conn.execute_fetchall("SELECT * FROM missions WHERE id=?", (root_id,)))[0])
        kids = [dict(r) for r in await conn.execute_fetchall(
            "SELECT * FROM missions WHERE parent_mission_id=? ORDER BY mission_number", (root_id,))]
    finally:
        await conn.close()

    assert json.loads(root["tags"]) == ["swarm_root"]
    assert root["status"] == "draft" and root["auto_dispatch"] == 0
    assert len(kids) == 3
    backend, frontend, tests = kids
    assert all(k["auto_dispatch"] == 1 and k["status"] == "draft" for k in kids)
    assert set(json.loads(tests["depends_on"])) == {backend["id"], frontend["id"]}
    assert json.loads(backend["depends_on"]) == [] and json.loads(frontend["depends_on"]) == []
    assert all(swarm_planner.QUALITY_GATE in k["detailed_prompt"] for k in kids)
    print("ok  launch_swarm: 3 missions, tests depends_on backend+frontend, quality gate in every prompt")

    # One watcher poll: the two independent missions run, tests stays draft
    await _watcher_poll()
    assert set(_dispatched) == {backend["id"], frontend["id"]}, _dispatched
    s = await swarm_planner.get_swarm_status(root_id)
    assert (s["running"], s["draft"], s["completed"]) == (2, 1, 0), s
    print("ok  watcher poll 1: backend+frontend running, tests draft")

    # Tests mission stays blocked while only one dependency is done
    await _set_status(backend["id"], "completed")
    await _watcher_poll()
    assert tests["id"] not in _dispatched
    print("ok  watcher poll 2: tests still waits with one dependency open")

    await _set_status(frontend["id"], "completed")
    await _watcher_poll()
    assert tests["id"] in _dispatched
    s = await swarm_planner.get_swarm_status(root_id)
    assert (s["running"], s["completed"], s["draft"]) == (1, 2, 0), s
    print("ok  watcher poll 3: tests dispatched once both dependencies completed")

    await _set_status(tests["id"], "completed")
    assert (await swarm_planner.get_swarm_status(root_id))["done"] is True
    assert await swarm_planner.get_swarm_status(backend["id"]) is None  # not a swarm root

    for t in _fake_engine.running_tasks.values():
        t.cancel()
    return root_id


async def test_path_mapping():
    swarm_planner._call_planner = _fake_planner
    _planner_cwds.clear()
    await swarm_planner.launch_swarm("p2", "add JWT auth on a mapped path", 3)
    assert _planner_cwds == [_tmp], _planner_cwds  # host path translated to container path

    try:
        await swarm_planner.launch_swarm("p3", "add JWT auth", 3)
    except LookupError:
        pass
    else:
        raise AssertionError("expected LookupError for a missing project directory")
    print("ok  path mapping: planner runs in the resolved container path; missing dir rejected")


async def test_blocked_by_failure():
    swarm_planner._call_planner = _fake_planner
    root_id = await swarm_planner.launch_swarm("p1", "add JWT auth again", 3)
    conn = await db.get_db()
    try:
        kids = [dict(r) for r in await conn.execute_fetchall(
            "SELECT id FROM missions WHERE parent_mission_id=? ORDER BY mission_number", (root_id,))]
    finally:
        await conn.close()
    await _set_status(kids[0]["id"], "failed")
    s = await swarm_planner.get_swarm_status(root_id)
    assert s["failed"] == 1 and s["blocked_by_failure"] == 1, s
    print("ok  status rollup flags drafts blocked by a failed dependency")


def test_routes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes_swarm import router

    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)
    assert c.post("/api/projects/nope/swarms", json={"goal": "add JWT auth"}).status_code == 404
    assert c.post("/api/projects/p1/swarms", json={"goal": ""}).status_code == 422
    r = c.post("/api/projects/p1/swarms", json={"goal": "add JWT auth via route", "max_agents": 2})
    assert r.status_code == 201, r.text
    sid = r.json()["swarm_id"]
    r = c.get(f"/api/swarms/{sid}")
    assert r.status_code == 200 and r.json()["total"] == 3 and r.json()["draft"] == 3, r.text
    assert c.get("/api/swarms/does-not-exist").status_code == 404

    async def _bad(prompt, cwd):
        return "I can't plan this"
    swarm_planner._call_planner = _bad
    assert c.post("/api/projects/p1/swarms", json={"goal": "add JWT auth"}).status_code == 502
    print("ok  routes: POST/GET, 404s, 422, 502 on unusable plan")


async def _main():
    test_validation()
    await test_sdk_message_sequence()
    await test_launch_and_watcher()
    await test_path_mapping()
    await test_blocked_by_failure()


if __name__ == "__main__":
    asyncio.run(_main())
    test_routes()
    print("\nall swarm planner checks passed")
