"""
Runnable check for routes_swarm_tree. No framework, no fixtures:

    cd backend && python3 test_swarm_tree.py

Seeds a throw-away SQLite DB with a swarm root, three children
(2 running, 1 draft blocked on the other two), one grandchild spawned by a
running child, and two sessions on one mission (to prove "latest" wins).
Exercises both the core functions and the HTTP routes (router mounted on a
private FastAPI app — app.py is not touched).
"""
import asyncio
import os
import sys
import tempfile
import uuid

_tmp = tempfile.mkdtemp(prefix="devfleet-swarm-test-")
os.environ["DEVFLEET_DB"] = os.path.join(_tmp, "test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402  (must come after DEVFLEET_DB is set)
import routes_swarm_tree as swarm  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def uid():
    return str(uuid.uuid4())


PROJECT = uid()
ROOT = uid()
BACKEND = uid()
FRONTEND = uid()
TESTS = uid()
GRANDCHILD = uid()
OTHER_ROOT = uid()          # a second swarm root with no children
PLAIN_MISSION = uid()       # not a swarm, tags malformed on purpose


async def seed():
    await db.init_db()
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
            (PROJECT, "Swarm Test Project", "/tmp/swarm-test"),
        )

        def mission(mid, title, status, parent=None, depends=None, tags="[]", created="2026-09-24 01:00:00"):
            return conn.execute(
                """INSERT INTO missions (id, project_id, title, detailed_prompt, status,
                                         parent_mission_id, depends_on, auto_dispatch, tags, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (mid, PROJECT, title, "prompt", status, parent,
                 __import__("json").dumps(depends or []), tags, created),
            )

        await mission(ROOT, "Swarm: add JWT auth", "draft", tags='["swarm_root"]', created="2026-09-24 00:00:00")
        await mission(BACKEND, "Backend middleware", "running", parent=ROOT, created="2026-09-24 00:01:00")
        await mission(FRONTEND, "Frontend login form", "running", parent=ROOT, created="2026-09-24 00:02:00")
        await mission(TESTS, "Integration tests", "draft", parent=ROOT,
                      depends=[BACKEND, FRONTEND], created="2026-09-24 00:03:00")
        # A running agent spawned a sub-mission of its own (create_sub_mission → parent = BACKEND)
        await mission(GRANDCHILD, "Write auth unit tests", "completed", parent=BACKEND, created="2026-09-24 00:30:00")
        await mission(OTHER_ROOT, "Swarm: empty", "draft", tags='["swarm_root","nightly"]', created="2026-09-23 00:00:00")
        await mission(PLAIN_MISSION, "Not a swarm", "draft", tags="{not json", created="2026-09-22 00:00:00")

        def session(mid, cost, tokens, started, status="running"):
            return conn.execute(
                """INSERT INTO agent_sessions (id, mission_id, status, started_at, total_cost_usd, total_tokens)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uid(), mid, status, started, cost, tokens),
            )

        # BACKEND has two sessions: an older failed one (cost 9.99) and the latest running one (0.42)
        await session(BACKEND, 9.99, 999_999, "2026-09-24 00:05:00", status="failed")
        await session(BACKEND, 0.42, 12_000, "2026-09-24 00:10:00")
        await session(FRONTEND, 0.31, 8_000, "2026-09-24 00:06:00")
        await session(GRANDCHILD, 0.10, 2_000, "2026-09-24 00:31:00", status="completed")
        await conn.commit()
    finally:
        await conn.close()


