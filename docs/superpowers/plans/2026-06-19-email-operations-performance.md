# Email Operations Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make interactive email operations (mark read/answered/done, switching, bulk-mark, search) fast by moving blocking IMAP off the event loop, collapsing bulk marking to one batched `UID STORE`, skipping redundant `SELECT`s, and fixing accented search.

**Architecture:** Four changes in the email subsystem: (1) flip ten `async def` mutation handlers to plain `def` so FastAPI runs them in its threadpool; (2) add a per-account lock + selected-folder cache to the existing IMAP connection pool so threaded handlers are leak-free and skip redundant `SELECT`; (3) add a generic `POST /api/email/bulk-flag` endpoint that issues one chunked batched `UID STORE` over a UID set, and route the frontend's multi-request "done"/"read"/"unread" paths through it; (4) pass `CHARSET UTF-8` + UTF-8 bytes in `search_emails` so accented queries stop crashing.

**Tech Stack:** Python 3 / FastAPI, stdlib `imaplib`, `pydantic` (`BaseModel`), vanilla-JS frontend (`static/js/emailInbox.js`, `static/js/emailLibrary.js`), `pytest` with hand-written fake IMAP connections.

**Spec:** `docs/superpowers/specs/2026-06-19-email-operations-performance-design.md`

**Commits:** The user has opted out of commits for this work. Each task ends at a green test run; staging and committing are deferred to the user. Do **not** run `git commit`.

---

## File Structure

**Modified**
- `routes/email_routes.py`
  - New module-level helper `_imap_select(conn, folder, readonly=False)` (near `_store_email_flag`, ~line 331).
  - New module-level constant `_BULK_STORE_CHUNK = 500`.
  - Pool rework inside `setup_email_routes()`: add `_pool_key_locks` + `_get_key_lock`; rework `_pooled_connect`/`_pooled_release` (~lines 525-575) to hold a per-account lock across connection use and reset `_odys_sel` on recycle.
  - Ten mutation handlers `async def` → `def`; all pooled handlers replace `conn.select(_q(folder))` with `_imap_select(...)`.
  - `search_emails`: UTF-8 charset SEARCH call.
  - New `bulk_flag` endpoint.
- `routes/email_helpers.py`
  - New `BulkFlagRequest(BaseModel)` (near `SendEmailRequest`, ~line 1643).
- `static/js/emailInbox.js` — "done" toggle → `bulk-flag`.
- `static/js/emailLibrary.js` — `_bulkAction` flag actions + single "done" toggles → `bulk-flag`.

**New**
- `tests/test_email_imap_select.py`
- `tests/test_email_pool_locking.py`
- `tests/test_email_handlers_threaded.py`
- `tests/test_email_search_charset.py`
- `tests/test_email_bulk_flag.py`

**Updated tests**
- `tests/test_email_library_bulk_actions.py`

**Test harness conventions (used throughout):** Build the router with `router = routes.email_routes.setup_email_routes()`, pull a handler with a small `_endpoint(router, path, method)` loop over `router.routes`, and call it directly with explicit `owner=` (the ASGI app is not booted). Stub IMAP with a hand-written fake connection class and `monkeypatch.setattr(routes.email_routes, "_imap", fake_cm)` (a `@contextmanager` yielding the fake) or `monkeypatch.setattr(routes.email_routes, "_imap_connect", fake_connect)`. This mirrors `tests/test_email_owner_scope.py` and `tests/test_email_fallback_reconnect.py`.

---

### Task 1: Selected-folder cache helper `_imap_select`

