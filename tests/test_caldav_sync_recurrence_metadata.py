"""Issue #3762 — CalDAV sync destroys recurrence metadata, so complex
recurring events disappear from the calendar.

Two ingestion gaps in src/caldav_sync.py:

1. A RECURRENCE-ID override VEVENT shares its uid with the series master,
   and the sync loop looked rows up by uid alone — whichever component
   walked last overwrote the other's row. An override (no RRULE) replaced
   the master's dtstart and blanked rrule, silently collapsing the whole
   weekly series to a single occurrence.
2. EXDATE was never read, so excluded occurrences could not be honored.

These tests drive the real ``_sync_blocking`` against a fake DAV client
(monkeypatched ``_build_dav_client``) and a temp SQLite database, feeding
the reporter-shaped ICS (TZID + EXDATE master plus a RECURRENCE-ID
override). They pin: the master keeps its RRULE with EXDATE lines
appended, the override becomes its own standalone row whose original slot
is suppressed, component order does not matter, and ``_expand_rrule``
honors the stored exclusions. Fails on the pre-fix sync, passes with it.
"""
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarCal, CalendarEvent

import src.caldav_sync as cs

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


_MASTER = """BEGIN:VEVENT
UID:rrule-3762@example.com
DTSTART;TZID=Europe/Oslo:20260604T200000
DTEND;TZID=Europe/Oslo:20260604T210000
EXDATE;TZID=Europe/Oslo:20260618T200000
RRULE:FREQ=WEEKLY
SUMMARY:Weekly invite
END:VEVENT"""

_OVERRIDE = """BEGIN:VEVENT
UID:rrule-3762@example.com
RECURRENCE-ID;TZID=Europe/Oslo:20260611T200000
DTSTART;TZID=Europe/Oslo:20260611T210000
DTEND;TZID=Europe/Oslo:20260611T220000
SUMMARY:Weekly invite (moved 1h)
END:VEVENT"""


def _ics(*vevents: str) -> str:
    return "BEGIN:VCALENDAR\nVERSION:2.0\n" + "\n".join(vevents) + "\nEND:VCALENDAR\n"


class _FakeObj:
    def __init__(self, data: str):
        self.data = data


class _FakeRemoteCal:
    url = "https://caldav.example.test/u/cal1/"
    name = "cal1"

    def __init__(self, objs):
        self._objs = objs

    def date_search(self, start, end, expand=False):
        return self._objs


class _FakePrincipal:
    def __init__(self, cals):
        self._cals = cals

    def calendars(self):
        return self._cals


class _FakeClient:
    def __init__(self, cals):
        self._cals = cals

    def principal(self):
        return _FakePrincipal(self._cals)

    def close(self):
        self.closed = True


def _patch_client(monkeypatch, fake):
    # The fork's _build_dav_client takes a 4th access_token argument
    # (OAuth path); accept and ignore it so the fake stands in cleanly.
    monkeypatch.setattr(cs, "_build_dav_client", lambda url, u, p, t=None: fake)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)


def _run_sync(monkeypatch, ics_text: str) -> dict:
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.commit()
    finally:
        db.close()

    fake = _FakeClient([_FakeRemoteCal([_FakeObj(ics_text)])])
    _patch_client(monkeypatch, fake)
    result = cs._sync_blocking("alice", "https://caldav.example.test/", "u", "p")
    assert not result["errors"], result
    return result


def _rows():
    db = _TS()
    try:
        return db.query(CalendarEvent).order_by(CalendarEvent.dtstart).all()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. The series master must survive an override component: keep its dtstart
#    and its RRULE (with the EXDATE + override slot appended as exclusions).
# ---------------------------------------------------------------------------
def test_master_survives_override_component(monkeypatch):
    _run_sync(monkeypatch, _ics(_MASTER, _OVERRIDE))
    rows = _rows()
    master = [r for r in rows if r.uid == "rrule-3762@example.com"]
    assert master, f"series master row is gone: {[(r.uid, r.rrule) for r in rows]}"
    m = master[0]
    assert m.rrule.startswith("FREQ=WEEKLY"), m.rrule
    assert m.dtstart == datetime(2026, 6, 4, 18, 0)  # 20:00 Oslo = 18:00 UTC
    # Own EXDATE (18/6) and the override's original slot (11/6) are excluded.
    assert "EXDATE:20260618T180000" in m.rrule
    assert "EXDATE:20260611T180000" in m.rrule


