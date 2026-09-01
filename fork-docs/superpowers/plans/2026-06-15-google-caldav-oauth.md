# Google Workspace CalDAV OAuth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Google Workspace accounts sync their primary CalDAV calendar by authenticating with an OAuth 2.0 bearer token against the v2 endpoint, and surface a clear diagnostic for accounts still stuck on basic auth.

**Architecture:** A new per-account `auth_mode` (`"basic"` default | `"oauth"`) on the prefs-stored `caldav_accounts` entries. OAuth accounts resolve/refresh a Google access token and build a **bearer**-authenticated `caldav.DAVClient` through the *existing* sync and write-back path; everything downstream (discovery, REPORT, event mapping, prune, write-back) is unchanged. Basic auth stays the default for all non-Google servers.

**Tech Stack:** Python 3 / FastAPI, `caldav` (2.x; bearer via client header), `httpx` for Google token endpoints, `src.secret_storage` (Fernet) for at-rest encryption, prefs JSON store (`routes/prefs_routes.py`), vanilla-JS settings UI (`static/js/settings.js`), `pytest`.

**Spec:** `docs/superpowers/specs/2026-06-15-google-caldav-oauth-design.md`

---

## File Structure

**New files**
- `src/google_oauth.py` — provider-agnostic Google OAuth2 token HTTP (code + refresh exchange, env client credentials). Imported by both the email and CalDAV paths. stdlib + `httpx` only — no FastAPI/DB imports, so `src/caldav_sync.py` can import it without a circular dependency.
- `tests/test_google_oauth.py` — unit tests for the exchange helpers.
- `tests/test_caldav_oauth.py` — unit tests for bearer client construction, token resolution/refresh, sync/write-back threading, and diagnostics.
- `tests/test_calendar_oauth_route.py` — unit tests for the authorize/callback routes.

**Modified files**
- `src/caldav_sync.py` — `access_token` param on `_build_dav_client`; `auth_mode` branch + token resolution in `sync_caldav`; thread token into `_sync_blocking`; Google basic-auth diagnostics.
- `src/caldav_writeback.py` — thread token resolution into `writeback_event` / `_writeback_blocking`.
- `routes/email_helpers.py` — `_refresh_google_token` delegates to the shared `src.google_oauth.exchange_refresh_token` (behaviour-preserving).
- `routes/calendar_routes.py` — `/api/calendar/oauth/google/authorize` + `/callback` routes; provision a new OAuth `caldav_accounts` entry.
- `static/js/settings.js` — "Connect Google Calendar" button in the CalDAV form; handle `calendar_oauth_success`/`calendar_oauth_error` return params.
- `.env.example`, `docs/setup.md` — deployment notes (Calendar API, scope, optional redirect-uri env).

**Note on running tests:** tests that construct a real `caldav.DAVClient` use `pytest.importorskip("caldav")` (matching `tests/test_caldav_redirect_hardening.py`); they run in CI where `caldav` is installed and skip where it is not. All other tests use fakes/mocks and run everywhere.

---

### Task 1: Shared Google OAuth2 token-exchange helper

