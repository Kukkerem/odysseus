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
    assert acc["read_only"] is True  # Google OAuth accounts default to read-only


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
    assert set(a.keys()) == {"id", "label", "url", "username", "has_password", "auth_mode",
                             "oauth_provider", "oauth_client_id", "has_oauth_client", "connected", "read_only"}
    assert a["read_only"] is False
    assert a["connected"] is True          # has an encrypted refresh token
    assert a["has_oauth_client"] is False  # but no per-account client configured
    assert "oauth_client_secret" not in a
    assert "oauth_access_token" not in a and "oauth_refresh_token" not in a

class _BodyRequest:
    headers = {"host": "localhost:7000"}

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _oauth_acc():
    from src.secret_storage import encrypt as _enc
    return {
        "id": "acc-o", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
        "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
        "username": "me@x.com", "password": "",
        "oauth_access_token": _enc("ya29.x"), "oauth_refresh_token": _enc("1//x"),
        "oauth_token_expiry": "9999999999",
    }


@pytest.mark.asyncio
async def test_test_connection_oauth_probes_events_url_with_bearer(monkeypatch):
    """Test Connection on an OAuth account (no password) must resolve a bearer
    token and PROPFIND the /events collection — not fail the old
    url+user+pw guard, and never send basic-auth creds."""
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user",
                        lambda o=None: {"caldav_accounts": [_oauth_acc()]})
    monkeypatch.setattr("src.caldav_sync.validate_caldav_url", lambda u: u)
    monkeypatch.setattr("src.caldav_sync._resolve_google_caldav_token",
                        lambda owner, acc: "ya29.live")

    captured = {}

    class _Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _Resp(207)

    monkeypatch.setattr("httpx.AsyncClient", _Client)

    test_conn = _route("/api/calendar/test", "POST")
    resp = await test_conn(request=_BodyRequest({"account_id": "acc-o"}))

    assert resp == {"ok": True}
    assert captured["method"] == "PROPFIND"
    assert captured["url"] == "https://apidata.googleusercontent.com/caldav/v2/me@x.com/events"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer ya29.live"
    assert "auth" not in captured["kwargs"]


@pytest.mark.asyncio
async def test_test_connection_oauth_expired_token_says_reconnect(monkeypatch):
    """When the token can't be resolved/refreshed, /test reports a reconnect
    hint and never touches the network."""
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user",
                        lambda o=None: {"caldav_accounts": [_oauth_acc()]})
    monkeypatch.setattr("src.caldav_sync.validate_caldav_url", lambda u: u)
    monkeypatch.setattr("src.caldav_sync._resolve_google_caldav_token",
                        lambda owner, acc: None)

    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("no network when the token is unresolvable")

    monkeypatch.setattr("httpx.AsyncClient", _Boom)

    test_conn = _route("/api/calendar/test", "POST")
    resp = await test_conn(request=_BodyRequest({"account_id": "acc-o"}))

    assert resp["ok"] is False
    assert "reconnect Google Calendar" in resp["error"]


@pytest.mark.asyncio
async def test_add_and_update_round_trip_read_only(monkeypatch):
    """read_only is persisted by add, surfaced by list, and toggled by PUT."""
    store = {}
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: dict(store.get(o, {})))
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: store.__setitem__(o, p))
    monkeypatch.setattr("src.caldav_sync.validate_caldav_url", lambda u: u)

    add = _route("/api/calendar/config/accounts", "POST")
    created = await add(request=_BodyRequest({
        "label": "Work", "url": "https://dav.example.com/u/",
        "username": "me", "password": "pw", "read_only": True}))
    acc_id = created["id"]

    lst = _route("/api/calendar/config/accounts", "GET")
    accounts = (await lst(request=_BodyRequest({})))["accounts"]
    assert accounts[0]["read_only"] is True

    upd = _route("/api/calendar/config/accounts/{account_id}", "PUT")
    await upd(account_id=acc_id, request=_BodyRequest({"read_only": False}))
    accounts = (await lst(request=_BodyRequest({})))["accounts"]
    assert accounts[0]["read_only"] is False

# --- Bring-your-own per-account OAuth client ---

@pytest.mark.asyncio
async def test_add_oauth_draft_encrypts_secret_and_defers_url(monkeypatch):
    from src.secret_storage import decrypt as _dec
    store = {}
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: dict(store.get(o, {})))
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: store.__setitem__(o, p))
    add = _route("/api/calendar/config/accounts", "POST")
    created = await add(request=_BodyRequest({
        "auth_mode": "oauth", "label": "Personal",
        "oauth_client_id": "p-id", "oauth_client_secret": "p-sec"}))
    acc = store["alice"]["caldav_accounts"][0]
    assert acc["id"] == created["id"]
    assert acc["auth_mode"] == "oauth" and acc["oauth_provider"] == "google"
    assert acc["oauth_client_id"] == "p-id"
    assert _dec(acc["oauth_client_secret"]) == "p-sec"   # encrypted at rest
    assert acc["url"] == "" and acc["password"] == ""    # filled by the callback
    assert acc["read_only"] is True                      # OAuth drafts default pull-only


@pytest.mark.asyncio
async def test_add_oauth_draft_parses_client_json(monkeypatch):
    from src.secret_storage import decrypt as _dec
    store = {}
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: dict(store.get(o, {})))
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: store.__setitem__(o, p))
    add = _route("/api/calendar/config/accounts", "POST")
    await add(request=_BodyRequest({
        "auth_mode": "oauth",
        "oauth_client_json": '{"web":{"client_id":"j-id","client_secret":"j-sec"}}'}))
    acc = store["alice"]["caldav_accounts"][0]
    assert acc["oauth_client_id"] == "j-id"
    assert _dec(acc["oauth_client_secret"]) == "j-sec"


