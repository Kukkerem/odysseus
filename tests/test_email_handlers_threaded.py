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


def test_list_folders_offloads_instead_of_blocking():
    """`/api/email/folders` is the one handler that stays `async def`.

    It keeps the event loop free a different way: the blocking IMAP LIST runs in
    `asyncio.to_thread` under a `wait_for` timeout, which also bounds a hung
    server — something a plain sync (threadpool) handler cannot express. So the
    no-blocking contract still holds; only the mechanism differs.
    """
    router = email_routes.setup_email_routes()
    ep = _endpoint(router, "/api/email/folders", "GET")
    assert inspect.iscoroutinefunction(ep)
    src = inspect.getsource(ep)
    assert "to_thread(_list_folders_sync)" in src, "must offload the blocking LIST"
    assert "wait_for" in src, "must bound the offloaded call with a timeout"


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
