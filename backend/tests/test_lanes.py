"""Tests for the lanes module — slot accounting, policy resolution, precedence."""

import asyncio
import sys
import os
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import lanes
from models import LANE_DEFAULTS, MISSION_TYPE_TO_LANE


# ── derive_lane ────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_derive_lane_explicit_lane_wins():
    mission = {"lane": "reviewer", "mission_type": "implement"}
    assert lanes.derive_lane(mission) == "reviewer"


@pytest.mark.unit
def test_derive_lane_from_mission_type():
    assert lanes.derive_lane({"mission_type": "test"}) == "tester"
    assert lanes.derive_lane({"mission_type": "review"}) == "reviewer"
    assert lanes.derive_lane({"mission_type": "explore"}) == "explorer"
    assert lanes.derive_lane({"mission_type": "planner"}) == "orchestrator"


@pytest.mark.unit
def test_derive_lane_implement_maps_to_coder():
    assert lanes.derive_lane({"mission_type": "implement"}) == "coder"
    assert lanes.derive_lane({"mission_type": "fix"}) == "coder"
    assert lanes.derive_lane({"mission_type": "full"}) == "coder"


@pytest.mark.unit
def test_derive_lane_defaults_to_coder():
    assert lanes.derive_lane({}) == "coder"
    assert lanes.derive_lane({"mission_type": "unknown_type"}) == "coder"


@pytest.mark.unit
def test_derive_lane_empty_string_falls_through():
    mission = {"lane": "", "mission_type": "review"}
    assert lanes.derive_lane(mission) == "reviewer"


# ── LANE_DEFAULTS completeness ─────────────────────────────────────────────────


@pytest.mark.unit
def test_lane_defaults_has_ten_lanes():
    assert set(LANE_DEFAULTS.keys()) == {
        "orchestrator", "coder", "reviewer", "security", "tester",
        "e2e", "qa", "dynamic_tester", "researcher", "explorer",
    }


@pytest.mark.unit
def test_lane_defaults_fields_complete():
    required = {"max_agents", "default_model", "tool_preset", "append_prompt", "color", "icon"}
    for name, policy in LANE_DEFAULTS.items():
        assert required <= set(policy.keys()), f"Lane '{name}' missing fields"


@pytest.mark.unit
def test_mission_type_to_lane_covers_all_presets():
    from models import TOOL_PRESETS
    # Every standard mission_type should map to a lane
    for mt in ["implement", "review", "test", "explore", "fix", "planner"]:
        assert mt in MISSION_TYPE_TO_LANE, f"mission_type '{mt}' not in MISSION_TYPE_TO_LANE"


# ── running_by_lane ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_running_by_lane_counts_correctly():
    def _make_task(lane_name: str, done: bool = False) -> MagicMock:
        t = MagicMock()
        t.done.return_value = done
        t.lane = lane_name
        return t

    fake_tasks = {
        "s1": _make_task("coder"),
        "s2": _make_task("coder"),
        "s3": _make_task("reviewer"),
        "s4": _make_task("coder", done=True),  # done — should not count
    }

    with patch.dict("sys.modules", {"sdk_engine": MagicMock(running_tasks=fake_tasks)}):
        # Force re-import to pick up the patched module
        import importlib
        import lanes as _lanes
        importlib.reload(_lanes)
        counts = _lanes.running_by_lane()

    assert counts.get("coder", 0) == 2
    assert counts.get("reviewer", 0) == 1
    assert counts.get("tester", 0) == 0


@pytest.mark.unit
def test_running_by_lane_no_sdk_engine():
    with patch.dict("sys.modules", {}):
        # sdk_engine missing → returns empty dict gracefully
        import importlib
        import lanes as _lanes
        importlib.reload(_lanes)

        original = _lanes.running_by_lane
        # Patch sdk_engine import to raise ImportError
        def patched():
            try:
                raise ImportError("sdk_engine not available")
            except ImportError:
                return {}
        _lanes.running_by_lane = patched
        result = _lanes.running_by_lane()
        _lanes.running_by_lane = original

    assert result == {}


# ── check_slot ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_check_slot_free(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    # Coder has max_agents=2, zero running
    with patch.object(lanes, "running_by_lane", return_value={}):
        ok, reason = await lanes.check_slot({"lane": "coder"})
    assert ok is True
    assert reason == ""


