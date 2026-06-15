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

# --- Task 6: writeback threading ---
from src import caldav_writeback


def test_writeback_oauth_resolves_token_and_passes_it(monkeypatch):
    acc = {"id": "acc-o", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
           "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
           "username": "me@x.com"}
    monkeypatch.setattr(caldav_writeback, "_load_caldav_accounts", lambda o: [acc], raising=False)
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda o: [acc])
    monkeypatch.setattr(caldav_sync, "_resolve_google_caldav_token", lambda o, a: "ya29.live")
    seen = {}

    def _fake_blocking(local_cal_id, ev, delete, url, username, password,
                       owner="", account_id="", access_token=None):
        seen.update(access_token=access_token, password=password)
        return {"ok": True}

    monkeypatch.setattr(caldav_writeback, "_writeback_blocking", _fake_blocking)
    monkeypatch.setattr(caldav_writeback, "_persist_writeback_result", lambda *a, **k: None)
    out = asyncio.run(caldav_writeback.writeback_event(
        "alice", "caldav", "cal-1", {"uid": "u1", "summary": "x"}))
    assert out["ok"] is True
    assert seen["access_token"] == "ya29.live"
    assert seen["password"] == ""

# --- Task 7: diagnostics ---
import types


def _install_fake_caldav_raising(monkeypatch, exc_factory):
    """Install a fake `caldav` module whose calendar.date_search raises."""
    import sys

    class _FakeError(Exception):
        pass

    fake = types.ModuleType("caldav")
    err_mod = types.ModuleType("caldav.lib.error")
    lib_mod = types.ModuleType("caldav.lib")

    class AuthorizationError(_FakeError):
        pass

    class NotFoundError(_FakeError):
        pass

    err_mod.AuthorizationError = AuthorizationError
    err_mod.NotFoundError = NotFoundError

    class _Cal:
        def __init__(self, url):
            self.url = url
            self.name = "Primary"

        def date_search(self, start, end, expand=False):
            raise exc_factory(err_mod)

    class _Principal:
        def calendars(self):
            return [_Cal("https://www.google.com/calendar/dav/me@x.com/events")]

    class _Session:
        def __init__(self):
            self.headers = {}
            self.max_redirects = None

    class _Client:
        def __init__(self, url=None, username=None, password=None):
            self.url = url
            self.session = _Session()
            self.headers = {}
            self.auth = None

        def principal(self):
            return _Principal()

        def calendar(self, url=None):
            return _Cal(url)

    fake.DAVClient = _Client
    fake.lib = lib_mod
    lib_mod.error = err_mod
    monkeypatch.setitem(sys.modules, "caldav", fake)
    monkeypatch.setitem(sys.modules, "caldav.lib", lib_mod)
    monkeypatch.setitem(sys.modules, "caldav.lib.error", err_mod)
    return err_mod


def test_google_basic_auth_primary_404_emits_oauth_hint(monkeypatch):
    _install_fake_caldav_raising(monkeypatch, lambda err: err.NotFoundError("404"))
    out = caldav_sync._sync_blocking(
        "alice", "https://www.google.com/calendar/dav/me@x.com/user", "me@x.com", "app-pw")
    assert any("OAuth" in e or "Connect Google Calendar" in e for e in out["errors"]), out["errors"]


def test_non_google_404_keeps_generic_message(monkeypatch):
    _install_fake_caldav_raising(monkeypatch, lambda err: err.NotFoundError("404"))
    out = caldav_sync._sync_blocking(
        "alice", "https://dav.fastmail.com/dav/calendars/user/me/", "me", "pw")
    assert any("date_search failed" in e for e in out["errors"])
    assert not any("Connect Google Calendar" in e for e in out["errors"])
