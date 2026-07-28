# tests/test_email_pool_locking.py
"""The IMAP pool bounds concurrent checkouts per (account_id, owner) and releases
its slot on every path — including a failed connect — so threaded handlers can't
deadlock or leak connections.

The cap is a BoundedSemaphore of `_IMAP_POOL_MAX_PER_KEY`, not a mutex: a
background scan reading many messages must not serialize interactive reads behind
one shared connection. So the (N+1)-th concurrent checkout is what blocks.
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


def _pool_with_cap():
    router = email_routes.setup_email_routes()
    p = router._email_pool
    return p["connect"], p["release"], p["max_per_key"]


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


def test_same_key_blocks_only_once_cap_is_reached(monkeypatch):
    monkeypatch.setattr(email_routes, "_imap_connect", lambda account_id, owner="": _FakeConn())
    connect, release, cap = _pool_with_cap()

    # Up to `cap` concurrent checkouts must NOT block — that is the whole point
    # of using a semaphore rather than a single per-key mutex.
    held = [connect("acct", owner="bob")[0] for _ in range(cap)]

    extra_started = threading.Event()
    extra_got = threading.Event()

    def worker():
        extra_started.set()
        c, _ = connect("acct", owner="bob")
        extra_got.set()
        release("acct", c, ok=True, owner="bob")

    t = threading.Thread(target=worker)
    t.start()
    assert extra_started.wait(1.0)
    assert not extra_got.wait(0.3), f"checkout {cap + 1} should block while {cap} are held"
    release("acct", held.pop(), ok=True, owner="bob")  # free one slot
    assert extra_got.wait(2.0), "blocked checkout should proceed after a release"
    t.join(timeout=2.0)
    for c in held:
        release("acct", c, ok=True, owner="bob")