**Files:**
- Modify: `routes/email_routes.py` (add module-level helper near `_store_email_flag`, ~line 331)
- Test: `tests/test_email_imap_select.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_imap_select.py
"""_imap_select skips a redundant SELECT on a pooled connection.

A remote-Gmail SELECT is a full round-trip (~450ms measured), so re-selecting a
folder the connection is already on is pure waste. The (folder, readonly) key
matters: a readonly SELECT left by a search must be re-issued read-write before
a STORE.
"""
from routes.email_routes import _imap_select


class _FakeConn:
    def __init__(self, status="OK"):
        self.status = status
        self.selects = []  # (mailbox, readonly) per SELECT actually issued

    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        return (self.status, [b"1"])


def test_select_runs_once_for_same_folder_and_mode():
    c = _FakeConn()
    assert _imap_select(c, "INBOX") == "OK"
    assert _imap_select(c, "INBOX") == "OK"
    assert len(c.selects) == 1  # second call skipped


def test_select_reissued_when_mode_changes():
    c = _FakeConn()
    _imap_select(c, "INBOX", readonly=True)
    _imap_select(c, "INBOX", readonly=False)
    assert len(c.selects) == 2


def test_select_reissued_when_folder_changes():
    c = _FakeConn()
    _imap_select(c, "INBOX")
    _imap_select(c, "Archive")
    assert len(c.selects) == 2


def test_failed_select_is_not_cached():
    c = _FakeConn(status="NO")
    _imap_select(c, "INBOX")
    _imap_select(c, "INBOX")
    assert len(c.selects) == 2  # NO not cached -> retried next time
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_imap_select.py -v`
Expected: FAIL with `ImportError: cannot import name '_imap_select'`.

- [ ] **Step 3: Write minimal implementation**

In `routes/email_routes.py`, immediately after `_store_email_flag` (ends ~line 337), add:

```python
def _imap_select(conn, folder, readonly=False):
    """SELECT `folder` only if this connection isn't already on it in the same
    read/write mode. Pooled connections persist across requests, so skipping a
    redundant SELECT (a full server round-trip — ~450ms on remote Gmail) is a
    large win. The (folder, readonly) key is required: a readonly SELECT left by
    a search must be re-issued read-write before a STORE."""
    want = (folder, bool(readonly))
    if getattr(conn, "_odys_sel", None) == want:
        return "OK"
    status, _ = conn.select(_q(folder), readonly=readonly)
    conn._odys_sel = want if status == "OK" else None
    return status
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_email_imap_select.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Verify (no commit)**

Run: `python -m pytest tests/test_email_imap_select.py -q`
Expected: all green. Do not commit.

---

### Task 2: Per-account pool lock + folder-cache reset

Threading the mutation handlers (Task 3) lets the frontend's client-side
6-way concurrency hit the server in parallel. The current single-slot pool
would then open a fresh-connect storm and leak handles (overwrite without
`logout`). This task makes the pool serialize per account on one reused
connection and release the lock on every path including connect failure.

**Files:**
- Modify: `routes/email_routes.py` inside `setup_email_routes()` — `_pool_lock` block (~line 531), `_pooled_connect` (~533-565), `_pooled_release` (~567-575)
- Test: `tests/test_email_pool_locking.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_pool_locking.py
"""The IMAP pool serializes per (account_id, owner) on one reused connection and
releases its key lock on every path — including a failed connect — so threaded
handlers can't deadlock or leak connections.
"""
import threading

import pytest

import routes.email_routes as email_routes


class _FakeConn:
    def __init__(self):
        self.logged_out = False

    def noop(self):
        return ("OK", [b""])

    def logout(self):
        self.logged_out = True


def _pool():
    router = email_routes.setup_email_routes()
    return router._email_pool["connect"], router._email_pool["release"]


def test_round_trip_reuses_pooled_connection(monkeypatch):
    made = []

    def fake_connect(account_id, owner=""):
        c = _FakeConn()
        made.append(c)
        return c

    monkeypatch.setattr(email_routes, "_imap_connect", fake_connect)
    connect, release = _pool()

    conn1, reused1 = connect("acct", owner="bob")
    assert reused1 is False and len(made) == 1
    release("acct", conn1, ok=True, owner="bob")

    conn2, reused2 = connect("acct", owner="bob")
    assert reused2 is True and conn2 is conn1
    assert len(made) == 1  # no new connection opened
    release("acct", conn2, ok=True, owner="bob")


def test_connect_failure_releases_key_lock(monkeypatch):
    def boom(account_id, owner=""):
        raise RuntimeError("imap down")

    monkeypatch.setattr(email_routes, "_imap_connect", boom)
    connect, release = _pool()

    with pytest.raises(RuntimeError):
        connect("acct", owner="bob")

    # The key lock must have been released; a second attempt must not deadlock.
    done = []

    def worker():
        try:
            connect("acct", owner="bob")
        except RuntimeError:
            done.append(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)
    assert not t.is_alive(), "second connect deadlocked -> key lock not released"
    assert done == [True]