**Files:**
- Create: `src/google_oauth.py`
- Test: `tests/test_google_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_google_oauth.py
"""Shared Google OAuth2 token-exchange helpers (src/google_oauth.py).

One HTTP round-trip owned in one place; email and CalDAV each keep their own
token persistence. These tests mock httpx so no network call is made.
"""
from unittest import mock

from src import google_oauth


def test_exchange_refresh_token_posts_refresh_grant_and_returns_json():
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"access_token": "ya29.new", "expires_in": 3599}
    with mock.patch("httpx.post", return_value=resp) as post:
        out = google_oauth.exchange_refresh_token("cid", "secret", "1//refresh")
    assert out["access_token"] == "ya29.new"
    args, kwargs = post.call_args
    assert args[0] == google_oauth.GOOGLE_TOKEN_URL
    assert kwargs["data"]["grant_type"] == "refresh_token"
    assert kwargs["data"]["refresh_token"] == "1//refresh"


def test_exchange_authorization_code_posts_code_grant():
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"access_token": "ya29.x", "refresh_token": "r", "expires_in": 3600}
    with mock.patch("httpx.post", return_value=resp) as post:
        out = google_oauth.exchange_authorization_code("cid", "secret", "4/code", "https://app/cb")
    assert out["refresh_token"] == "r"
    _, kwargs = post.call_args
    assert kwargs["data"]["grant_type"] == "authorization_code"
    assert kwargs["data"]["code"] == "4/code"
    assert kwargs["data"]["redirect_uri"] == "https://app/cb"


def test_google_client_credentials_reads_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "the-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "the-secret")
    assert google_oauth.google_client_credentials() == ("the-id", "the-secret")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_google_oauth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.google_oauth'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/google_oauth.py
"""Shared Google OAuth2 token-exchange helpers.

The email integration (tokens on the EmailAccount row) and the CalDAV
integration (tokens in user prefs) both refresh Google access tokens against
the same endpoint. This module owns the single HTTP round-trip; each caller
keeps its own persistence. Deliberately stdlib + httpx only (no FastAPI / DB
imports) so src/caldav_sync.py can import it without a circular dependency.
"""
from __future__ import annotations

import os

import httpx

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def google_client_credentials() -> tuple[str, str]:
    """The Google OAuth client id/secret from env (shared with the Gmail flow)."""
    return (
        os.environ.get("GOOGLE_OAUTH_CLIENT_ID", ""),
        os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
    )


def exchange_refresh_token(client_id: str, client_secret: str, refresh_token: str,
                           timeout: float = 10) -> dict:
    """Exchange a refresh token for a fresh access token.

    Returns the parsed token JSON ({"access_token", "expires_in", ...}).
    Raises httpx.HTTPError on a non-2xx response or transport failure.
    """
    resp = httpx.post(GOOGLE_TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def exchange_authorization_code(client_id: str, client_secret: str, code: str,
                                redirect_uri: str, timeout: float = 10) -> dict:
    """Exchange a consent-flow authorization code for tokens.

    Returns the parsed token JSON ({"access_token", "refresh_token",
    "expires_in", ...}). Raises httpx.HTTPError on failure.
    """
    resp = httpx.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_google_oauth.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/google_oauth.py tests/test_google_oauth.py
git commit -m "feat(google-oauth): shared token-exchange helper for email + caldav"
```

---

### Task 2: Route the email refresh through the shared helper

This is a behaviour-preserving refactor that proves the shared helper works for the existing Gmail path. The email OAuth tests (`tests/test_email_oauth.py`) mock the global `httpx.post`, so they keep passing because `src.google_oauth` calls `httpx.post`.

**Files:**
- Modify: `routes/email_helpers.py:92-126` (`_refresh_google_token`)

- [ ] **Step 1: Replace the inline token POST with the shared helper**

Original `routes/email_helpers.py:92-126` (the body that builds the httpx POST inline) becomes:

```python
def _refresh_google_token(account_id: str) -> str | None:
    """Exchange the stored refresh token for a new access token and persist it."""
    from core.database import SessionLocal as _SL, EmailAccount as _EA
    from src.secret_storage import encrypt as _enc, decrypt as _dec
    from src.google_oauth import exchange_refresh_token, google_client_credentials
    client_id, client_secret = google_client_credentials()
    if not client_id or not client_secret:
        return None
    db = _SL()
    try:
        row = db.get(_EA, account_id)
        if not row or not row.oauth_refresh_token:
            return None
        refresh_token = _dec(row.oauth_refresh_token or "")
        if not refresh_token:
            return None
        data = exchange_refresh_token(client_id, client_secret, refresh_token)
        access_token = data["access_token"]
        row.oauth_access_token = _enc(access_token)
        row.oauth_token_expiry = str(int(time.time()) + data.get("expires_in", 3600))
        db.commit()
        return access_token
    except Exception:
        logger.warning(f"Google token refresh failed for account {account_id}")
        return None
    finally:
        db.close()
```

- [ ] **Step 2: Run the existing email OAuth tests to verify no regression**

Run: `python -m pytest tests/test_email_oauth.py -v`
Expected: PASS (all existing tests still pass — the refresh now goes through `src.google_oauth`)

- [ ] **Step 3: Commit**

```bash
git add routes/email_helpers.py
git commit -m "refactor(email): refresh Google token via shared google_oauth helper"
```

---

### Task 3: Bearer-token support in `_build_dav_client`

**Files:**
- Modify: `src/caldav_sync.py:247-270` (`_build_dav_client`)
- Test: `tests/test_caldav_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_caldav_oauth.py::test_build_dav_client_bearer_sets_header_and_no_basic_auth -v`
Expected: FAIL — `TypeError: _build_dav_client() got an unexpected keyword argument 'access_token'` (or SKIP if `caldav` is not installed locally; run in CI to confirm)

- [ ] **Step 3: Write minimal implementation**

Replace `_build_dav_client` (`src/caldav_sync.py:247-270`):

