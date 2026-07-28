"""Read-only CalDAV calendars are remote-authoritative on pull.

A read-only calendar can never push a local edit, so the normal pull rule
("don't overwrite a row with an un-pushed pending change") would strand any
local divergence forever — this is the bug behind "I moved an event in a
read-only calendar and it vanished / I can't reset it". For read-only accounts
the pull must instead overwrite the diverged row from the server and clear the
pending flag, healing it on the next sync. Writable accounts keep the old
protective behaviour.

No live server is required: a fake `caldav` module yields one VEVENT via
``date_search`` and we drive ``_sync_blocking`` directly.
"""
import sys
import tempfile
import types
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarCal, CalendarEvent
from src import caldav_sync

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

_URL = "https://dav.example.com/cal/"
_UID = "ro-heal-1"


def _ics_remote():
    # Event inside the sync window (now-90d .. now+365d), carrying the real
    # server-side summary + time the local row diverged away from.
    dt = datetime.utcnow() + timedelta(days=2)
    stamp = dt.strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{_UID}\r\n"
        f"DTSTART:{stamp}\r\n"
        f"DTEND:{stamp}\r\n"
        "SUMMARY:Remote Standup\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


class _FakeObj:
    def __init__(self, data):
        self.data = data

    @property
    def url(self):
        return _URL + "evt.ics"


class _FakeCalendar:
    def __init__(self, url):
        self.url = url
        self.name = "Primary"

    def date_search(self, start, end, expand=False):
        return [_FakeObj(_ics_remote())]


class _FakePrincipal:
    def calendars(self):
        return [_FakeCalendar(_URL)]


class _FakeClient:
    def __init__(self, url=None, username=None, password=None):
        self.url = url
        self.headers = {}
        self.session = types.SimpleNamespace(max_redirects=30)

    def principal(self):
        return _FakePrincipal()

    def close(self):
        # _sync_blocking closes the client in its finally block.
        pass

    def calendar(self, url=None):
        return _FakeCalendar(url)


def _install(monkeypatch):
    fake = types.ModuleType("caldav")
    fake.DAVClient = _FakeClient
    err = types.ModuleType("caldav.lib.error")

    class AuthorizationError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    err.AuthorizationError = AuthorizationError
    err.NotFoundError = NotFoundError
    lib = types.ModuleType("caldav.lib")
    lib.error = err
    fake.lib = lib
    monkeypatch.setitem(sys.modules, "caldav", fake)
    monkeypatch.setitem(sys.modules, "caldav.lib", lib)
    monkeypatch.setitem(sys.modules, "caldav.lib.error", err)
    monkeypatch.setattr(caldav_sync, "SessionLocal", _TS, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS, raising=False)


def _seed(owner, account_id):
    """A local CalDAV row an edit diverged: wrong time + summary, pending=update."""
    cal_id = caldav_sync._stable_cal_id(_URL, owner=owner, account_id=account_id)
    db = _TS()
    try:
        db.query(CalendarEvent).filter(CalendarEvent.uid == _UID).delete()
        db.query(CalendarCal).filter(CalendarCal.id == cal_id).delete()
        db.add(CalendarCal(id=cal_id, owner=owner, name="Primary", source="caldav",
                           account_id=account_id, caldav_base_url=_URL))
        db.add(CalendarEvent(
            uid=_UID, calendar_id=cal_id, summary="Local Moved",
            dtstart=datetime.utcnow() + timedelta(days=20),
            dtend=datetime.utcnow() + timedelta(days=20),
            origin="caldav", remote_href=_URL + "evt.ics",
            caldav_sync_pending="update"))
        db.commit()
    finally:
        db.close()
    return cal_id


def test_read_only_pull_heals_diverged_event(monkeypatch):
    _install(monkeypatch)
    owner = "ro-owner"
    _seed(owner, "acc-ro")

    res = caldav_sync._sync_blocking(owner, _URL, "u", "pw",
                                     account_id="acc-ro", read_only=True)
    assert not res["errors"], res["errors"]

    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == _UID).first()
        assert ev is not None
        assert ev.summary == "Remote Standup"     # overwritten from the server
        assert ev.caldav_sync_pending is None      # diverged flag cleared
    finally:
        db.close()


def test_writable_pull_preserves_pending_edit(monkeypatch):
    """Control: a writable account must keep protecting an un-pushed local edit."""
    _install(monkeypatch)
    owner = "rw-owner"
    _seed(owner, "acc-rw")

    res = caldav_sync._sync_blocking(owner, _URL, "u", "pw",
                                     account_id="acc-rw", read_only=False)
    assert not res["errors"], res["errors"]

    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == _UID).first()
        assert ev is not None
        assert ev.summary == "Local Moved"            # pending edit untouched
        assert ev.caldav_sync_pending == "update"
    finally:
        db.close()
