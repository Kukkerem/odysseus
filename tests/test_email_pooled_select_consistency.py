"""Every SELECT on a *pooled* IMAP connection must go through `_imap_select` so
the connection's `_odys_sel` cache stays authoritative.

The pool reuses one connection per (account_id, owner) across requests. A read
path that calls raw `conn.select(...)` changes the server's selected folder/mode
without updating `_odys_sel`; a later mutation handler that trusts the cache
would then skip its own SELECT and STORE against the wrong folder or in a
read-only session. This test pins a representative pooled read path
(`resolve_contact`, which selects Sent/INBOX/Drafts read-only) and asserts the
cache reflects the last selection it made.
"""
from contextlib import contextmanager

import pytest

import routes.email_routes as email_routes


def _endpoint(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class _SelTrackConn:
    def __init__(self):
        self.selects = []
        self._odys_sel = None

    def select(self, mailbox, readonly=False):
        self.selects.append((mailbox, readonly))
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        return ("OK", [b""])  # no hits -> handler moves to the next folder


@pytest.mark.asyncio
async def test_pooled_read_path_updates_select_cache(monkeypatch):
    conn = _SelTrackConn()

    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    resolve_contact = _endpoint(router, "/api/email/resolve-contact", "GET")

    await resolve_contact(name="bob", owner="")

    # If the read path used raw conn.select(), _odys_sel would still be None and
    # a later pooled mutation could wrongly skip its SELECT.
    assert conn._odys_sel is not None, "pooled read path bypassed _imap_select"
    folder, readonly = conn._odys_sel
    assert folder in ("Sent", "INBOX", "Drafts")
    assert readonly is True