```python
def _build_dav_client(url: str, username: str, password: str, access_token: str | None = None):
    """Construct a CalDAV client with automatic redirects disabled.

    Basic auth (username/password) for most servers. When ``access_token`` is
    given (Google OAuth), authenticate with a Bearer token instead: set the
    Authorization header on the caldav client's own header set, which
    ``DAVClient._prepare_request`` merges into every request. This is version-
    robust — unlike ``auth_type="bearer"`` / ``caldav.requests.HTTPBearerAuth``,
    which exist only in caldav 2.x.

    Redirects are pinned to zero (set on the session, created in ``__init__``)
    so a validated public host cannot be redirected, at request time, into
    loopback/private space — the SSRF the host check closes.
    """
    import caldav

    if access_token:
        client = caldav.DAVClient(url=url)
        client.headers["Authorization"] = f"Bearer {access_token}"
    else:
        client = caldav.DAVClient(url=url, username=username, password=password)
    client.session.max_redirects = 0
    return client
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_caldav_oauth.py -k build_dav_client -v`
Expected: PASS (or SKIP locally without `caldav`; PASS in CI)

- [ ] **Step 5: Run the redirect-hardening regression tests**

Run: `python -m pytest tests/test_caldav_redirect_hardening.py -v`
Expected: PASS (basic-auth construction + the `_build_dav_client(` grep guard still hold)

- [ ] **Step 6: Commit**

```bash
git add src/caldav_sync.py tests/test_caldav_oauth.py
git commit -m "feat(caldav): bearer-token auth in _build_dav_client"
```

---

### Task 4: Prefs-backed Google token resolution for CalDAV

`caldav_accounts` live in the prefs JSON (`routes/prefs_routes._load_for_user`/`_save_for_user`), not the DB — so this mirrors `_refresh_google_token` but reads/writes the prefs account entry.

**Files:**
- Modify: `src/caldav_sync.py` (add helpers after `_load_caldav_accounts`, near line 631)
- Test: `tests/test_caldav_oauth.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_caldav_oauth.py
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
    # No httpx call expected when the cached token is still valid.
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
    # Persisted: encrypted token (not plaintext) + a numeric future expiry.
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_caldav_oauth.py -k resolve_token -v`
Expected: FAIL — `AttributeError: module 'src.caldav_sync' has no attribute '_resolve_google_caldav_token'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/caldav_sync.py` (after `_load_caldav_accounts`, ~line 631). Import `time` at the top if not already imported — it is not currently imported, so add `import time` to the import block near line 25.

```python
# Refresh slightly before expiry so an in-flight sync doesn't race the cutoff.
_TOKEN_EXPIRY_SKEW_SECONDS = 60


def _refresh_google_caldav_token(owner: str, account_id: str) -> str | None:
    """Exchange the account's stored refresh token for a new access token and
    persist the encrypted token + expiry back into the owner's prefs entry."""
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src.secret_storage import decrypt as _dec, encrypt as _enc
    from src.google_oauth import exchange_refresh_token, google_client_credentials

    client_id, client_secret = google_client_credentials()
    if not (client_id and client_secret):
        return None
    prefs = _load_for_user(owner) or {}
    accounts = list(prefs.get("caldav_accounts") or [])
    idx = next((i for i, a in enumerate(accounts) if a.get("id") == account_id), None)
    if idx is None:
        return None
    acc = accounts[idx]
    try:
        refresh_token = _dec(acc.get("oauth_refresh_token") or "")
    except Exception:
        refresh_token = ""
    if not refresh_token:
        return None
    try:
        data = exchange_refresh_token(client_id, client_secret, refresh_token)
        access_token = data["access_token"]
    except Exception:
        logger.warning("Google CalDAV token refresh failed for account %s", account_id)
        return None
    acc["oauth_access_token"] = _enc(access_token)
    acc["oauth_token_expiry"] = str(int(time.time()) + data.get("expires_in", 3600))
    accounts[idx] = acc
    prefs["caldav_accounts"] = accounts
    try:
        _save_for_user(owner, prefs)
    except Exception:
        logger.warning("Persisting refreshed CalDAV token failed for account %s", account_id)
    return access_token


def _resolve_google_caldav_token(owner: str, account: dict) -> str | None:
    """Return a valid Google access token for an OAuth CalDAV account,
    refreshing (and persisting) when the cached token is missing or expiring."""
    from src.secret_storage import decrypt as _dec

    try:
        access_token = _dec(account.get("oauth_access_token") or "")
    except Exception:
        access_token = ""
    expiry_raw = account.get("oauth_token_expiry") or ""
    if access_token and expiry_raw:
        try:
            if int(expiry_raw) - _TOKEN_EXPIRY_SKEW_SECONDS > time.time():
                return access_token
        except (ValueError, TypeError):
            pass
    return _refresh_google_caldav_token(owner, account.get("id") or "")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_caldav_oauth.py -k resolve_token -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add src/caldav_sync.py tests/test_caldav_oauth.py
git commit -m "feat(caldav): resolve/refresh Google OAuth tokens from prefs"
```

