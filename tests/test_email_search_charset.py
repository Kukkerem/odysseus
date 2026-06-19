# tests/test_email_search_charset.py
"""Non-ASCII IMAP SEARCH must send the term as a CHARSET UTF-8 *literal*, never
as inline 8-bit octets.

Regression for two production bugs in order:
  1. `Search failed: 'ascii' codec can't encode character '\\xe1'` — imaplib's
     ASCII command encoder choking on accented queries.
  2. `Search failed: UID command error: BAD [b'Could not parse command']` —
     Gmail rejecting raw UTF-8 octets placed inline on the command line.

The fix: ASCII queries keep the single inline `UID SEARCH (OR ...)`; non-ASCII
queries issue three `UID SEARCH CHARSET UTF-8 <FIELD>` commands, each carrying
the term via `conn.literal` (imaplib's `{N}` literal), and union the results.
"""
from contextlib import contextmanager

import routes.email_routes as email_routes


class _FakeConn:
    """Records every UID SEARCH plus the `conn.literal` value at call time, and
    returns canned hits keyed by field (or "ASCII" for the inline path)."""

    def __init__(self, hits=None):
        self.literal = None
        self.searches = []  # list of (args_tuple, literal_at_call_time)
        self._odys_sel = None
        self._hits = hits or {}

    def list(self):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])  # no All Mail -> stay on INBOX

    def select(self, mailbox, readonly=False):
        self._odys_sel = (mailbox, readonly)
        return ("OK", [b"1"])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            lit = self.literal
            self.literal = None  # imaplib consumes the literal each command
            self.searches.append((args, lit))
            if lit is not None:  # CHARSET UTF-8 literal path -> ("CHARSET","UTF-8",FIELD)
                return ("OK", [b" ".join(self._hits.get(args[-1], []))])
            return ("OK", [b" ".join(self._hits.get("ASCII", []))])
        return ("OK", [None])


def _endpoint(router, path, method):
    for r in router.routes:
        if r.path == path and method in getattr(r, "methods", set()):
            return r.endpoint
    raise AssertionError(f"route not found: {method} {path}")


def test_ascii_query_uses_single_inline_search():
    conn = _FakeConn({"ASCII": [b"5", b"1"]})

    out = email_routes._imap_uid_search_query(conn, "invoice")

    assert out == [b"5", b"1"]
    assert len(conn.searches) == 1, "ASCII query must be one round-trip"
    args, lit = conn.searches[0]
    assert lit is None, "ASCII query must not use a literal"
    assert args[0] is None  # UID SEARCH <criteria>
    assert "invoice" in args[1]
    assert all(f in args[1] for f in ("FROM", "SUBJECT", "TEXT"))


def test_non_ascii_query_uses_charset_literals_and_unions():
    conn = _FakeConn({"FROM": [b"3", b"7"], "SUBJECT": [b"7", b"2"], "TEXT": [b"9"]})

    out = email_routes._imap_uid_search_query(conn, "árvíz")

    # OR across the three fields == union, deduped, ascending UID order.
    assert out == [b"2", b"3", b"7", b"9"]
    assert [args for (args, _) in conn.searches] == [
        ("CHARSET", "UTF-8", "FROM"),
        ("CHARSET", "UTF-8", "SUBJECT"),
        ("CHARSET", "UTF-8", "TEXT"),
    ]
    # Every command carried the term as a UTF-8 literal — never inline octets.
    for _, lit in conn.searches:
        assert lit == "árvíz".encode("utf-8")


def test_non_ascii_query_with_no_hits_returns_empty():
    conn = _FakeConn({})  # every field empty

    out = email_routes._imap_uid_search_query(conn, "köszönöm")

    assert out == []
    assert len(conn.searches) == 3  # still probes all three fields


def test_search_endpoint_non_ascii_does_not_error(monkeypatch):
    conn = _FakeConn({})  # no hits -> handler short-circuits, searches still recorded

    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    search = _endpoint(router, "/api/email/search", "GET")

    result = search(q="árvíz", folder="INBOX", limit=50, account_id=None, owner="")

    assert "error" not in result
    assert result["emails"] == []
    assert conn.searches, "endpoint issued no SEARCH"
    for args, lit in conn.searches:
        assert args[:2] == ("CHARSET", "UTF-8")
        assert lit == "árvíz".encode("utf-8")


def test_search_endpoint_ascii_query_still_works(monkeypatch):
    conn = _FakeConn({})

    @contextmanager
    def fake_imap(account_id=None, owner=""):
        yield conn

    monkeypatch.setattr(email_routes, "_imap", fake_imap)
    router = email_routes.setup_email_routes()
    search = _endpoint(router, "/api/email/search", "GET")

    result = search(q="invoice", folder="INBOX", limit=50, account_id=None, owner="")

    assert "error" not in result
    assert len(conn.searches) == 1
    args, lit = conn.searches[0]
    assert lit is None
    assert "invoice" in args[1]
