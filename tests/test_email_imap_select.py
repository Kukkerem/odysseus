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
