# tests/test_caldav_oauth.py
"""Google CalDAV OAuth: bearer client, token resolution, sync/write-back
threading, and basic-auth diagnostics."""
import pytest

from src import caldav_sync


def test_build_dav_client_bearer_sets_header_and_no_basic_auth():
    """An access_token builds a bearer client: Authorization header on the
    caldav client header set (merged into every request), no basic auth object,
    redirects still disabled."""
    pytest.importorskip("caldav")
    client = caldav_sync._build_dav_client(
        "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
        "me@x.com", "", access_token="ya29.tok",
    )
    assert client.headers.get("Authorization") == "Bearer ya29.tok"
    assert client.auth is None, "bearer client must not carry a basic-auth object"
    assert client.session.max_redirects == 0


def test_build_dav_client_basic_unchanged():
    """No access_token → basic auth path is unchanged (regression guard)."""
    pytest.importorskip("caldav")
    client = caldav_sync._build_dav_client("https://dav.example.com/", "u", "p")
    assert client.session.max_redirects == 0
    assert client.headers.get("Authorization") is None


# --- Task 4: token resolution ---
from unittest import mock

from src.secret_storage import encrypt as _enc


def _oauth_account(expiry):
    return {
        "id": "acc-oauth", "label": "Work", "auth_mode": "oauth",
        "oauth_provider": "google",
        "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
        "username": "me@x.com",
        "oauth_access_token": _enc("ya29.cached"),
        "oauth_refresh_token": _enc("1//refresh"),
        "oauth_token_expiry": str(expiry),
    }


def test_resolve_token_returns_cached_when_not_expired(monkeypatch):
    import time
    acc = _oauth_account(int(time.time()) + 3600)
    monkeypatch.setattr("src.google_oauth.exchange_refresh_token",
                        mock.MagicMock(side_effect=AssertionError("must not refresh")))
    assert caldav_sync._resolve_google_caldav_token("alice", acc) == "ya29.cached"


def test_resolve_token_refreshes_when_expired_and_persists(monkeypatch):
    import time
    acc = _oauth_account(int(time.time()) - 5)  # expired
    store = {"caldav_accounts": [dict(acc)]}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: dict(store))
    saved = {}
    monkeypatch.setattr("routes.prefs_routes._save_for_user",
                        lambda o, p: saved.update({"owner": o, "prefs": p}))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setattr("src.google_oauth.exchange_refresh_token",
                        lambda *a, **k: {"access_token": "ya29.fresh", "expires_in": 3600})

    tok = caldav_sync._resolve_google_caldav_token("alice", acc)
    assert tok == "ya29.fresh"
    persisted = saved["prefs"]["caldav_accounts"][0]
    from src.secret_storage import decrypt as _dec
    assert _dec(persisted["oauth_access_token"]) == "ya29.fresh"
    assert "ya29" not in persisted["oauth_token_expiry"]
    assert int(persisted["oauth_token_expiry"]) > int(time.time())


def test_resolve_token_returns_none_when_refresh_fails(monkeypatch):
    import time
    acc = _oauth_account(int(time.time()) - 5)
    monkeypatch.setattr("routes.prefs_routes._load_for_user",
                        lambda o=None: {"caldav_accounts": [dict(acc)]})
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: None)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")

    def _boom(*a, **k):
        raise RuntimeError("token endpoint 400")
    monkeypatch.setattr("src.google_oauth.exchange_refresh_token", _boom)

    assert caldav_sync._resolve_google_caldav_token("alice", acc) is None


# --- Task 5: sync threading ---
import asyncio


def test_sync_caldav_oauth_resolves_token_and_passes_it(monkeypatch):
    """An OAuth account resolves a token and hands it to _sync_blocking; no
    password is required."""
    acc = {
        "id": "acc-o", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
        "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
        "username": "me@x.com",
    }
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda o: [acc])
    monkeypatch.setattr(caldav_sync, "_resolve_google_caldav_token", lambda o, a: "ya29.live")
    seen = {}

    def _fake_blocking(owner, url, username, password, account_id="", access_token=None):
        seen.update(url=url, username=username, password=password,
                    account_id=account_id, access_token=access_token)
        return {"calendars": 1, "events": 3, "deleted": 0, "errors": []}

    monkeypatch.setattr(caldav_sync, "_sync_blocking", _fake_blocking)
    out = asyncio.run(caldav_sync.sync_caldav("alice"))
    assert out["events"] == 3
    assert seen["access_token"] == "ya29.live"
    assert seen["password"] == ""  # OAuth carries no password


def test_sync_caldav_oauth_unresolvable_token_surfaces_reconnect(monkeypatch):
    acc = {"id": "acc-o", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
           "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
           "username": "me@x.com"}
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda o: [acc])
    monkeypatch.setattr(caldav_sync, "_resolve_google_caldav_token", lambda o, a: None)
    monkeypatch.setattr(caldav_sync, "_sync_blocking",
                        mock.MagicMock(side_effect=AssertionError("must not sync")))
    out = asyncio.run(caldav_sync.sync_caldav("alice"))
    assert any("reconnect" in e.lower() for e in out["errors"])