---

### Task 5: Thread the token through `sync_caldav` and `_sync_blocking`

**Files:**
- Modify: `src/caldav_sync.py:287` (`_sync_blocking` signature + client build) and `:646-673` (`sync_caldav` per-account loop)
- Test: `tests/test_caldav_oauth.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_caldav_oauth.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_caldav_oauth.py -k sync_caldav_oauth -v`
Expected: FAIL — `_sync_blocking` called with unexpected `access_token` kwarg / reconnect error not present.

- [ ] **Step 3: Update `_sync_blocking` signature and client build**

In `src/caldav_sync.py`, change the signature (line 287) and the client construction (line 297):

```python
def _sync_blocking(owner: str, url: str, username: str, password: str,
                   account_id: str = "", access_token: str | None = None) -> dict:
```

```python
    client = _build_dav_client(url, username, password, access_token)
```

- [ ] **Step 4: Update the `sync_caldav` per-account loop**

Replace the loop body in `src/caldav_sync.py:647-672` (from `for acc in accounts:` to the end of the loop):

```python
    for acc in accounts:
        url = (acc.get("url") or "").strip()
        user = (acc.get("username") or "").strip()
        account_id = acc.get("id") or ""
        label = acc.get("label") or url or account_id
        auth_mode = (acc.get("auth_mode") or "basic").lower()
        access_token = None
        pw = ""
        if auth_mode == "oauth":
            access_token = _resolve_google_caldav_token(owner, acc)
            if not access_token:
                totals["errors"].append(
                    f"{label}: Google authorization expired — reconnect Google Calendar")
                continue
            if not (url and user):
                totals["errors"].append(f"{label}: missing URL or account email")
                continue
        else:
            pw = acc.get("password") or ""
            try:
                pw = decrypt(pw)
            except Exception:
                pass
            if not (url and user and pw):
                totals["errors"].append(f"{label}: missing URL, username, or password")
                continue
        try:
            url = validate_caldav_url(url)
            result = await asyncio.to_thread(
                _sync_blocking, owner, url, user, pw, account_id, access_token)
        except ValueError as e:
            result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)]}
        except Exception as e:
            logger.exception("CalDAV sync raised for account %s", label)
            result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)[:200]]}
        totals["calendars"] += result.get("calendars", 0)
        totals["events"] += result.get("events", 0)
        totals["deleted"] += result.get("deleted", 0)
        for err in result.get("errors", []):
            totals["errors"].append(f"{label}: {err}")
```

- [ ] **Step 5: Run tests to verify they pass + regression**

Run: `python -m pytest tests/test_caldav_oauth.py -k sync_caldav_oauth tests/test_caldav_google_principal_url.py -v`
Expected: PASS (new OAuth tests pass; existing basic-auth Google sync test still passes — `access_token` defaults to `None`)

- [ ] **Step 6: Commit**

```bash
git add src/caldav_sync.py tests/test_caldav_oauth.py
git commit -m "feat(caldav): sync OAuth accounts with a bearer token"
```

---

### Task 6: Thread the token through write-back

**Files:**
- Modify: `src/caldav_writeback.py:179-189` (`_writeback_blocking`) and `:240-289` (`writeback_event`)
- Test: `tests/test_caldav_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_caldav_oauth.py
from src import caldav_writeback


def test_writeback_oauth_resolves_token_and_passes_it(monkeypatch):
    acc = {"id": "acc-o", "label": "Work", "auth_mode": "oauth", "oauth_provider": "google",
           "url": "https://apidata.googleusercontent.com/caldav/v2/me@x.com/user",
           "username": "me@x.com"}
    monkeypatch.setattr(caldav_writeback, "_load_caldav_accounts", lambda o: [acc], raising=False)
    # writeback_event imports _load_caldav_accounts from src.caldav_sync at call time:
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_caldav_oauth.py -k writeback_oauth -v`
Expected: FAIL — `_writeback_blocking` has no `access_token` parameter / `writeback_event` skips the OAuth account (incomplete credentials).

- [ ] **Step 3: Update `_writeback_blocking`**

Replace `src/caldav_writeback.py:179-189`:

```python
def _writeback_blocking(local_cal_id, ev, delete, url, username, password,
                        owner="", account_id="", access_token=None) -> dict:
    from src.caldav_sync import _build_dav_client
    # Redirects disabled here too: the write-back path opens its own DAVClient,
    # so it needs the same SSRF-via-redirect protection as the pull path.
    client = _build_dav_client(url, username, password, access_token)
    calendars = _discover_calendars(client)
    if not calendars:
        return {"ok": False, "error": "no remote calendars discovered"}
    return push_event(calendars, local_cal_id, ev, delete=delete,
                      owner=owner, account_id=account_id)
```