def test_same_key_blocks_until_release(monkeypatch):
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id, owner="": _FakeConn())
    connect, release = _pool()

    conn1, _ = connect("acct", owner="bob")  # holds the key lock
    second_started = threading.Event()
    second_got = threading.Event()

    def worker():
        second_started.set()
        c, _ = connect("acct", owner="bob")
        second_got.set()
        release("acct", c, ok=True, owner="bob")

    t = threading.Thread(target=worker)
    t.start()
    assert second_started.wait(1.0)
    assert not second_got.wait(0.3), "second connect should block while key lock held"
    release("acct", conn1, ok=True, owner="bob")  # free the lock
    assert second_got.wait(2.0), "second connect should proceed after release"
    t.join(timeout=2.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_pool_locking.py -v`
Expected: FAIL — `test_same_key_blocks_until_release` fails (current pool does not serialize; the second connect proceeds immediately) and/or `test_connect_failure_releases_key_lock` behavior is undefined.

- [ ] **Step 3: Write minimal implementation**

In `routes/email_routes.py`, in the pool-state block (right after `_pool_lock = _threading.Lock()`, ~line 531) add the per-key lock registry:

```python
    _pool_key_locks = {}  # (account_id, owner) → threading.Lock (held while in use)

    def _get_key_lock(pool_key):
        with _pool_lock:
            lock = _pool_key_locks.get(pool_key)
            if lock is None:
                lock = _threading.Lock()
                _pool_key_locks[pool_key] = lock
            return lock
```

Replace `_pooled_connect` (currently ~533-565) with:

```python
    def _pooled_connect(account_id, owner=""):
        """Acquire this account's lock, then reuse a live pooled connection or
        open a fresh one. The lock is held until `_pooled_release`, so all
        pooled ops for one account serialize on a single connection (no
        fresh-connect storm / handle leak under concurrency). Different accounts
        proceed independently.
        """
        pool_key = (account_id, owner)
        lock = _get_key_lock(pool_key)
        lock.acquire()
        try:
            now = _time.monotonic()
            with _pool_lock:
                entry = _IMAP_POOL.pop(pool_key, None)
            if entry:
                conn, last_used = entry
                if (now - last_used) < _IMAP_IDLE_MAX:
                    try:
                        conn.noop()
                        return conn, True  # reused (lock stays held)
                    except Exception:
                        try: conn.logout()
                        except Exception: pass
                else:
                    try: conn.logout()
                    except Exception: pass
            # Fresh connection (network I/O outside _pool_lock).
            return _imap_connect(account_id, owner=owner), False
        except BaseException:
            # Connect/noop path raised before we could hand the conn back —
            # release the key lock so the next caller for this account is not
            # wedged.
            lock.release()
            raise
```

Replace `_pooled_release` (currently ~567-575) with:

```python
    def _pooled_release(account_id, conn, ok=True, owner=""):
        pool_key = (account_id, owner)
        try:
            if not ok:
                try: conn.logout()
                except Exception: pass
                try: conn._odys_sel = None
                except Exception: pass
                return
            with _pool_lock:
                _IMAP_POOL[pool_key] = (conn, _time.monotonic())
        finally:
            # Always release the per-account lock acquired in _pooled_connect.
            _get_key_lock(pool_key).release()
```

Note: `_imap()` in `email_helpers.py` already calls `pool_connect` then `pool_release` in a `finally`; the only gap was a connect-time raise (handled above by `_pooled_connect`'s `except`). No change to `email_helpers.py` is required for this task.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_email_pool_locking.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Verify no regression in existing pool/leak tests (no commit)**

Run: `python -m pytest tests/test_email_pool_locking.py tests/test_imap_leak_fixes.py tests/test_email_polly_imap_leak.py tests/test_email_fallback_reconnect.py -q`
Expected: all green. Do not commit.

---

### Task 3: Move the 10 mutation handlers off the event loop + adopt `_imap_select`

**Files:**
- Modify: `routes/email_routes.py` handlers — `mark_unread` (~1752), `flag_email` (~1766), `mark_read` (~1782), `archive_email` (~1798, already `def`), `delete_email` (~1812), `delete_email_permanent` (~1826), `delete_odysseus_reminder_emails` (~1841), `move_email` (~1916), `list_folders` (~1930), `mark_answered` (~1948), `clear_answered` (~1961), and `search_emails` (~1139 select call)
- Test: `tests/test_email_handlers_threaded.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_handlers_threaded.py
"""The flag-mutation handlers must run in FastAPI's threadpool (plain `def`),
not as `async def` that block the event loop on synchronous remote IMAP I/O.
Also verifies mark_read still sets \\Seen through a stubbed connection.
"""
import inspect
from contextlib import contextmanager

import routes.email_routes as email_routes

MUTATION_ROUTES = [
    ("/api/email/mark-unread/{uid}", "POST"),
    ("/api/email/flag/{uid}", "POST"),
    ("/api/email/mark-read/{uid}", "POST"),
    ("/api/email/delete/{uid}", "DELETE"),
    ("/api/email/delete-permanent/{uid}", "DELETE"),
    ("/api/email/odysseus/reminders", "DELETE"),
    ("/api/email/move/{uid}", "POST"),
    ("/api/email/folders", "GET"),
    ("/api/email/mark-answered/{uid}", "POST"),
    ("/api/email/clear-answered/{uid}", "POST"),
]


def _endpoint(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_mutation_handlers_run_off_the_event_loop():
    router = email_routes.setup_email_routes()
    for path, method in MUTATION_ROUTES:
        ep = _endpoint(router, path, method)
        assert not inspect.iscoroutinefunction(ep), (
            f"{method} {path} must be sync def (threadpool), not async-blocking"
        )


class _StoreConn:
    def __init__(self):
        self.selects = []
        self.stores = []
        self._odys_sel = None

    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "FETCH":          # _uid_exists probe
            return ("OK", [b"1 (UID 5)"])
        if cmd == "STORE":
            self.stores.append(args)
            return ("OK", [b""])
        return ("OK", [None])


def test_mark_read_sets_seen_flag(monkeypatch):
    conn = _StoreConn()

    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    mark_read = _endpoint(router, "/api/email/mark-read/{uid}", "POST")

    result = mark_read("5", folder="INBOX", account_id=None, owner="")  # sync call
    assert result == {"success": True}
    assert conn.stores, "expected a UID STORE"
    seq, op, flag = conn.stores[0]
    assert op == "+FLAGS" and "\\Seen" in str(flag)
    assert len(conn.selects) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_handlers_threaded.py -v`
Expected: FAIL — `test_mutation_handlers_run_off_the_event_loop` fails (handlers are still `async def`); `test_mark_read_sets_seen_flag` fails because calling the still-`async` handler returns a coroutine, not `{"success": True}`.

- [ ] **Step 3: Write minimal implementation**

For each handler below, delete the `async` keyword and replace its
`conn.select(_q(folder))` with `_imap_select(conn, folder)`. Add the marker
comment used on `archive_email` to the ones being newly converted. Example —
`mark_read` becomes:

```python
    @router.post("/mark-read/{uid}")
    # Sync def: blocking IMAP I/O with no awaits — runs in a threadpool instead
    # of blocking the event loop. See search_emails / archive_email.
    def mark_read(uid: str, folder: str = Query("INBOX"), account_id: str | None = Query(None), owner: str = Depends(require_owner)):
        """Mark an email as read (set \\Seen flag)."""
        try:
            with _imap(account_id, owner=owner) as conn:
                _imap_select(conn, folder)
                if not _store_email_flag(conn, uid, "\\Seen", add=True):
                    return {"success": False, "error": "Email not found"}
            _invalidate_list_cache(account_id, folder)
            return {"success": True}
        except Exception as e:
            logger.error(f"Failed to mark read {uid}: {e}")
            return {"success": False, "error": "Mail operation failed"}
```

Apply the identical pattern (drop `async`, swap to `_imap_select`) to:
`mark_unread`, `flag_email`, `delete_email`, `delete_email_permanent`,
`move_email`, `mark_answered`, `clear_answered`. For `list_folders` (no
`select`) and `delete_email_permanent` keep their existing bodies otherwise
unchanged.

`archive_email` is already `def`; just swap its `conn.select(_q(folder))` →
`_imap_select(conn, folder)`.

`delete_odysseus_reminder_emails`: drop `async`; replace the loop's
`st, _ = conn.select(_q(folder_name))` (~1876) with `st = _imap_select(conn, folder_name)`.

`search_emails` (~1139): replace `conn.select(_q(effective_folder), readonly=True)`
with `_imap_select(conn, effective_folder, readonly=True)`. (It is already
sync `def`; the SEARCH charset fix is Task 4.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_email_handlers_threaded.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify no regression in email route tests (no commit)**

Run: `python -m pytest tests/test_email_owner_scope.py tests/test_email_imap_timeout.py -q`
Expected: all green. Do not commit.

---

### Task 4: Fix accented search (UTF-8 CHARSET)

**Files:**
- Modify: `routes/email_routes.py` `search_emails` SEARCH call (~line 1145)
- Test: `tests/test_email_search_charset.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_search_charset.py
"""IMAP SEARCH must be issued with CHARSET UTF-8 and a UTF-8 *bytes* criteria so
accented queries don't crash. Regression for the production error:
`Search failed: 'ascii' codec can't encode character '\\xe1'`.
"""
from contextlib import contextmanager

import routes.email_routes as email_routes


class _SearchConn:
    def __init__(self):
        self.search_args = None
        self._odys_sel = None

    def list(self):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])  # no All Mail -> stay on INBOX

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.search_args = args
            return ("OK", [b""])  # no hits -> short-circuit
        return ("OK", [None])


