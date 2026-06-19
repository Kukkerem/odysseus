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