- [ ] **Step 4: Update `writeback_event` credential resolution**

In `src/caldav_writeback.py`, replace the credential block (`:275-289`, from `url = (acc.get("url")...` through the `_writeback_blocking` call) with:

```python
        url = (acc.get("url") or "").strip()
        user = (acc.get("username") or "").strip()
        auth_mode = (acc.get("auth_mode") or "basic").lower()
        access_token = None
        pw = ""
        if auth_mode == "oauth":
            from src.caldav_sync import _resolve_google_caldav_token
            access_token = _resolve_google_caldav_token(owner, acc)
            if not access_token:
                return {"ok": False, "error": "Google authorization expired — reconnect Google Calendar"}
            if not (url and user):
                return {"skipped": "caldav account credentials incomplete"}
        else:
            pw = decrypt(acc.get("password") or "")
            if not (url and user and pw):
                return {"skipped": "caldav account credentials incomplete"}
        from src.caldav_sync import validate_caldav_url
        try:
            url = validate_caldav_url(url)
        except ValueError as e:
            logger.warning("CalDAV write-back URL rejected: %s", e)
            return {"ok": False, "error": str(e)[:200]}
        acc_id = acc.get("id") or ""
        result = await asyncio.to_thread(
            _writeback_blocking, calendar_id, ev, delete, url, user, pw, owner, acc_id, access_token
        )
```

- [ ] **Step 5: Run tests to verify they pass + regression**

Run: `python -m pytest tests/test_caldav_oauth.py -k writeback_oauth tests/test_caldav_writeback.py tests/test_caldav_redirect_hardening.py -v`
Expected: PASS (new OAuth write-back test passes; existing write-back + redirect tests still pass)

- [ ] **Step 6: Commit**

```bash
git add src/caldav_writeback.py tests/test_caldav_oauth.py
git commit -m "feat(caldav): write back OAuth accounts with a bearer token"
```

---

### Task 7: Diagnostics for Google basic-auth accounts

When a Google account is still on basic auth (`access_token is None`) and Google rejects the primary calendar (`NotFoundError`/404 on `date_search`) or rejects v2 outright (`AuthorizationError` at discovery), emit a specific, actionable message instead of the generic one.

**Files:**
- Modify: `src/caldav_sync.py` — add `_is_google_host`; refine the discovery except block (`:306-308`) and the per-calendar `date_search` except block (`:378-382`); thread the basic-auth-Google flag into `_sync_blocking`.
- Test: `tests/test_caldav_oauth.py`

- [ ] **Step 1: Write the failing tests** (uses the fake-caldav installer pattern from `tests/test_caldav_google_principal_url.py`)

```python
# append to tests/test_caldav_oauth.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_caldav_oauth.py -k "oauth_hint or generic_message" -v`
Expected: FAIL — both calendars currently produce the generic `date_search failed` message.

- [ ] **Step 3: Add the host helper**

Add near `_google_caldav_events_url` in `src/caldav_sync.py` (after line 233):

```python
def _is_google_host(url: str) -> bool:
    """True for either Google CalDAV endpoint form (legacy www.google.com or
    the v2 apidata host)."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return (
        host.endswith("googleusercontent.com")
        or (host in ("www.google.com", "google.com") and "/calendar/dav/" in parts.path)
    )
```

- [ ] **Step 4: Emit the diagnostic on a Google basic-auth primary 404**

In `_sync_blocking`, replace the `date_search` try/except (`src/caldav_sync.py:378-382`):

```python
                try:
                    objs = remote_cal.date_search(start=start, end=end, expand=False)
                except NotFoundError as e:
                    if access_token is None and _is_google_host(url):
                        result["errors"].append(
                            f"{display_name}: primary calendar rejected basic-auth CalDAV — "
                            "this Google Workspace account needs OAuth. Use 'Connect Google Calendar'.")
                    else:
                        result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue
                except Exception as e:
                    result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue
```

- [ ] **Step 5: Emit the diagnostic when v2 rejects basic auth at discovery**

In `_sync_blocking`, replace the discovery `except (AuthorizationError, NotFoundError)` block (`src/caldav_sync.py:306-308`):

```python
    except (AuthorizationError, NotFoundError) as e:
        if access_token is None and _is_google_host(url):
            result["errors"].append(
                "Google rejected the app password (v2 CalDAV requires OAuth). "
                "Use 'Connect Google Calendar'.")
        else:
            result["errors"].append(f"Discovery failed: {e}")
        return result
```

- [ ] **Step 6: Run tests to verify they pass + regression**

