from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from emission_tracker.web.auth import current_user, is_admin, require_admin


def _make_app(admin_users: list[str]) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(admin_users=admin_users)

    @app.get("/who")
    def who(request: Request):
        return {"user": current_user(request), "is_admin": is_admin(request)}

    @app.post("/settle")
    def settle(user: str = Depends(require_admin)):
        return {"settled_by": user}

    return app


def test_current_user_returns_header_value():
    client = TestClient(_make_app(admin_users=["alice"]))
    resp = client.get("/who", headers={"X-Remote-User": "alice"})
    assert resp.json() == {"user": "alice", "is_admin": True}


def test_current_user_none_when_header_missing():
    client = TestClient(_make_app(admin_users=["alice"]))
    resp = client.get("/who")
    assert resp.json() == {"user": None, "is_admin": False}


def test_is_admin_false_when_user_not_in_list():
    client = TestClient(_make_app(admin_users=["alice"]))
    resp = client.get("/who", headers={"X-Remote-User": "bob"})
    assert resp.json() == {"user": "bob", "is_admin": False}


def test_require_admin_allows_admin_user():
    client = TestClient(_make_app(admin_users=["alice"]))
    resp = client.post("/settle", headers={"X-Remote-User": "alice"})
    assert resp.status_code == 200
    assert resp.json() == {"settled_by": "alice"}


def test_require_admin_rejects_non_admin_403():
    client = TestClient(_make_app(admin_users=["alice"]))
    resp = client.post("/settle", headers={"X-Remote-User": "bob"})
    assert resp.status_code == 403
    assert "not an admin" in resp.json()["detail"]


def test_require_admin_rejects_unauthenticated_401():
    client = TestClient(_make_app(admin_users=["alice"]))
    resp = client.post("/settle")  # no header
    assert resp.status_code == 401
    assert "Not authenticated" in resp.json()["detail"]


def test_admin_users_empty_means_nobody_is_admin():
    client = TestClient(_make_app(admin_users=[]))
    resp = client.post("/settle", headers={"X-Remote-User": "alice"})
    assert resp.status_code == 403