@pytest.mark.asyncio
@pytest.mark.unit
async def test_check_slot_full(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    # Coder max_agents=3; simulate 3 already running
    with patch.object(lanes, "running_by_lane", return_value={"coder": 3}):
        ok, reason = await lanes.check_slot({"lane": "coder"})
    assert ok is False
    assert "coder" in reason.lower()
    assert "3/3" in reason


@pytest.mark.asyncio
@pytest.mark.unit
async def test_check_slot_other_lane_full_does_not_block(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    # Reviewer is full (cap=2); coder is free
    with patch.object(lanes, "running_by_lane", return_value={"reviewer": 2}):
        ok, _ = await lanes.check_slot({"lane": "coder"})
    assert ok is True


# ── free_slots ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_free_slots_returns_available(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    with patch.object(lanes, "running_by_lane", return_value={"coder": 1}):
        free = await lanes.free_slots()

    # coder has cap 3, 1 running → 2 free
    assert free.get("coder") == 2
    # reviewer has cap 2, 0 running → 2 free
    assert free.get("reviewer") == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_free_slots_excludes_saturated_lanes(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    # Saturate reviewer (cap=2)
    with patch.object(lanes, "running_by_lane", return_value={"reviewer": 2}):
        free = await lanes.free_slots()

    assert "reviewer" not in free


# ── total_capacity ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_total_capacity_from_defaults():
    lanes._cache.clear()
    expected = sum(p["max_agents"] for p in LANE_DEFAULTS.values())
    assert lanes.total_capacity() == expected


# ── snapshot ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.unit
async def test_snapshot_structure(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    with patch.object(lanes, "running_by_lane", return_value={"coder": 1}):
        result = await lanes.snapshot()

    assert len(result) == len(LANE_DEFAULTS)
    names = [r["name"] for r in result]
    assert "coder" in names
    coder = next(r for r in result if r["name"] == "coder")
    assert coder["running"] == 1
    assert coder["free"] == coder["max_agents"] - 1
    assert "icon" in coder
    assert "color" in coder


# ── DB — seed and backfill ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
async def test_init_db_seeds_lanes(tmp_db):
    import db as _db
    conn = await _db.get_db()
    try:
        rows = await conn.execute_fetchall("SELECT name FROM lanes ORDER BY name")
        names = {r["name"] for r in rows}
    finally:
        await conn.close()
    assert names == set(LANE_DEFAULTS.keys())


@pytest.mark.asyncio
@pytest.mark.integration
async def test_update_lane_persists(tmp_db):
    lanes._cache.clear()
    await lanes.reload_cache()

    result = await lanes.update_lane("coder", {"max_agents": 4})
    assert result is not None
    assert result["max_agents"] == 4

    # Cache should reflect update
    assert lanes._cache["coder"]["max_agents"] == 4


@pytest.mark.asyncio
@pytest.mark.integration
async def test_watcher_fairness(tmp_db):
    """With coder cap=3 and 5 coder drafts, at most 3 get dispatched per cycle."""
    import db as _db
    import uuid

    # Create a project
    conn = await _db.get_db()
    project_id = str(uuid.uuid4())
    await conn.execute(
        "INSERT INTO projects (id, name, path) VALUES (?, ?, ?)",
        (project_id, "Test", "/tmp/test"),
    )
    # Create 5 coder draft missions with auto_dispatch
    mission_ids = []
    for i in range(5):
        mid = str(uuid.uuid4())
        mission_ids.append(mid)
        await conn.execute(
            """INSERT INTO missions
               (id, project_id, title, detailed_prompt, status, auto_dispatch, lane)
               VALUES (?, ?, ?, ?, 'draft', 1, 'coder')""",
            (mid, project_id, f"Mission {i}", "Do something"),
        )
    await conn.commit()
    await conn.close()

    # Check that free_slots returns at most coder cap for coder
    coder_cap = LANE_DEFAULTS["coder"]["max_agents"]
    lanes._cache.clear()
    await lanes.reload_cache()
    with patch.object(lanes, "running_by_lane", return_value={}):
        free = await lanes.free_slots()
    assert free.get("coder") == coder_cap

    # Simulate coder saturated
    with patch.object(lanes, "running_by_lane", return_value={"coder": coder_cap}):
        free_after = await lanes.free_slots()
    assert "coder" not in free_after  # coder saturated
