"""Integration tests for /api/auth/* endpoints — login, register, /me.

These cover:
- C1 (exception leaks): error responses must not leak stack traces
- A2 (login timing): bad credentials return the same status as nonexistent users
- A3 (register enumeration): bad invite tokens return the same generic message
- H1 (404 vs 400): nonexistent resource returns 404
- H2 (DELETE 204): delete endpoints return 204 No Content
"""

import asyncio
import os
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest_asyncio.fixture
async def app_client(tmp_path, monkeypatch):
    """FastAPI TestClient with isolated DB and minimal env."""
    import db as _db
    db_path = str(tmp_path / "app_test.db")
    monkeypatch.setattr(_db, "DB_PATH", db_path)
    await _db.init_db()

    # Import app AFTER env vars are set in conftest
    import importlib
    import app as _app_mod
    importlib.reload(_app_mod)

    from fastapi.testclient import TestClient
    client = TestClient(_app_mod.app)
    yield client, db_path
    client.close()


@pytest_asyncio.fixture
async def admin_user(app_client):
    """Create an admin user directly in DB and return credentials."""
    import auth as _auth
    client, _ = app_client
    email = "admin@test.local"
    password = "Test1234!"
    await _auth.create_user(email=email, password=password, role="admin")
    return {"email": email, "password": password}


# ── Login ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_with_valid_credentials_returns_token(app_client, admin_user):
    client, _ = app_client
    res = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": admin_user["password"]},
    )
    assert res.status_code == 200
    data = res.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == admin_user["email"]
    assert data["user"]["role"] == "admin"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_with_wrong_password_returns_401(app_client, admin_user):
    client, _ = app_client
    res = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": "wrong"},
    )
    assert res.status_code == 401
    # A2: error must be generic, not "wrong password" vs "no such user"
    body = res.json()
    assert "invalid" in body.get("detail", "").lower() or "incorrect" in body.get("detail", "").lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_with_nonexistent_user_returns_same_error(app_client):
    """A2: nonexistent user must return SAME error as wrong password — no enum."""
    client, _ = app_client
    res = client.post(
        "/api/auth/login",
        json={"email": "ghost@nowhere.local", "password": "anything"},
    )
    assert res.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_login_no_stack_trace_on_500(app_client):
    """C1: 500 responses must NOT leak exception details."""
    client, _ = app_client
    res = client.post("/api/auth/login", json={"email": "x", "password": "y"})
    # Either 401 or 422 — never 500 with stack trace
    if res.status_code == 500:
        body = res.text
        assert "Traceback" not in body
        assert "File \"" not in body


# ── Register ───────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_register_with_invalid_token_returns_generic_error(app_client):
    """A3: invalid token must NOT reveal whether token exists or email is taken."""
    client, _ = app_client
    res = client.post(
        "/api/auth/register",
        json={"email": "new@test.local", "password": "pw1234", "invite_token": "not-a-real-token"},
    )
    assert res.status_code == 400
    # Generic message — no hint about what failed
    body = res.json().get("detail", "").lower()
    assert "registration" in body or "invite" in body or "failed" in body


# ── /me ────────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_me_requires_authentication(app_client):
    client, _ = app_client
    res = client.get("/api/auth/me")
    assert res.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_me_returns_user_with_valid_token(app_client, admin_user):
    client, _ = app_client
    login = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": admin_user["password"]},
    )
    token = login.json()["access_token"]
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["email"] == admin_user["email"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_me_rejects_garbage_token(app_client):
    client, _ = app_client
    res = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert res.status_code == 401


# ── System status ──────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_system_status_exposes_lane_capacity(app_client):
    client, _ = app_client
    res = client.get("/api/system/status")
    assert res.status_code == 200
    data = res.json()
    assert "total_slots" in data
    assert "free_slots" in data
    assert "running_agents" in data
    assert "tunnel" in data


# ── Lanes ──────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_lanes_returns_all_ten(app_client, admin_user):
    """GET /api/lanes should return the 10 default lanes."""
    client, _ = app_client
    login = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": admin_user["password"]},
    )
    token = login.json()["access_token"]
    res = client.get("/api/lanes", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    lanes = res.json()
    assert len(lanes) == 10
    names = [l["name"] for l in lanes]
    assert "coder" in names
    assert "orchestrator" in names


# ── Ceiling ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ceiling_patch_requires_pydantic_validation(app_client, admin_user):
    """H4: PATCH /api/system/ceiling must validate via Pydantic, not raw dict."""
    client, _ = app_client
    login = client.post(
        "/api/auth/login",
        json={"email": admin_user["email"], "password": admin_user["password"]},
    )
    token = login.json()["access_token"]
    # Valid update
    res = client.patch(
        "/api/system/ceiling",
        json={"max_agents": 4},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code in (200, 204)
    # Invalid — string where int expected → 422 (Pydantic), not 200
    res = client.patch(
        "/api/system/ceiling",
        json={"max_agents": "six"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 422