async def check_core():
    # 404 on unknown id
    try:
        await swarm.build_tree("does-not-exist")
        raise AssertionError("expected 404")
    except swarm.HTTPException as e:
        assert e.status_code == 404, e

    tree = await swarm.build_tree(ROOT)
    assert tree["root"]["id"] == ROOT
    assert tree["is_swarm_root"] is True
    assert tree["truncated"] is False
    assert tree["root"]["latest_session"] is None

    by_id = {m["id"]: m for m in tree["missions"]}
    assert set(by_id) == {BACKEND, FRONTEND, TESTS, GRANDCHILD}, set(by_id)

    # Linkage + depth
    assert by_id[BACKEND]["parent_mission_id"] == ROOT and by_id[BACKEND]["depth"] == 1
    assert by_id[FRONTEND]["parent_mission_id"] == ROOT and by_id[FRONTEND]["depth"] == 1
    assert by_id[TESTS]["parent_mission_id"] == ROOT and by_id[TESTS]["depth"] == 1
    assert by_id[GRANDCHILD]["parent_mission_id"] == BACKEND and by_id[GRANDCHILD]["depth"] == 2
    # Stable order: depth, then created_at
    assert [m["id"] for m in tree["missions"]] == [BACKEND, FRONTEND, TESTS, GRANDCHILD]

    # Dependencies / blocked_on
    assert set(by_id[TESTS]["depends_on"]) == {BACKEND, FRONTEND}
    assert set(by_id[TESTS]["blocked_on"]) == {BACKEND, FRONTEND}
    assert by_id[BACKEND]["blocked_on"] == []  # not draft → never "blocked"

    # Latest session wins over the older, pricier one
    assert by_id[BACKEND]["latest_session"]["total_cost_usd"] == 0.42
    assert by_id[BACKEND]["latest_session"]["total_tokens"] == 12_000
    assert by_id[BACKEND]["latest_session"]["status"] == "running"
    assert by_id[TESTS]["latest_session"] is None

    # Summary roll-up
    s = tree["summary"]
    assert s["total"] == 4
    assert s["counts"]["running"] == 2 and s["counts"]["draft"] == 1 and s["counts"]["completed"] == 1
    assert s["counts"]["failed"] == 0 and s["counts"]["cancelled"] == 0
    assert s["blocked"] == 1
    assert abs(s["total_cost_usd"] - (0.42 + 0.31 + 0.10)) < 1e-9, s["total_cost_usd"]
    assert s["total_tokens"] == 22_000
    assert s["is_active"] is True

    # A non-swarm-root mission still gets a (generic) tree, flagged accordingly
    plain = await swarm.build_tree(PLAIN_MISSION)
    assert plain["is_swarm_root"] is False and plain["missions"] == []
    assert plain["root"]["tags"] == []  # malformed JSON tolerated

    # Listing swarms: only tagged roots, newest first, with roll-ups
    page = await swarm.list_swarms()
    roots = page["items"]
    assert page["total"] == 2 and page["limit"] == swarm.LIST_DEFAULT_LIMIT and page["offset"] == 0
    assert [r["id"] for r in roots] == [ROOT, OTHER_ROOT], [r["title"] for r in roots]
    assert roots[0]["summary"]["total"] == 4 and roots[1]["summary"]["total"] == 0
    assert roots[1]["summary"]["is_active"] is False
    assert roots[0]["project_name"] == "Swarm Test Project"
    assert roots[0]["summary"]["cost_basis"] == "latest_session"
    assert (await swarm.list_swarms(project_id="nope"))["items"] == []
    assert len((await swarm.list_swarms(project_id=PROJECT))["items"]) == 2
    # pagination
    p1 = await swarm.list_swarms(limit=1, offset=0)
    p2 = await swarm.list_swarms(limit=1, offset=1)
    assert [r["id"] for r in p1["items"]] == [ROOT] and [r["id"] for r in p2["items"]] == [OTHER_ROOT]
    assert p1["total"] == 2 and (await swarm.list_swarms(limit=1, offset=5))["items"] == []


async def check_completion_unblocks():
    """Once deps complete, the tests mission is no longer blocked and the swarm can go inactive."""
    conn = await db.get_db()
    try:
        for mid in (BACKEND, FRONTEND):
            await conn.execute("UPDATE missions SET status='completed' WHERE id=?", (mid,))
        await conn.commit()
    finally:
        await conn.close()
    tree = await swarm.build_tree(ROOT)
    tests = next(m for m in tree["missions"] if m["id"] == TESTS)
    assert tests["blocked_on"] == [] and tests["status"] == "draft"
    assert tree["summary"]["blocked"] == 0
    assert tree["summary"]["is_active"] is True  # TESTS is still draft (non-terminal)

    conn = await db.get_db()
    try:
        await conn.execute("UPDATE missions SET status='completed' WHERE id=?", (TESTS,))
        await conn.commit()
    finally:
        await conn.close()
    tree = await swarm.build_tree(ROOT)
    assert tree["summary"]["is_active"] is False
    assert tree["summary"]["counts"]["completed"] == 4