def _endpoint(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_search_uses_utf8_charset_for_accented_query(monkeypatch):
    conn = _SearchConn()

    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    search = _endpoint(router, "/api/email/search", "GET")

    # Must not raise UnicodeEncodeError.
    result = search(q="árvíz", folder="INBOX", limit=50, account_id=None, owner="")
    assert "error" not in result

    assert conn.search_args is not None
    assert conn.search_args[0] == "CHARSET"
    assert conn.search_args[1] == "UTF-8"
    assert isinstance(conn.search_args[2], (bytes, bytearray))
    assert "árvíz".encode("utf-8") in conn.search_args[2]


def test_search_ascii_query_still_works(monkeypatch):
    conn = _SearchConn()

    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    search = _endpoint(router, "/api/email/search", "GET")

    result = search(q="invoice", folder="INBOX", limit=50, account_id=None, owner="")
    assert "error" not in result
    assert b"invoice" in conn.search_args[2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_search_charset.py -v`
Expected: FAIL — current code calls `_imap_uid_search(conn, search_cmd)` → `conn.uid("SEARCH", None, criteria)`, so `search_args[0]` is `None`, not `"CHARSET"` (the accented test would also raise `UnicodeEncodeError` against a real imaplib, captured here as a non-matching call).

- [ ] **Step 3: Write minimal implementation**

In `search_emails`, replace the search call (~line 1145):

```python
                status, data = _imap_uid_search(conn, search_cmd)
```

with a direct UTF-8 SEARCH (leave the shared `_imap_uid_search` helper and its
ASCII-only `_list_emails_sync` filter callers untouched):

```python
                # Encode as UTF-8 bytes + CHARSET so accented queries (e.g. "árvíz")
                # don't blow up imaplib's ASCII command encoder. ASCII is a UTF-8
                # subset, so plain queries are unaffected.
                status, data = conn.uid("SEARCH", "CHARSET", "UTF-8", search_cmd.encode("utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_email_search_charset.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify (no commit)**

Run: `python -m pytest tests/test_email_search_charset.py -q`
Expected: all green. Do not commit.

---

### Task 5: `POST /api/email/bulk-flag` endpoint

One generic endpoint collapses the multi-request "done"/"read"/"unread"/"clear"
paths into a single chunked batched `UID STORE`. `folder` and `account_id` are
query params (matching every sibling email endpoint); `uids`/`add`/`remove` are
the JSON body.

**Files:**
- Modify: `routes/email_helpers.py` — add `BulkFlagRequest` near `SendEmailRequest` (~1643)
- Modify: `routes/email_routes.py` — import `BulkFlagRequest`; add `_BULK_STORE_CHUNK` constant; add `bulk_flag` endpoint
- Test: `tests/test_email_bulk_flag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_bulk_flag.py
"""POST /api/email/bulk-flag issues ONE chunked batched UID STORE over a UID set
(not 2N per-email requests), validates input, and is owner-scoped.
"""
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

import routes.email_routes as email_routes
from routes.email_helpers import BulkFlagRequest


class _BulkConn:
    def __init__(self):
        self.selects = []
        self.stores = []  # (seqset, op, flags)
        self._odys_sel = None

    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "STORE":
            self.stores.append(args)
            return ("OK", [b""])
        return ("OK", [None])


def _bulk(monkeypatch, conn):
    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    for r in router.routes:
        if r.path == "/api/email/bulk-flag" and "POST" in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError("bulk-flag route missing")


def test_single_store_over_uid_set(monkeypatch):
    conn = _BulkConn()
    bulk = _bulk(monkeypatch, conn)
    req = BulkFlagRequest(uids=["1", "2", "3"], add=["\\Seen", "\\Answered"])
    result = bulk(req, folder="INBOX", account_id=None, owner="")
    assert result["success"] is True and result["count"] == 3
    assert len(conn.stores) == 1
    seqset, op, flags = conn.stores[0]
    assert seqset == b"1,2,3"
    assert op == "+FLAGS"
    assert flags == "(\\Seen \\Answered)"
    assert len(conn.selects) == 1


def test_add_and_remove_emit_two_stores(monkeypatch):
    conn = _BulkConn()
    bulk = _bulk(monkeypatch, conn)
    req = BulkFlagRequest(uids=["7"], add=["\\Seen"], remove=["\\Answered"])
    bulk(req, folder="INBOX", account_id=None, owner="")
    ops = [s[1] for s in conn.stores]
    assert ops == ["+FLAGS", "-FLAGS"]


def test_large_set_is_chunked(monkeypatch):
    conn = _BulkConn()
    bulk = _bulk(monkeypatch, conn)
    uids = [str(i) for i in range(1, 1102)]  # 1101 -> ceil(1101/500) = 3 chunks
    bulk(BulkFlagRequest(uids=uids, add=["\\Seen"]), folder="INBOX", account_id=None, owner="")
    assert len(conn.stores) == 3
    assert conn.stores[0][0].count(b",") == 499  # 500 uids -> 499 commas


def test_rejects_non_numeric_uid(monkeypatch):
    conn = _BulkConn()
    bulk = _bulk(monkeypatch, conn)
    with pytest.raises(HTTPException) as ei:
        bulk(BulkFlagRequest(uids=["1", "2 OR 1=1"], add=["\\Seen"]), folder="INBOX", account_id=None, owner="")
    assert ei.value.status_code == 400


def test_rejects_unknown_flag(monkeypatch):
    conn = _BulkConn()
    bulk = _bulk(monkeypatch, conn)
    with pytest.raises(HTTPException) as ei:
        bulk(BulkFlagRequest(uids=["1"], add=["\\Deleted"]), folder="INBOX", account_id=None, owner="")
    assert ei.value.status_code == 400


def test_rejects_empty(monkeypatch):
    conn = _BulkConn()
    bulk = _bulk(monkeypatch, conn)
    with pytest.raises(HTTPException):
        bulk(BulkFlagRequest(uids=[], add=["\\Seen"]), folder="INBOX", account_id=None, owner="")
    with pytest.raises(HTTPException):
        bulk(BulkFlagRequest(uids=["1"]), folder="INBOX", account_id=None, owner="")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_bulk_flag.py -v`
Expected: FAIL — `ImportError: cannot import name 'BulkFlagRequest'` (and the route does not exist yet).

- [ ] **Step 3: Write minimal implementation**

In `routes/email_helpers.py`, after `SendEmailRequest` (ends ~1662), add:

```python
class BulkFlagRequest(BaseModel):
    uids: List[str]
    add: Optional[List[str]] = None
    remove: Optional[List[str]] = None
```

In `routes/email_routes.py`, add `BulkFlagRequest` to the `from routes.email_helpers import (...)` block (the line with `SendEmailRequest, ExtractStyleRequest,` ~line 55):

```python
    SendEmailRequest, ExtractStyleRequest, BulkFlagRequest,
```

Add a module-level constant near the other email constants (top of file, after the imports):

```python
_BULK_STORE_CHUNK = 500  # bound the IMAP command-line length for a UID set
_BULK_FLAG_ALLOWED = {"\\Seen", "\\Answered", "\\Flagged"}  # never \\Deleted via bulk
```

Add the endpoint inside `setup_email_routes()` next to the other mutation
handlers (e.g. right after `clear_answered`, ~line 1971):

```python
    @router.post("/bulk-flag")
    # Sync def: blocking IMAP I/O with no awaits — runs in a threadpool.
    def bulk_flag(req: BulkFlagRequest, folder: str = Query("INBOX"),
                  account_id: str | None = Query(None), owner: str = Depends(require_owner)):
        """Add/remove IMAP flags on a set of UIDs in one batched STORE.

        Collapses the UI's per-email "mark done / read / unread" requests into a
        single round-trip. `add`/`remove` are restricted to \\Seen \\Answered
        \\Flagged; \\Deleted is intentionally excluded so this cannot mass-delete.
        """
        if account_id:
            _assert_owns_account(account_id, owner)
        uids = [str(u) for u in (req.uids or [])]
        if not uids or not all(re.fullmatch(r"\d+", u) for u in uids):
            raise HTTPException(400, "Invalid uids")
        add = list(req.add or [])
        remove = list(req.remove or [])
        if not add and not remove:
            raise HTTPException(400, "No flags to change")
        if any(f not in _BULK_FLAG_ALLOWED for f in add + remove):
            raise HTTPException(400, "Unsupported flag")
        try:
            with _imap(account_id, owner=owner) as conn:
                _imap_select(conn, folder)
                for i in range(0, len(uids), _BULK_STORE_CHUNK):
                    seqset = ",".join(uids[i:i + _BULK_STORE_CHUNK]).encode()
                    if add:
                        conn.uid("STORE", seqset, "+FLAGS", "(" + " ".join(add) + ")")
                    if remove:
                        conn.uid("STORE", seqset, "-FLAGS", "(" + " ".join(remove) + ")")
            _invalidate_list_cache(account_id, folder)
            return {"success": True, "count": len(uids), "folder": folder}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"bulk_flag failed: {e}")
            return {"success": False, "error": "Mail operation failed"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_email_bulk_flag.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Verify (no commit)**

Run: `python -m pytest tests/test_email_bulk_flag.py tests/test_email_handlers_threaded.py -q`
Expected: all green. Do not commit.

---

### Task 6: Frontend — route "done"/"read"/"unread" through `bulk-flag`

**Files:**
- Modify: `static/js/emailLibrary.js` — `_bulkAction` (~6000) flag actions; single "done" toggles (~2968-2969, 5577-5578, 5770-5771, 6045-6046)
- Modify: `static/js/emailInbox.js` — "done" toggle (~1110-1115)
- Test: `tests/test_email_library_bulk_actions.py` (update existing assertions)

- [ ] **Step 1: Update the failing test**

Replace the two existing tests in `tests/test_email_library_bulk_actions.py`
with assertions for the new single-request behavior, preserving the original
invariants (writes persist to the provider, backend success is checked,
cache write-back runs):

```python
def test_bulk_flag_actions_use_single_bulk_flag_request():
    """Bulk done/read/unread must issue ONE /bulk-flag request over all UIDs,
    not a per-UID loop of mark-read/mark-answered."""
    src = _bulk_action_source()
    assert "Local toggle for now" not in src
    assert "/api/email/bulk-flag" in src
    assert "JSON.stringify" in src
    # done sets both flags; read/unread toggle \Seen via add/remove
    assert "Answered" in src and "Seen" in src


def test_bulk_flag_checks_backend_success_before_syncing_cache():
    src = _bulk_action_source()
    assert "data?.success === false" in src
    assert "throw new Error(data?.error" in src
    assert "_libCacheWriteBack()" in src
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_email_library_bulk_actions.py -v`
Expected: FAIL — `/api/email/bulk-flag` not present in `_bulkAction` source.

- [ ] **Step 3: Implement the frontend change**

In `static/js/emailLibrary.js` `_bulkAction(action)`, **before** the per-UID
`handleOne`/concurrency loop, add a fast path for the flag actions and return
early (leaving `archive`/`delete` on the existing loop):

```javascript
  // Flag-only actions collapse to ONE batched server request.
  if (action === 'done' || action === 'read' || action === 'unread') {
    const add = [];
    const remove = [];
    if (action === 'done') { add.push('\\Seen', '\\Answered'); }
    else if (action === 'read') { add.push('\\Seen'); }
    else if (action === 'unread') { remove.push('\\Seen'); }

    for (const uid of uids) {                       // optimistic UI
      const em = state._libEmails.find(e => String(e.uid) === String(uid));
      if (em) {
        if (action === 'done') { em.is_answered = true; em.is_read = true; }
        else { em.is_read = (action === 'read'); }
      }
      if (action !== 'done') _syncEmailReadState(uid, action === 'read');
    }

    try {
      const res = await fetch(`${API_BASE}/api/email/bulk-flag?folder=${encodeURIComponent(state._libFolder)}${_acct()}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uids: uids.map(String), add, remove }),
      });
      let data = null;
      try { data = await res.json(); } catch (_) {}
      if (!res.ok || data?.success === false) {
        throw new Error(data?.error || `HTTP ${res.status}`);
      }
      _libCacheWriteBack();
    } catch (e) {
      console.error(`Bulk ${action} failed:`, e);
    } finally {
      _restoreBulkButtons();   // reuse whatever the existing finally block does
    }
    _renderGrid();
    return;
  }
```

> Implementation note: if the existing `_bulkAction` performs button-restore
> inline in its `finally` rather than via a helper, inline the same restore
> statements here instead of `_restoreBulkButtons()` — match the existing code,
> do not invent a new helper.

Replace each single-email "done" 2-fetch sequence
(`emailLibrary.js` ~2968-2969, ~5577-5578, ~5770-5771, ~6045-6046) of the form:

```javascript
          await fetch(`${API_BASE}/api/email/mark-answered/${em.uid}?folder=${encodeURIComponent(state._libFolder)}${_acct()}`, { method: 'POST' });
          await fetch(`${API_BASE}/api/email/mark-read/${em.uid}?folder=${encodeURIComponent(state._libFolder)}${_acct()}`, { method: 'POST' });
```

with a single bulk-flag call:

```javascript
          await fetch(`${API_BASE}/api/email/bulk-flag?folder=${encodeURIComponent(state._libFolder)}${_acct()}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uids: [String(em.uid)], add: ['\\Seen', '\\Answered'] }),
          });
```

(At ~6045-6046 the result is checked via `ansRes`/`readRes`; replace with a
single `const res = await fetch(...)` and `if (!res.ok) throw new Error(...)`.)
Leave the matching un-done branches that call `clear-answered` unchanged.

In `static/js/emailInbox.js` (~1110-1115), replace the "done" branch's two
fetches the same way:

```javascript
    if (newState) {
      await fetch(`${API_BASE}/api/email/bulk-flag?folder=${encodeURIComponent(_currentFolder)}${_acct()}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uids: [String(em.uid)], add: ['\\Seen', '\\Answered'] }),
      });
    } else {
      await fetch(`${API_BASE}/api/email/clear-answered/${em.uid}?folder=${encodeURIComponent(_currentFolder)}${_acct()}`, { method: 'POST' });
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_email_library_bulk_actions.py -v`
Expected: PASS.

- [ ] **Step 5: Verify the full email test set (no commit)**

Run: `python -m pytest tests/test_email_imap_select.py tests/test_email_pool_locking.py tests/test_email_handlers_threaded.py tests/test_email_search_charset.py tests/test_email_bulk_flag.py tests/test_email_library_bulk_actions.py tests/test_email_owner_scope.py tests/test_imap_leak_fixes.py -q`
Expected: all green. Do not commit.

---

## Self-Review

**Spec coverage:**
- A1 (off-loop handlers) → Task 3. ✔
- B1 (bulk-flag endpoint + frontend) → Task 5 (backend) + Task 6 (frontend). ✔
- C1 (per-account lock + folder cache) → Task 1 (`_imap_select`) + Task 2 (lock) + Task 3 (adoption). ✔
- #5 (UTF-8 search) → Task 4. ✔
- Security (UID `^\d+$`, flag allowlist sans `\Deleted`, owner scope) → Task 5. ✔
- Tests 1-11 from spec map onto Tasks 1-5; frontend test update → Task 6. ✔

**Placeholder scan:** No "TBD"/"add error handling"/"similar to" — every code step is complete. The one prose note (button-restore in `_bulkAction`) is explicit about matching existing code rather than a placeholder.

**Type/name consistency:** `_imap_select` (Task 1) used identically in Tasks 3 & 5. `BulkFlagRequest(uids/add/remove)` defined in Task 5 helper step, imported and used with the same field names in the endpoint and the Task-5 tests. `_BULK_STORE_CHUNK`/`_BULK_FLAG_ALLOWED` defined once and referenced once. Pool helpers `_pooled_connect`/`_pooled_release`/`_get_key_lock` signatures match their call sites in `_imap()`.

**Out of scope (unchanged):** multi-connection pool, bulk archive/delete/move endpoints, prefetch breadth — as stated in the spec.
