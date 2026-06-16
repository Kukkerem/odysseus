"""Google Calendar over CalDAV must surface events, not come back empty (#2507).

Google's CalDAV principal lives at ``.../caldav/v2/<id>/user`` but events are
served from ``.../caldav/v2/<id>/events``. When the `caldav` library's
principal discovery yields no calendars for Google's ``/user`` endpoint,
``_sync_blocking`` fell back to ``client.calendar(url=url)`` — i.e. it queried
the principal URL itself, which returns a clean but empty 200 for every date
range. Auth succeeded, the calendar stayed empty.

These tests inject a fake ``caldav`` module that mimics Google's behaviour
(principal discovery returns no calendars; the ``/user`` collection holds no
events; the ``/events`` collection holds one VEVENT) and assert the sync now
maps the principal URL to its events collection and pulls the event. No live
Google account is required.
"""
import sys
import tempfile
import types
from datetime import datetime, timedelta

import pytest
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

_GOOGLE_PRINCIPAL = "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/user"
_GOOGLE_EVENTS = "https://apidata.googleusercontent.com/caldav/v2/me@gmail.com/events"


def _ics_one_event():
    # An event inside the sync window (now-90d .. now+365d).
    dt = datetime.utcnow() + timedelta(days=2)
    stamp = dt.strftime("%Y%m%dT%H%M%SZ")
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-1@google\r\n"
        f"DTSTART:{stamp}\r\n"
        f"DTEND:{stamp}\r\n"
        "SUMMARY:Standup\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


class _FakeObj:
    def __init__(self, data):
        self.data = data


class _FakeCalendar:
    def __init__(self, url):
        self.url = url
        self.name = "Primary"

    def date_search(self, start, end, expand=False):
        # Google's /user principal holds no events; the /events collection does.
        if str(self.url).rstrip("/").endswith("/events"):
            return [_FakeObj(_ics_one_event())]
        return []


class _FakePrincipal:
    def calendars(self):
        # Simulate Google's /user endpoint yielding no calendars from discovery.
        return []


class _FakeClient:
    def __init__(self, url=None, username=None, password=None):
        self.url = url
        self.headers = {}
        # Mirror the real DAVClient: _build_dav_client sets
        # session.max_redirects = 0 right after construction.
        self.session = types.SimpleNamespace(max_redirects=30)

    def principal(self):
        return _FakePrincipal()

    def calendar(self, url=None):
        return _FakeCalendar(url)

    def request(self, url, method="GET", body="", headers=None):
        # OAuth Google sync issues a calendar-query REPORT and expects a 207
        # multistatus with calendar-data inline (see _google_report_events).
        # A non-/events target gets Google's empty-window "404 No events found."
        if method == "REPORT" and str(url).rstrip("/").endswith("/events"):
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<D:multistatus xmlns:D="DAV:" xmlns:caldav="urn:ietf:params:xml:ns:caldav">'
                '<D:response><D:href>/caldav/v2/me%40gmail.com/events/x</D:href>'
                '<D:propstat><D:status>HTTP/1.1 200 OK</D:status><D:prop>'
                '<D:getetag>"1"</D:getetag>'
                '<caldav:calendar-data>' + _ics_one_event() + '</caldav:calendar-data>'
                '</D:prop></D:propstat></D:response></D:multistatus>'
            )
            return types.SimpleNamespace(status=207, raw=xml)
        return types.SimpleNamespace(status=404, raw="No events found.")


def _install_fake_caldav(monkeypatch):
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


def _clear_db():
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.commit()
    finally:
        db.close()


def test_maps_google_principal_url_to_events_collection():
    assert caldav_sync._google_caldav_events_url(_GOOGLE_PRINCIPAL) == _GOOGLE_EVENTS
    # Trailing slash tolerated.
    assert caldav_sync._google_caldav_events_url(_GOOGLE_PRINCIPAL + "/") == _GOOGLE_EVENTS
    # Non-Google or non-principal URLs are left untouched (None => caller keeps URL).
    assert caldav_sync._google_caldav_events_url("https://calendar.example.com/dav") is None
    assert caldav_sync._google_caldav_events_url(_GOOGLE_EVENTS) is None


def test_maps_legacy_google_calendar_dav_url():
    # Google's older endpoint (some accounts authenticate only against this one).
    legacy_user = "https://www.google.com/calendar/dav/me@gmail.com/user"
    legacy_events = "https://www.google.com/calendar/dav/me@gmail.com/events"
    assert caldav_sync._google_caldav_events_url(legacy_user) == legacy_events
    assert caldav_sync._google_caldav_events_url(legacy_user + "/") == legacy_events
    # A non-CalDAV www.google.com /user path must NOT be rewritten.
    assert caldav_sync._google_caldav_events_url("https://www.google.com/accounts/user") is None


def test_google_sync_pulls_events_instead_of_empty(monkeypatch):
    _install_fake_caldav(monkeypatch)
    _clear_db()

    result = caldav_sync._sync_blocking("alice", _GOOGLE_PRINCIPAL, "me@gmail.com", "app-pw")

    # The fix routes discovery-less Google sync to the /events collection, so
    # the VEVENT is pulled. Pre-fix this queried /user and returned 0 events.
    assert result["events"] == 1, result
    assert not result["errors"], result["errors"]

    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == "evt-1@google").first()
        assert ev is not None and ev.summary == "Standup"
    finally:
        db.close()


def test_google_report_events_parses_inline_calendar_data():
    from icalendar import Calendar as iCal
    client = _FakeClient()
    start = datetime.utcnow() - timedelta(days=1)
    end = datetime.utcnow() + timedelta(days=30)
    objs = caldav_sync._google_report_events(client, _GOOGLE_EVENTS, start, end)
    assert len(objs) == 1
    ical = iCal.from_ical(objs[0].data)
    assert any(c.name == "VEVENT" and str(c.get("summary")) == "Standup"
               for c in ical.walk())


def test_google_report_events_empty_window_returns_empty():
    # Google answers an empty window with a plain-text "404 No events found.";
    # that must be zero events, never an error. The fake returns 404 for any
    # non-/events target.
    client = _FakeClient()
    objs = caldav_sync._google_report_events(
        client, _GOOGLE_PRINCIPAL, datetime.utcnow(),
        datetime.utcnow() + timedelta(days=1))
    assert objs == []


def test_oauth_google_sync_uses_report_and_pulls_events(monkeypatch):
    # OAuth Google sync must bypass the caldav library's broken search() and
    # pull events through the direct REPORT path.
    _install_fake_caldav(monkeypatch)
    _clear_db()

    result = caldav_sync._sync_blocking(
        "alice", _GOOGLE_PRINCIPAL, "me@gmail.com", "",
        account_id="acc-o", access_token="ya29.live")

    assert result["events"] == 1, result
    assert not result["errors"], result["errors"]

    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == "evt-1@google").first()
        assert ev is not None and ev.summary == "Standup"
    finally:
        db.close()