@pytest.mark.asyncio
async def test_add_oauth_draft_without_client_or_env_rejected(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: {})
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: None)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    add = _route("/api/calendar/config/accounts", "POST")
    with pytest.raises(HTTPException) as ei:
        await add(request=_BodyRequest({"auth_mode": "oauth"}))
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_list_exposes_client_flags_never_secret(monkeypatch):
    from src.secret_storage import encrypt as _enc
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    acc = {"id": "byo", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
           "oauth_client_id": "c-id", "oauth_client_secret": _enc("c-sec"),
           "oauth_refresh_token": _enc("1//r")}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: {"caldav_accounts": [acc]})
    lst = _route("/api/calendar/config/accounts")
    a = (await lst(request=_FakeRequest()))["accounts"][0]
    assert a["has_oauth_client"] is True
    assert a["oauth_client_id"] == "c-id"
    assert a["connected"] is True
    assert "oauth_client_secret" not in a


@pytest.mark.asyncio
async def test_authorize_with_account_id_uses_account_client(monkeypatch):
    from src.secret_storage import encrypt as _enc
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-sec")
    acc = {"id": "byo-1", "auth_mode": "oauth", "oauth_provider": "google",
           "oauth_client_id": "acc-cid", "oauth_client_secret": _enc("acc-sec")}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: {"caldav_accounts": [acc]})
    authorize = _route("/api/calendar/oauth/google/authorize")
    loc = _loc(await authorize(request=_FakeRequest(), account_id="byo-1"))
    assert "client_id=acc-cid" in loc
    assert "env-cid" not in loc


@pytest.mark.asyncio
async def test_authorize_unknown_account_id_404(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: {"caldav_accounts": []})
    authorize = _route("/api/calendar/oauth/google/authorize")
    with pytest.raises(HTTPException) as ei:
        await authorize(request=_FakeRequest(), account_id="nope")
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_callback_byo_account_uses_its_client_and_updates_in_place(monkeypatch):
    from routes.email_helpers import make_oauth_state
    from src.secret_storage import encrypt as _enc, decrypt as _dec
    draft = {"id": "byo-1", "label": "My Work", "auth_mode": "oauth", "oauth_provider": "google",
             "oauth_client_id": "acc-cid", "oauth_client_secret": _enc("acc-sec"),
             "url": "", "username": "", "password": "", "read_only": False}
    store = {"alice": {"caldav_accounts": [draft]}}
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: dict(store.get(o, {})))
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: store.__setitem__(o, p))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-cid")     # differs — must not be used
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-sec")
    seen = {}

    def _exchange(cid, csec, code, redirect, *a, **k):
        seen["cid"], seen["csec"] = cid, csec
        return {"access_token": "ya29.A", "refresh_token": "1//R", "expires_in": 3600}
    monkeypatch.setattr("src.google_oauth.exchange_authorization_code", _exchange)
    ui = mock.MagicMock(); ui.is_success = True
    ui.json.return_value = {"email": "work@corp.com"}
    monkeypatch.setattr("httpx.get", lambda *a, **k: ui)

    callback = _route("/api/calendar/oauth/google/callback")
    resp = await callback(code="4/c", state=make_oauth_state("byo-1", "alice"),
                          error=None, request=_FakeRequest())
    assert "calendar_oauth_success=1" in _loc(resp)
    accounts = store["alice"]["caldav_accounts"]
    assert len(accounts) == 1                            # updated in place, not appended
    acc = accounts[0]
    assert seen == {"cid": "acc-cid", "csec": "acc-sec"} # used the account's own client
    assert acc["id"] == "byo-1"
    assert acc["label"] == "My Work"                     # preserved
    assert acc["read_only"] is False                     # preserved
    assert acc["oauth_client_id"] == "acc-cid"           # preserved
    assert acc["url"] == "https://apidata.googleusercontent.com/caldav/v2/work@corp.com/user"
    assert _dec(acc["oauth_access_token"]) == "ya29.A"
    assert _dec(acc["oauth_refresh_token"]) == "1//R"


@pytest.mark.asyncio
async def test_update_changes_client_creds(monkeypatch):
    from src.secret_storage import encrypt as _enc, decrypt as _dec
    draft = {"id": "byo-1", "auth_mode": "oauth", "oauth_provider": "google",
             "oauth_client_id": "old-id", "oauth_client_secret": _enc("old-sec")}
    store = {"alice": {"caldav_accounts": [draft]}}
    monkeypatch.setattr("routes.calendar_routes._require_user", lambda req: "alice", raising=False)
    monkeypatch.setattr("routes.prefs_routes._load_for_user", lambda o=None: dict(store.get(o, {})))
    monkeypatch.setattr("routes.prefs_routes._save_for_user", lambda o, p: store.__setitem__(o, p))
    upd = _route("/api/calendar/config/accounts/{account_id}", "PUT")
    await upd(account_id="byo-1", request=_BodyRequest({
        "oauth_client_id": "new-id", "oauth_client_secret": "new-sec"}))
    acc = store["alice"]["caldav_accounts"][0]
    assert acc["oauth_client_id"] == "new-id"
    assert _dec(acc["oauth_client_secret"]) == "new-sec"
