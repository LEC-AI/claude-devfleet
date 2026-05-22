"""Tests for Pydantic models — validation, defaults, optional fields."""

import pytest
from pydantic import ValidationError

from models import (
    LANE_DEFAULTS,
    MISSION_TYPE_TO_LANE,
    MODEL_CHOICES,
    CeilingUpdate,
    DispatchOptions,
    LaneCreate,
    LaneUpdate,
    MissionCreate,
    MissionUpdate,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserOut,
)


# ── CeilingUpdate ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_ceiling_update_accepts_zero():
    """0 means 'defer to lane system' — must be accepted."""
    c = CeilingUpdate(max_agents=0)
    assert c.max_agents == 0


@pytest.mark.unit
def test_ceiling_update_accepts_positive():
    c = CeilingUpdate(max_agents=6)
    assert c.max_agents == 6


@pytest.mark.unit
def test_ceiling_update_requires_max_agents():
    with pytest.raises(ValidationError):
        CeilingUpdate()


@pytest.mark.unit
def test_ceiling_update_rejects_string():
    with pytest.raises(ValidationError):
        CeilingUpdate(max_agents="six")


# ── UserCreate / UserLogin ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_user_create_requires_all_fields():
    with pytest.raises(ValidationError):
        UserCreate(email="x@y.com")
    with pytest.raises(ValidationError):
        UserCreate(email="x@y.com", password="pass")


@pytest.mark.unit
def test_user_create_accepts_valid_input():
    u = UserCreate(email="hasan@devfleet.local", password="pw123", invite_token="abc-123")
    assert u.email == "hasan@devfleet.local"
    assert u.invite_token == "abc-123"


@pytest.mark.unit
def test_user_login_requires_email_and_password():
    with pytest.raises(ValidationError):
        UserLogin(email="x@y.com")


@pytest.mark.unit
def test_user_out_role_field():
    u = UserOut(id="u1", email="x@y.com", role="admin")
    assert u.role == "admin"


# ── TokenResponse ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_token_response_structure():
    user = UserOut(id="u1", email="a@b.com", role="user")
    t = TokenResponse(access_token="tok", token_type="bearer", user=user)
    assert t.access_token == "tok"
    assert t.user.email == "a@b.com"


# ── DispatchOptions ────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_dispatch_options_lane_optional():
    opts = DispatchOptions()
    assert opts.lane is None


@pytest.mark.unit
def test_dispatch_options_accepts_lane():
    opts = DispatchOptions(lane="reviewer")
    assert opts.lane == "reviewer"


# ── MissionCreate / MissionUpdate ─────────────────────────────────────────────


@pytest.mark.unit
def test_mission_create_lane_field_optional():
    m = MissionCreate(
        project_id="p1",
        title="t",
        detailed_prompt="dp",
    )
    assert m.lane is None


@pytest.mark.unit
def test_mission_create_accepts_lane():
    m = MissionCreate(
        project_id="p1",
        title="t",
        detailed_prompt="dp",
        lane="coder",
    )
    assert m.lane == "coder"


@pytest.mark.unit
def test_mission_update_all_optional():
    """MissionUpdate should accept partial patches."""
    u = MissionUpdate(status="done")
    assert u.status == "done"
    assert u.title is None
    assert u.lane is None


# ── LaneCreate / LaneUpdate ────────────────────────────────────────────────────


@pytest.mark.unit
def test_lane_create_requires_name():
    """LaneCreate requires name; other fields have sensible defaults."""
    with pytest.raises(ValidationError):
        LaneCreate()


@pytest.mark.unit
def test_lane_create_defaults_applied():
    lc = LaneCreate(name="newlane")
    assert lc.max_agents == 1
    assert lc.default_model == "claude-sonnet-4-6"
    assert lc.tool_preset == "implement"
    assert lc.color == "#888888"


@pytest.mark.unit
def test_lane_create_full():
    lc = LaneCreate(
        name="newlane",
        max_agents=2,
        default_model="claude-sonnet-4-6",
        tool_preset="implement",
        append_prompt="extra",
        color="#fff",
        icon="rocket",
    )
    assert lc.name == "newlane"
    assert lc.max_agents == 2


@pytest.mark.unit
def test_lane_update_partial():
    """LaneUpdate fields all Optional — partial patches must work."""
    u = LaneUpdate(max_agents=5)
    assert u.max_agents == 5
    assert u.default_model is None


@pytest.mark.unit
def test_lane_update_enabled_field():
    u = LaneUpdate(enabled=False)
    assert u.enabled is False


# ── LANE_DEFAULTS structure ────────────────────────────────────────────────────


@pytest.mark.unit
def test_lane_defaults_has_ten_lanes():
    expected = {
        "orchestrator", "coder", "reviewer", "security", "tester",
        "e2e", "qa", "dynamic_tester", "researcher", "explorer",
    }
    assert set(LANE_DEFAULTS.keys()) == expected


@pytest.mark.unit
def test_lane_defaults_total_capacity_is_18():
    total = sum(p["max_agents"] for p in LANE_DEFAULTS.values())
    assert total == 18


@pytest.mark.unit
def test_lane_defaults_all_models_in_model_choices():
    for name, policy in LANE_DEFAULTS.items():
        assert policy["default_model"] in MODEL_CHOICES, (
            f"Lane {name} model {policy['default_model']} not in MODEL_CHOICES"
        )


@pytest.mark.unit
def test_lane_defaults_has_required_fields():
    required = {"max_agents", "default_model", "tool_preset", "append_prompt", "color", "icon"}
    for name, policy in LANE_DEFAULTS.items():
        missing = required - set(policy.keys())
        assert not missing, f"Lane {name} missing fields: {missing}"


@pytest.mark.unit
def test_mission_type_to_lane_targets_exist():
    """Every lane name in MISSION_TYPE_TO_LANE values must exist in LANE_DEFAULTS."""
    for mt, lane in MISSION_TYPE_TO_LANE.items():
        assert lane in LANE_DEFAULTS, (
            f"mission_type {mt} → lane {lane} but {lane} is not in LANE_DEFAULTS"
        )


@pytest.mark.unit
def test_model_choices_includes_current_models():
    """MODEL_CHOICES must include the three production models."""
    assert "claude-opus-4-7" in MODEL_CHOICES
    assert "claude-sonnet-4-6" in MODEL_CHOICES
    assert "claude-haiku-4-5-20251001" in MODEL_CHOICES
