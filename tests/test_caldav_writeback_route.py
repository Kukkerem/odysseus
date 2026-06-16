"""Issue #800 — the calendar write handlers actually trigger CalDAV write-back.

Route-level: proves POST/DELETE /api/calendar/events fire writeback_event for a
CalDAV-backed calendar and not for a local one.

Calls the async route handlers DIRECTLY (extracted from the router) rather than
through Starlette's TestClient — the TestClient middleware-app + threadpool could
hang in some environments; a direct call with a minimal fake request keeps the
same coverage and completes reliably.
"""

import tempfile
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.calendar_routes as croutes
import src.caldav_sync as csync
from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent
from datetime import datetime
from fastapi import HTTPException
from routes.calendar_routes import EventCreate, EventUpdate

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
croutes.SessionLocal = _TS


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    async def _fake_create(owner, uid):
        recorded.append({"uid": uid, "delete": False, "action": "create"})
        return {"ok": True}

    async def _fake_delete(owner, uid):
        recorded.append({"uid": uid, "delete": True, "action": "delete"})
        return {"ok": True}

    monkeypatch.setattr(csync, "push_event_create", _fake_create)
    monkeypatch.setattr(csync, "push_event_delete", _fake_delete)
    return recorded


def _req():
    return SimpleNamespace(state=SimpleNamespace(current_user="tester"))


def _endpoint(method, suffix):
    router = croutes.setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", "").endswith(suffix) and method in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError(f"{method} *{suffix} not found")


def _make_cal(source):
    cid = ("caldav-" if source == "caldav" else "loc-") + uuid.uuid4().hex[:10]
    db = _TS()
    try:
        db.add(CalendarCal(id=cid, owner="tester", name="C", source=source))
        db.commit()
        return cid
    finally:
        db.close()


async def test_create_on_caldav_calendar_pushes_to_remote(calls):
    create_event = _endpoint("POST", "/events")
    cal_id = _make_cal("caldav")
    res = await create_event(_req(), EventCreate(
        summary="Dentist", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    assert res["ok"] is True
    assert len(calls) == 1
    assert calls[0]["delete"] is False


async def test_create_on_local_calendar_does_not_push(calls):
    create_event = _endpoint("POST", "/events")
    cal_id = _make_cal("local")
    res = await create_event(_req(), EventCreate(
        summary="Local", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    assert res["ok"] is True
    assert calls == []


async def test_delete_on_caldav_calendar_pushes_delete(calls):
    create_event = _endpoint("POST", "/events")
    delete_event = _endpoint("DELETE", "/events/{uid}")
    cal_id = _make_cal("caldav")
    res = await create_event(_req(), EventCreate(
        summary="Temp", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    uid = res["uid"]
    calls.clear()
    rd = await delete_event(_req(), uid)
    assert rd["ok"] is True
    assert len(calls) == 1 and calls[0]["delete"] is True and calls[0]["uid"] == uid


# ── Read-only calendar guards ───────────────────────────────────────────
# Moving/editing an event in a read-only calendar used to mutate the local
# row + set caldav_sync_pending; the skipped write-back then left it diverged
# forever (the server-authoritative pull refuses to overwrite pending rows).
# The write endpoints must reject the mutation at the source with a 403.
@pytest.fixture
def read_only_acc(monkeypatch):
    monkeypatch.setattr(csync, "_load_caldav_accounts",
                        lambda owner: [{"id": "acc-ro", "read_only": True}])


def _make_ro_cal():
    cid = "caldav-ro-" + uuid.uuid4().hex[:10]
    db = _TS()
    try:
        db.add(CalendarCal(id=cid, owner="tester", name="RO", source="caldav",
                           account_id="acc-ro"))
        db.commit()
        return cid
    finally:
        db.close()


def _seed_event(cal_id, uid, summary="Orig"):
    db = _TS()
    try:
        db.add(CalendarEvent(uid=uid, calendar_id=cal_id, summary=summary,
                             dtstart=datetime(2026, 6, 10, 14, 0, 0),
                             dtend=datetime(2026, 6, 10, 15, 0, 0),
                             origin="caldav",
                             remote_href="https://dav.example.com/x.ics"))
        db.commit()
    finally:
        db.close()


async def test_create_on_read_only_calendar_is_rejected(calls, read_only_acc):
    create_event = _endpoint("POST", "/events")
    cal_id = _make_ro_cal()
    with pytest.raises(HTTPException) as exc:
        await create_event(_req(), EventCreate(
            summary="blocked", dtstart="2026-06-10T14:00:00Z", calendar_href=cal_id))
    assert exc.value.status_code == 403
    assert calls == []  # rejected before any write-back push


async def test_update_on_read_only_calendar_rejected_and_unchanged(read_only_acc):
    update_event = _endpoint("PUT", "/events/{uid}")
    cal_id = _make_ro_cal()
    uid = "ro-upd-" + uuid.uuid4().hex[:6]
    _seed_event(cal_id, uid, summary="Orig")
    with pytest.raises(HTTPException) as exc:
        await update_event(_req(), uid,
                           EventUpdate(summary="Moved", dtstart="2026-06-20T14:00:00Z"))
    assert exc.value.status_code == 403
    db = _TS()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        assert ev.summary == "Orig"            # row never mutated
        assert ev.caldav_sync_pending is None  # no stuck pending flag
    finally:
        db.close()


async def test_delete_on_read_only_calendar_rejected_and_kept(read_only_acc):
    delete_event = _endpoint("DELETE", "/events/{uid}")
    cal_id = _make_ro_cal()
    uid = "ro-del-" + uuid.uuid4().hex[:6]
    _seed_event(cal_id, uid)
    with pytest.raises(HTTPException) as exc:
        await delete_event(_req(), uid)
    assert exc.value.status_code == 403
    db = _TS()
    try:
        assert db.query(CalendarEvent).filter(
            CalendarEvent.uid == uid).first() is not None
        assert db.query(CalendarDeletedEvent).filter(
            CalendarDeletedEvent.uid == uid).first() is None  # no tombstone
    finally:
        db.close()