def test_override_becomes_standalone_row(monkeypatch):
    _run_sync(monkeypatch, _ics(_MASTER, _OVERRIDE))
    rows = _rows()
    moved = [r for r in rows if "moved 1h" in (r.summary or "")]
    assert len(moved) == 1, [(r.uid, r.summary) for r in rows]
    o = moved[0]
    assert o.uid != "rrule-3762@example.com"
    assert "::" not in o.uid, "compound :: uids are reserved for expanded occurrences"
    assert o.rrule == ""
    assert o.dtstart == datetime(2026, 6, 11, 19, 0)  # 21:00 Oslo = 19:00 UTC


def test_component_order_does_not_matter(monkeypatch):
    _run_sync(monkeypatch, _ics(_OVERRIDE, _MASTER))  # override walks first
    rows = _rows()
    master = [r for r in rows if r.uid == "rrule-3762@example.com"]
    assert master and master[0].rrule.startswith("FREQ=WEEKLY")
    assert len(rows) == 2


def test_plain_series_without_extras_is_unchanged(monkeypatch):
    plain = """BEGIN:VEVENT
UID:plain@example.com
DTSTART;VALUE=DATE:20260601
DTEND;VALUE=DATE:20260602
RRULE:FREQ=WEEKLY
SUMMARY:All-day weekly
END:VEVENT"""
    _run_sync(monkeypatch, _ics(plain))
    rows = _rows()
    assert len(rows) == 1
    assert rows[0].rrule == "FREQ=WEEKLY"
    assert rows[0].all_day is True


def test_resync_updates_rows_instead_of_duplicating(monkeypatch):
    _run_sync(monkeypatch, _ics(_MASTER, _OVERRIDE))
    # Second sync over the same server state must keep exactly 2 rows.
    fake = _FakeClient([_FakeRemoteCal([_FakeObj(_ics(_MASTER, _OVERRIDE))])])
    _patch_client(monkeypatch, fake)
    result = cs._sync_blocking("alice", "https://caldav.example.test/", "u", "p")
    assert not result["errors"], result
    assert len(_rows()) == 2


# ---------------------------------------------------------------------------
# 2. Expansion honors the stored block: rrulestr parses "FREQ=...\nEXDATE:..."
#    into an exclusion-aware set, so the excluded and overridden slots are
#    not expanded while the rest of the series is.
# ---------------------------------------------------------------------------
def test_expand_rrule_honors_exdate_lines(monkeypatch):
    _run_sync(monkeypatch, _ics(_MASTER, _OVERRIDE))

    from routes.calendar_routes import _expand_rrule

    db = _TS()
    try:
        master = db.query(CalendarEvent).filter(
            CalendarEvent.uid == "rrule-3762@example.com"
        ).one()
        occs = _expand_rrule(master, datetime(2026, 6, 1), datetime(2026, 7, 13))
    finally:
        db.close()
    starts = sorted(d["dtstart"] for d in occs)
    assert "2026-06-04T18:00:00Z" in starts          # base occurrence
    assert "2026-06-25T18:00:00Z" in starts          # series continues
    assert not any(s.startswith("2026-06-18") for s in starts), starts  # EXDATE
    assert not any(s.startswith("2026-06-11") for s in starts), starts  # override slot


# ---------------------------------------------------------------------------
# 3. Write-back splits the stored block back into RRULE + EXDATE properties
#    instead of dropping recurrence on the unparseable combined string.
# ---------------------------------------------------------------------------
def test_writeback_splits_rrule_block_into_properties():
    from src.caldav_writeback import build_event_ical

    ics = build_event_ical({
        "uid": "rrule-3762@example.com",
        "summary": "Weekly invite",
        "description": "",
        "location": "",
        "dtstart": datetime(2026, 6, 4, 18, 0),
        "dtend": datetime(2026, 6, 4, 19, 0),
        "all_day": False,
        "is_utc": True,
        "rrule": "FREQ=WEEKLY\nEXDATE:20260618T180000\nEXDATE:20260611T180000",
    })
    assert "RRULE:FREQ=WEEKLY" in ics
    assert "EXDATE:20260618T180000Z" in ics
    assert "EXDATE:20260611T180000Z" in ics