Run: `python -m pytest tests/test_caldav_oauth.py tests/test_caldav_google_principal_url.py -v`
Expected: PASS (diagnostics fire only for Google basic-auth; the empty-discovery fallback test still works)

- [ ] **Step 7: Commit**

```bash
git add src/caldav_sync.py tests/test_caldav_oauth.py
git commit -m "feat(caldav): diagnose Google basic-auth primary calendar failures"
```

---

### Task 8: OAuth authorize + callback routes

**Files:**
- Modify: `routes/calendar_routes.py` — add two routes inside `setup_calendar_routes` (after the `/sync` route, ~line 900), reusing the existing `_get_caldav_accounts` / `_save_caldav_accounts` closures.
- Test: `tests/test_calendar_oauth_route.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_calendar_oauth_route.py -v`
Expected: FAIL — `AssertionError: route GET /api/calendar/oauth/google/authorize not found`

- [ ] **Step 3: Add the routes**

In `routes/calendar_routes.py`, add these imports near the top of the module (with the other imports):

```python
import os
import time
import urllib.parse
import uuid as _uuid
```

(`os` is already imported as `import os as _os` at line 43; add a plain `import os` too, or reuse `_os` consistently — use `os.environ` below, so add `import os`.)

Add inside `setup_calendar_routes`, after the `/sync` route (after line 900):

```python
    def _calendar_redirect_uri(request: Request) -> str:
        return (
            os.environ.get("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI")
            or f"http://{request.headers.get('host', 'localhost:7000')}/api/calendar/oauth/google/callback"
        )

    @router.get("/oauth/google/authorize")
    async def calendar_google_oauth_authorize(request: Request):
        from routes.email_helpers import make_oauth_state
        owner = _require_user(request)
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
        if not client_id:
            raise HTTPException(400, "GOOGLE_OAUTH_CLIENT_ID not set — add it to .env")
        # One-click connect mints the account id here; the callback creates the
        # prefs entry under it. The id is bound into the signed state.
        account_id = str(_uuid.uuid4())
        state = make_oauth_state(account_id, owner)
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": _calendar_redirect_uri(request),
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/calendar",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
        from fastapi.responses import RedirectResponse as _RR
        return _RR(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")

    @router.get("/oauth/google/callback")
    async def calendar_google_oauth_callback(
        code: str = Query(None),
        state: str = Query(None),
        error: str = Query(None),
        request: Request = None,
    ):
        from fastapi.responses import RedirectResponse as _RR
        from routes.email_helpers import verify_oauth_state
        from src.secret_storage import encrypt as _enc
        from src.google_oauth import exchange_authorization_code, google_client_credentials
        import httpx as _httpx

        if error:
            return _RR("/?section=integrations&calendar_oauth_error=google_error")
        if not code or not state:
            return _RR("/?section=integrations&calendar_oauth_error=missing_code")
        state_data = verify_oauth_state(state)
        if not state_data:
            return _RR("/?section=integrations&calendar_oauth_error=invalid_state")
        account_id = state_data.get("a", "")
        owner = state_data.get("o", "")
        client_id, client_secret = google_client_credentials()
        try:
            data = exchange_authorization_code(
                client_id, client_secret, code, _calendar_redirect_uri(request))
        except Exception:
            logger.warning("Google CalDAV token exchange failed")
            return _RR("/?section=integrations&calendar_oauth_error=token_exchange_failed")
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token", "")
        expiry = str(int(time.time()) + data.get("expires_in", 3600))
        if not access_token:
            return _RR("/?section=integrations&calendar_oauth_error=token_exchange_failed")

        email = ""
        try:
            ui = _httpx.get("https://www.googleapis.com/oauth2/v1/userinfo",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            if ui.is_success:
                email = (ui.json() or {}).get("email", "") or ""
        except Exception:
            email = ""
        if not email:
            return _RR("/?section=integrations&calendar_oauth_error=userinfo_failed")

        accounts = _get_caldav_accounts(owner)
        accounts.append({
            "id": account_id,
            "label": email,
            "auth_mode": "oauth",
            "oauth_provider": "google",
            "url": f"https://apidata.googleusercontent.com/caldav/v2/{email}/user",
            "username": email,
            "password": "",
            "oauth_access_token": _enc(access_token),
            "oauth_refresh_token": _enc(refresh_token) if refresh_token else "",
            "oauth_token_expiry": expiry,
        })
        _save_caldav_accounts(owner, accounts)
        return _RR("/?section=integrations&calendar_oauth_success=1")
```

