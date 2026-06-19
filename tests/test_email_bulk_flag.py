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
