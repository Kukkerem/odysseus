# tests/test_calendar_oauth_route.py
"""Google CalDAV OAuth authorize/callback routes (routes/calendar_routes.py)."""
from unittest import mock

import pytest


def _route(path, method="GET"):
    from routes.calendar_routes import setup_calendar_routes
    router = setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route {method} {path} not found")


class _FakeRequest:
    headers = {"host": "localhost:7000"}


def _loc(resp):
    return resp.headers["location"]


@pytest.mark.asyncio
async def test_authorize_redirects_to_google_consent_with_calendar_scope(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setattr("routes.calendar_routes._require_user",
                        lambda req: "alice", raising=False)
    authorize = _route("/api/calendar/oauth/google/authorize")
    resp = await authorize(request=_FakeRequest())
    loc = _loc(resp)
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcalendar" in loc
    assert "access_type=offline" in loc
    assert "calendar+email" in loc


@pytest.mark.asyncio
async def test_callback_valid_state_creates_oauth_account_with_encrypted_tokens(monkeypatch):
    from routes.email_helpers import make_oauth_state
    from src.secret_storage import decrypt as _dec

    store = {}
    monkeypatch.setattr("routes.prefs_routes._load_for_user",
                        lambda o=None: dict(store.get(o, {})))
    monkeypatch.setattr("routes.prefs_routes._save_for_user",
                        lambda o, p: store.__setitem__(o, p))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")

    monkeypatch.setattr("src.google_oauth.exchange_authorization_code",
                        lambda *a, **k: {"access_token": "ya29.A", "refresh_token": "1//R", "expires_in": 3600})
    ui = mock.MagicMock(); ui.is_success = True
    ui.json.return_value = {"email": "alice@workspace.com"}
    monkeypatch.setattr("httpx.get", lambda *a, **k: ui)

    state = make_oauth_state("new-acc-1", "alice")
    callback = _route("/api/calendar/oauth/google/callback")
    resp = await callback(code="4/code", state=state, error=None, request=_FakeRequest())
    assert "calendar_oauth_success=1" in _loc(resp)

    accounts = store["alice"]["caldav_accounts"]
    acc = next(a for a in accounts if a["id"] == "new-acc-1")
    assert acc["auth_mode"] == "oauth"
    assert acc["url"] == "https://apidata.googleusercontent.com/caldav/v2/alice@workspace.com/user"
    assert acc["username"] == "alice@workspace.com"
    assert _dec(acc["oauth_access_token"]) == "ya29.A"
    assert _dec(acc["oauth_refresh_token"]) == "1//R"
    assert "ya29" not in acc["oauth_token_expiry"]


@pytest.mark.asyncio
async def test_callback_tampered_state_writes_nothing(monkeypatch):
    saved = {"called": False}
    monkeypatch.setattr("routes.prefs_routes._save_for_user",
                        lambda o, p: saved.__setitem__("called", True))
    callback = _route("/api/calendar/oauth/google/callback")
    resp = await callback(code="4/secret", state="not-valid", error=None, request=_FakeRequest())
    assert "calendar_oauth_error=invalid_state" in _loc(resp)
    assert "4/secret" not in _loc(resp)
    assert saved["called"] is False


@pytest.mark.asyncio
async def test_callback_missing_code_returns_generic_error():
    from routes.email_helpers import make_oauth_state
    callback = _route("/api/calendar/oauth/google/callback")
    resp = await callback(code=None, state=make_oauth_state("a", "alice"),
                          error=None, request=_FakeRequest())
    assert "calendar_oauth_error=missing_code" in _loc(resp)

@pytest.mark.asyncio
async def test_account_list_exposes_auth_mode_never_tokens(monkeypatch):
    """The accounts list must expose auth_mode (so the UI shows OAuth status)
    but never the token fields/values."""
    from src.secret_storage import encrypt as _enc
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    acc = {
        "id": "acc-o", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
        "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
        "username": "me@x.com", "password": "",
        "oauth_access_token": _enc("ya29.secret"),
        "oauth_refresh_token": _enc("1//secret"),
        "oauth_token_expiry": "1750000000",
    }
    monkeypatch.setattr("routes.prefs_routes._load_for_user",
                        lambda o=None: {"caldav_accounts": [acc]})
    list_accounts = _route("/api/calendar/config/accounts")
    resp = await list_accounts(request=_FakeRequest())
    a = resp["accounts"][0]
    assert a["auth_mode"] == "oauth"
    assert set(a.keys()) == {"id", "label", "url", "username", "has_password", "auth_mode"}