async def check_cycles_and_depth():
    """Parent cycles must yield a finite, duplicate-free tree with honest counts."""
    a, b, c = uid(), uid(), uid()
    conn = await db.get_db()
    try:
        # Root A whose parent points back at its own child B (A → B → A)
        await conn.execute(
            "INSERT INTO missions (id, project_id, title, detailed_prompt, status, parent_mission_id, tags) VALUES (?,?,?,?,?,?,?)",
            (a, PROJECT, "cycle a", "p", "draft", b, '["swarm_root"]'))
        await conn.execute(
            "INSERT INTO missions (id, project_id, title, detailed_prompt, status, parent_mission_id) VALUES (?,?,?,?,?,?)",
            (b, PROJECT, "cycle b", "p", "running", a))
        # A legitimate child of B, to prove the walk continues past the guard
        await conn.execute(
            "INSERT INTO missions (id, project_id, title, detailed_prompt, status, parent_mission_id) VALUES (?,?,?,?,?,?)",
            (c, PROJECT, "leaf c", "p", "completed", b))
        await conn.commit()
    finally:
        await conn.close()

    tree = await swarm.build_tree(a)
    ids = [m["id"] for m in tree["missions"]]
    assert ids == [b, c], ids                      # finite, each id exactly once, root never re-entered
    assert len(ids) == len(set(ids))
    assert tree["cycle_detected"] is True
    assert tree["truncated"] is False
    assert tree["summary"]["total"] == 2           # not inflated by repeated rows
    assert tree["summary"]["counts"]["running"] == 1 and tree["summary"]["counts"]["completed"] == 1
    # The same root through the list endpoint agrees
    listed = next(r for r in (await swarm.list_swarms())["items"] if r["id"] == a)
    assert listed["summary"]["total"] == 2 and listed["cycle_detected"] is True

    # Viewed from B (a non-root member of the cycle) the walk also terminates: B → C, and B → A → (B blocked)
    tree_b = await swarm.build_tree(b)
    ids_b = [m["id"] for m in tree_b["missions"]]
    assert sorted(ids_b) == sorted([a, c]) and len(ids_b) == 2, ids_b

    # A legitimately deep chain is cut at MAX_DEPTH and flagged truncated, never cycle_detected
    chain_root = uid()
    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO missions (id, project_id, title, detailed_prompt, status, tags) VALUES (?,?,?,?,?,?)",
            (chain_root, PROJECT, "deep root", "p", "draft", '["swarm_root"]'))
        parent = chain_root
        chain_ids = []
        for i in range(swarm.MAX_DEPTH + 3):
            mid = uid()
            chain_ids.append(mid)
            await conn.execute(
                "INSERT INTO missions (id, project_id, title, detailed_prompt, status, parent_mission_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (mid, PROJECT, f"depth {i+1}", "p", "completed", parent, f"2026-09-24 02:{i:02d}:00"))
            parent = mid
        await conn.commit()
    finally:
        await conn.close()
    deep = await swarm.build_tree(chain_root)
    assert deep["truncated"] is True and deep["cycle_detected"] is False
    assert len(deep["missions"]) == swarm.MAX_DEPTH
    assert deep["missions"][-1]["depth"] == swarm.MAX_DEPTH

    # clean up so the HTTP checks see the original fixture
    conn = await db.get_db()
    try:
        await conn.execute("DELETE FROM missions WHERE id IN (?,?,?,?)", (a, b, c, chain_root))
        placeholders = ",".join("?" for _ in chain_ids)
        await conn.execute(f"DELETE FROM missions WHERE id IN ({placeholders})", tuple(chain_ids))
        await conn.commit()
    finally:
        await conn.close()


def check_http():
    app = FastAPI()
    app.include_router(swarm.router)  # exactly the integration line
    client = TestClient(app)

    r = client.get("/api/swarms/nope/tree")
    assert r.status_code == 404, r.text

    r = client.get(f"/api/swarms/{ROOT}/tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"]["id"] == ROOT and len(body["missions"]) == 4
    assert "generated_at" in body

    r = client.get("/api/swarms")
    assert r.status_code == 200 and [x["id"] for x in r.json()["items"]] == [ROOT, OTHER_ROOT]
    r = client.get(f"/api/swarms?project_id={PROJECT}")
    assert len(r.json()["items"]) == 2 and r.json()["total"] == 2
    r = client.get("/api/swarms?limit=1&offset=1")
    assert [x["id"] for x in r.json()["items"]] == [OTHER_ROOT]
    assert client.get("/api/swarms?limit=0").status_code == 422
    assert client.get(f"/api/swarms?limit={swarm.LIST_MAX_LIMIT + 1}").status_code == 422


def main():
    asyncio.run(seed())
    asyncio.run(check_core())
    asyncio.run(check_cycles_and_depth())
    check_http()
    asyncio.run(check_completion_unblocks())
    print("routes_swarm_tree: all checks passed")


if __name__ == "__main__":
    main()