Confirm `Query` and `HTTPException` are already imported in `routes/calendar_routes.py` (they are used by the existing CalDAV routes). If `logger` is not module-level, it is — see line 18.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_calendar_oauth_route.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add routes/calendar_routes.py tests/test_calendar_oauth_route.py
git commit -m "feat(calendar): one-click Google Calendar OAuth authorize + callback"
```

---

### Task 9: settings_scrub coverage test for CalDAV OAuth tokens

The scrub already recurses into lists and matches `*_access_token`/`*_refresh_token`; this pins that the new keys are covered so a future scrub change can't silently leak them.

**Files:**
- Test: `tests/test_settings_scrub.py` (append)

- [ ] **Step 1: Write the test**

```python
# append to tests/test_settings_scrub.py
def test_scrub_blanks_caldav_oauth_tokens_keeps_label():
    from src.settings_scrub import scrub_settings
    out = scrub_settings({"caldav_accounts": [{
        "id": "a1", "label": "Work", "username": "me@x.com",
        "oauth_access_token": "ya29.secret",
        "oauth_refresh_token": "1//secret",
        "oauth_token_expiry": "1750000000",
    }]})
    acc = out["caldav_accounts"][0]
    assert acc["oauth_access_token"] == ""
    assert acc["oauth_refresh_token"] == ""
    assert acc["label"] == "Work"          # non-secret preserved
    assert acc["username"] == "me@x.com"
    assert acc["oauth_token_expiry"] == "1750000000"  # not secret-shaped
```

- [ ] **Step 2: Run test to verify it passes**

Run: `python -m pytest tests/test_settings_scrub.py -k caldav_oauth -v`
Expected: PASS (the recursion + key patterns already cover this)

- [ ] **Step 3: Commit**

```bash
git add tests/test_settings_scrub.py
git commit -m "test(settings-scrub): pin CalDAV OAuth token redaction"
```

---

### Task 10: "Connect Google Calendar" button + return-param handling (UI)

Frontend only; verified manually (no JS unit-test harness for this module).

**Files:**
- Modify: `static/js/settings.js` — `showCalDavForm` (button, ~after line 3947); `_handleOauthRedirect` IIFE (`:5738-5769`).

- [ ] **Step 1: Add the Connect button to the CalDAV form**

In `showCalDavForm` (`static/js/settings.js`), insert this block immediately after the opening `<div class="settings-col">` (line 3948), before the Label row. For an OAuth-connected account, show connected status; otherwise show the connect button (one-click — no fields needed):

```javascript
          <div style="border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:8px">
            <div style="font-size:11px;font-weight:600;margin-bottom:6px">Google Workspace — primary calendar requires OAuth</div>
            <div id="uf-caldav-oauth-status" style="font-size:11px;opacity:0.7;margin-bottom:6px"></div>
            <button type="button" id="uf-caldav-oauth-btn" class="admin-btn-add" style="font-size:11px">Connect Google Calendar</button>
          </div>
```

- [ ] **Step 2: Wire the button** — add after the `uf-caldav-cancel` listener (line 3975):

```javascript
    // One-click Google Calendar OAuth — the server mints the account and
    // fills the v2 endpoint + email; no URL/password entry needed.
    {
      const oauthStatus = el('uf-caldav-oauth-status');
      const isOauth = !isNew && _calDavEditingOauth;
      if (oauthStatus) oauthStatus.textContent = isOauth
        ? '✓ Connected via Google OAuth'
        : 'Workspace / locked-down accounts: connect with Google instead of an app password.';
      const btn = el('uf-caldav-oauth-btn');
      if (btn) btn.addEventListener('click', () => {
        window.location.href = '/api/calendar/oauth/google/authorize';
      });
    }
```

Set `_calDavEditingOauth` where the form loads the existing account (inside the `if (acc) { ... }` block at line 3967):

```javascript
          _calDavEditingOauth = (acc.auth_mode === 'oauth');
```

Declare `let _calDavEditingOauth = false;` at the top of `showCalDavForm`.

- [ ] **Step 3: Handle the calendar return params**

In the `_handleOauthRedirect` IIFE (`static/js/settings.js:5738-5769`), broaden the guard and the banner text to also cover the calendar params. Replace line 5740:

```javascript
  const isEmail = sp.has('email_oauth_success') || sp.has('email_oauth_error');
  const isCal = sp.has('calendar_oauth_success') || sp.has('calendar_oauth_error');
  if (!isEmail && !isCal) return;
```

Replace the `success`/`errMsg` lines (5744-5745):

```javascript
  const success = sp.has('email_oauth_success') || sp.has('calendar_oauth_success');
  const errMsg = sp.get('email_oauth_error') || sp.get('calendar_oauth_error') || '';
