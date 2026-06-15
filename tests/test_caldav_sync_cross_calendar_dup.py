"""CalDAV sync must not blank a whole calendar over a shared VEVENT uid.

CalendarEvent.uid is the global primary key, but the same VEVENT uid legitimately
appears in more than one calendar: a Holidays subscription added under several
Google accounts, a calendar shared between two accounts, or a meeting cross-
invited between them. _find_existing_event is scoped to the calendar being synced
(so it never hijacks another calendar's row), so a shared uid reaches the insert
path as "new". Inserting it violates the PK; that IntegrityError is caught only
per-calendar and rolls back the WHOLE calendar's batch — silently blanking every
event in it (the origoss work calendar and "Közös naptár" showed nothing).

_uid_taken_by_other_calendar lets the sync skip the duplicate instead, so the
calendar keeps its non-colliding events; the shared event stays visible under
whichever calendar synced it first.
"""
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarEvent, CalendarCal
from src.caldav_sync import _uid_taken_by_other_calendar, _find_existing_event

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


def _mk(uid, cal):
    return CalendarEvent(
        uid=uid, calendar_id=cal, summary=uid,
        dtstart=datetime(2026, 6, 4, 9, 0), dtend=datetime(2026, 6, 4, 10, 0),
        origin="caldav",
    )


def _seed():
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.add(CalendarCal(id="calA", owner="admin", name="Holidays A", source="caldav"))
        db.add(CalendarCal(id="calB", owner="admin", name="Közös naptár", source="caldav"))
        # calA synced first and owns the shared holiday uid.
        db.add(_mk("shared@google.com", "calA"))
        db.commit()
    finally:
        db.close()


def _sync_calB(db, uids):
    """Replicate the post-fix per-event insert decision from _sync_blocking."""
    pending = {}
    skipped = 0
    for uid in uids:
        if _find_existing_event(db, pending, uid, "calB"):
            continue
        if _uid_taken_by_other_calendar(db, uid):
            skipped += 1
            continue
        ev = _mk(uid, "calB")
        db.add(ev)
        pending[uid] = ev
    db.commit()
    return skipped


def test_naive_batch_insert_blanks_whole_calendar():
    """Pre-fix reproduction: one shared uid aborts the whole per-calendar commit."""
    _seed()
    db = _TS()
    try:
        db.add(_mk("shared@google.com", "calB"))   # dup of calA's uid
        db.add(_mk("unique-B@google.com", "calB"))  # calB's own event
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # Whole calB batch lost — exactly the reported symptom.
        assert db.query(CalendarEvent).filter_by(calendar_id="calB").count() == 0
    finally:
        db.close()


def test_skip_dup_keeps_calendars_unique_events():
    _seed()
    db = _TS()
    try:
        skipped = _sync_calB(db, ["shared@google.com", "unique-B@google.com"])
        assert skipped == 1
        # calB keeps its own event...
        assert db.query(CalendarEvent).filter_by(
            calendar_id="calB", uid="unique-B@google.com").first() is not None
        # ...and the shared uid stays under calA (synced first), not hijacked.
        shared = db.query(CalendarEvent).filter_by(uid="shared@google.com").first()
        assert shared.calendar_id == "calA"
        # calB is no longer blank.
        assert db.query(CalendarEvent).filter_by(calendar_id="calB").count() == 1
    finally:
        db.close()


def test_helper_detects_cross_calendar_uid():
    _seed()
    db = _TS()
    try:
        assert _uid_taken_by_other_calendar(db, "shared@google.com") is True
        assert _uid_taken_by_other_calendar(db, "brand-new@google.com") is False
    finally:
        db.close()