```

Replace the banner text expression (5752-5754):

```javascript
      banner.textContent = success
        ? (isCal ? '✓ Google Calendar connected' : '✓ Google account connected — email is ready')
        : `Google OAuth failed: ${errMsg || 'unknown error'}`;
```

- [ ] **Step 4: Manual verification**

Run the app, open Settings → Integrations → Add CalDAV Calendar, confirm the "Connect Google Calendar" button is present and redirects to `/api/calendar/oauth/google/authorize`. (Full consent round-trip needs a configured Google Cloud OAuth app — see Task 11.) Confirm that returning with `?calendar_oauth_success=1` shows the success banner and opens Integrations.

- [ ] **Step 5: Commit**

```bash
git add static/js/settings.js
git commit -m "feat(ui): Connect Google Calendar button + OAuth return handling"
```

---

### Task 11: Deployment docs + `.env.example`

**Files:**
- Modify: `.env.example` (near the existing `GOOGLE_OAUTH_CLIENT_ID` entry)
- Modify: `docs/setup.md` (the Google OAuth / integrations section)

- [ ] **Step 1: Document the optional redirect-uri env in `.env.example`**

Add near the existing `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` lines:

```bash
# Optional: override the Google Calendar OAuth callback URL (defaults to the
# request host + /api/calendar/oauth/google/callback). Set this when behind a
# reverse proxy / custom domain. The Gmail and Calendar flows reuse the same
# GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET.
# GOOGLE_CALENDAR_OAUTH_REDIRECT_URI=https://your.domain/api/calendar/oauth/google/callback
```

- [ ] **Step 2: Document the Google Cloud setup in `docs/setup.md`**

Add to the Google OAuth section: to connect a Google Workspace calendar (whose primary calendar rejects app-password CalDAV), in the same Google Cloud project used for Gmail OAuth: (1) enable the **Google Calendar API**; (2) add the `https://www.googleapis.com/auth/calendar` scope to the OAuth consent screen; (3) register the redirect URI `<base>/api/calendar/oauth/google/callback`. Then in Settings → Integrations → Add CalDAV Calendar, click **Connect Google Calendar**.

- [ ] **Step 3: Commit**

```bash
git add .env.example docs/setup.md
git commit -m "docs: Google Calendar OAuth setup + redirect-uri env"
```

---

### Task 12: Full regression sweep

- [ ] **Step 1: Run the CalDAV + OAuth + scrub suites**

Run: `python -m pytest tests/test_caldav_oauth.py tests/test_google_oauth.py tests/test_calendar_oauth_route.py tests/test_email_oauth.py tests/test_caldav_google_principal_url.py tests/test_caldav_writeback.py tests/test_caldav_redirect_hardening.py tests/test_settings_scrub.py -v`
Expected: PASS (caldav-requiring tests may SKIP locally if `caldav` is absent; all run in CI)

- [ ] **Step 2: Commit any fixes**

```bash
git add -A
git commit -m "test(caldav-oauth): regression sweep green"
```

---

## Self-Review

**1. Spec coverage**
- OAuth real fix → Tasks 1–8 (shared helper, bearer client, token resolution, sync + write-back threading, routes).
- Diagnostics → Task 7.
- Read-write scope (`auth/calendar`) → Task 8 authorize.
- One-click connect, always-separate account → Task 8 callback (`accounts.append`).
- Per-account `auth_mode`, prefs storage, encrypted tokens → Tasks 4, 5, 8.
- `settings_scrub` coverage → Task 9.
- Security (HMAC state, owner-in-state, max_redirects, refresh failure non-destructive) → Tasks 4, 8.
- Deployment / no migration → Task 11.
- Testing matrix → each task's tests + Task 12.

**2. Placeholder scan** — no TBD/TODO; every code step shows complete code; commands have expected output.

**3. Type consistency** — `_build_dav_client(..., access_token=None)` signature matches all call sites (`_sync_blocking`, `_writeback_blocking`). `_resolve_google_caldav_token(owner, account_dict)` and `_refresh_google_caldav_token(owner, account_id)` names are used consistently across Tasks 4–6. `exchange_refresh_token` / `exchange_authorization_code` / `google_client_credentials` names match between Task 1 and Tasks 2, 4, 8. Account dict keys (`auth_mode`, `oauth_provider`, `oauth_access_token`, `oauth_refresh_token`, `oauth_token_expiry`) are identical in producer (Task 8 callback) and consumers (Tasks 4–7).

**Open risk to verify during execution:** whether Google's v2 OAuth principal discovery enumerates *secondary* calendars (Task 5). The core bug (primary) is fixed regardless via the `_open_url_as_calendar` `/user`→`/events` fallback; if discovery returns no secondaries over OAuth, that is a follow-up, not a regression of this plan's goal.
