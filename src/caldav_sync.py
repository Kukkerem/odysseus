"""CalDAV → local SQLite sync.

The Settings UI lets users save CalDAV credentials, but the original
sync path was removed when calendar storage was migrated to SQLite.
This module re-wires that gap as a one-way pull (remote → local),
called on calendar open and from a periodic scheduler loop.

Design notes:
- We use the `caldav` lib so PROPFIND discovery + REPORT XML work
  across Radicale / Nextcloud / Apple / Fastmail without us
  reinventing the protocol. It's pure Python.
- The lib is synchronous; we run it in a threadpool via
  `asyncio.to_thread` so the FastAPI event loop stays free.
- Each remote calendar maps to one local `CalendarCal` row with
  `source="caldav"` and `id` = a stable hash of the remote URL so
  re-syncs idempotently target the same row.
- Events upsert by VEVENT UID (kept as the local `uid`). Local
  CalDAV-sourced events not seen in the latest pull are deleted so
  remote deletions propagate.
- Datetimes are converted to UTC and the row is flagged `is_utc=True`
  so the serializer adds the Z suffix and the frontend renders in the
  user's local TZ correctly.
"""

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
import uuid
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Pull window: 90 days back, 1 year forward. Keeps the REPORT cheap and
# matches what the calendar UI typically renders. Far-future recurring
# events still come through via RRULE expansion on the frontend.
_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365
_BLOCKED_HOSTS = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "metadata.google.internal",
}


def _private_caldav_allowed() -> bool:
    return os.environ.get("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "0").lower() in {"1", "true", "yes"}


def _validate_caldav_address(addr: ipaddress._BaseAddress) -> None:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        raise ValueError("CalDAV URL host is not allowed")
    if addr.is_private and not _private_caldav_allowed():
        raise ValueError("Private CalDAV IPs require ODYSSEUS_ALLOW_PRIVATE_CALDAV=1")


def _validate_caldav_ip(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    _validate_caldav_address(ip)


def _resolve_caldav_host_ips(host: str) -> list[ipaddress._BaseAddress]:
    addrs: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0].split("%", 1)[0]))
        except ValueError:
            continue
    return addrs


def _validate_caldav_hostname(host: str) -> None:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return
    except ValueError:
        pass
    try:
        addrs = _resolve_caldav_host_ips(host)
    except OSError:
        raise ValueError("CalDAV URL host does not resolve")
    if not addrs:
        raise ValueError("CalDAV URL host does not resolve")
    for addr in addrs:
        _validate_caldav_address(addr)


def validate_caldav_url(raw_url: str) -> str:
    """Validate and normalize a user-provided CalDAV URL before server-side use."""
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if not url:
        raise ValueError("CalDAV URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CalDAV URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("CalDAV URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("Put CalDAV credentials in the username/password fields, not the URL")
    if parsed.fragment:
        raise ValueError("CalDAV URL fragments are not allowed")
    try:
        parsed.port
    except ValueError:
        raise ValueError("CalDAV URL has an invalid port")
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("CalDAV URL host is not allowed")
    _validate_caldav_ip(host)
    _validate_caldav_hostname(host)
    return urlunparse(parsed._replace(fragment="")).rstrip("/")


def _event_etag(obj) -> str:
    """Best-effort ETag extraction from python-caldav resources."""
    try:
        etag = getattr(obj, "etag", None)
        if callable(etag):
            etag = etag()
        return str(etag or "")
    except Exception:
        return ""


def _stable_cal_id(remote_url: str, owner: str = "", account_id: str = "") -> str:
    """Deterministic local id for a remote CalDAV calendar, scoped to owner
    and account so two users — or one user with two accounts — pointing at
    the same server URL get distinct local rows (avoids PK collision, #2765).
    The owner and account_id default to "" for the legacy/URL-only path so
    existing callers without those arguments keep working."""
    key = f"{owner}\n{account_id}\n{remote_url}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"caldav-{h}"


def _to_utc_naive(dt):
    """CalDAV datetimes can be tz-aware (with a TZID) or naive. The DB
    column is naive but we set is_utc=True so the serializer adds Z.
    All-day events stay as date and get widened to datetime here."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None), False
        return dt, False  # naive → treat as local
    # date-only (all-day)
    return datetime(dt.year, dt.month, dt.day), True


def _event_fields_from_component(comp):
    """Extract the CalendarEvent column values from one VEVENT component.

    Returns None when the component has no DTSTART (nothing renderable).
    Shared by the series-master and RECURRENCE-ID-override ingestion paths
    so both store identical time/text semantics.
    """
    dtstart_p = comp.get("dtstart")
    if not dtstart_p:
        return None
    start_dt, all_day = _to_utc_naive(dtstart_p.dt)

    dtend_p = comp.get("dtend")
    if dtend_p:
        end_dt, _ = _to_utc_naive(dtend_p.dt)
    elif all_day:
        end_dt = start_dt + timedelta(days=1)
    else:
        end_dt = start_dt + timedelta(hours=1)

    # is_utc reflects whether the source carried a TZ we converted from.
    # All-day = no TZ semantics.
    row_is_utc = (
        not all_day
        and isinstance(dtstart_p.dt, datetime)
        and dtstart_p.dt.tzinfo is not None
    )
    return {
        "dtstart": start_dt,
        "dtend": end_dt,
        "all_day": all_day,
        "is_utc": row_is_utc,
        "summary": str(comp.get("summary", "")),
        "description": str(comp.get("description", "")),
        "location": str(comp.get("location", "")),
        "rrule": (
            comp.get("rrule").to_ical().decode() if comp.get("rrule") else ""
        ),
    }


def _exdate_stamp(dt) -> str:
    """UTC-naive EXDATE value matching the dtstart storage semantics."""
    moment, day_only = _to_utc_naive(dt)
    return moment.strftime("%Y%m%d") if day_only else moment.strftime("%Y%m%dT%H%M%S")


def _exdate_lines(comp, override_comps) -> list:
    """EXDATE lines to append to a series master's stored rrule (#3762).

    Combines the component's own EXDATE values with the RECURRENCE-ID of
    every override component: overrides are stored as standalone rows, so
    the master must not also expand the original slot. Values use the same
    UTC-naive conversion as dtstart, and the resulting ``RRULE-content +
    EXDATE lines`` block is exactly what dateutil's ``rrulestr`` parses
    into an exclusion-aware ``rruleset`` at expansion time.
    """
    stamps = []
    ex = comp.get("exdate")
    if ex is not None:
        for chunk in (ex if isinstance(ex, list) else [ex]):
            for v in getattr(chunk, "dts", None) or []:
                stamps.append(_exdate_stamp(v.dt))
    for oc in override_comps:
        rid = oc.get("recurrence-id")
        if rid is not None:
            stamps.append(_exdate_stamp(rid.dt))
    lines = []
    for stamp in stamps:
        line = f"EXDATE:{stamp}"
        if line not in lines:
            lines.append(line)
    return lines


def _override_uid(uid_val: str, comp) -> str:
    """Stable row uid for a RECURRENCE-ID override occurrence.

    Deliberately does NOT use the ``{base}::{suffix}`` compound form: that
    form is reserved for expanded occurrences, and _expand_rrule's compound
    uid would strip the suffix and retarget edits/deletes at the series
    master row.
    """
    rid = comp.get("recurrence-id")
    return f"{uid_val}--override--{_exdate_stamp(rid.dt)}"


def _find_existing_event(db, pending, uid_val, calendar_id):
    """Find the event to update for THIS calendar.

    CalendarEvent.uid is the global primary key, so an unscoped lookup by uid
    returns whatever row holds that VEVENT uid — including another owner's.
    The old code then reassigned that row's calendar_id, moving (stealing)
    another user's event into the syncing calendar whenever the two share a
    uid (shared/subscribed/public calendars, or two accounts on one server).
    Scope the lookup to the calendar being synced; a genuine cross-user uid
    collision then fails the PK insert inside the per-calendar try/except
    instead of hijacking the row. (import_ics was already fixed this way.)
    """
    from core.database import CalendarEvent
    return pending.get(uid_val) or db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid_val,
        CalendarEvent.calendar_id == calendar_id,
    ).first()


def _uid_taken_by_other_calendar(db, uid_val) -> bool:
    """True if ``uid_val`` is already stored under some other calendar.

    CalendarEvent.uid is the global primary key, yet a VEVENT uid legitimately
    recurs across calendars: the same Holidays subscription under several Google
    accounts, a calendar shared between two accounts, or a meeting cross-invited
    between them. _find_existing_event is scoped to the calendar being synced (so
    we never hijack another calendar's row), so such a uid reaches the insert
    path as "new" — and inserting it violates the PK. That IntegrityError is
    caught only per-calendar and rolls back the WHOLE calendar's batch, silently
    blanking every event in it. The sync uses this check to skip the duplicate
    instead; the event stays visible under whichever calendar synced it first.
    """
    from core.database import CalendarEvent
    return db.query(CalendarEvent.uid).filter(CalendarEvent.uid == uid_val).first() is not None


def _google_caldav_events_url(url: str) -> str | None:
    """Map a Google CalDAV *principal* URL to its event-collection URL.

    Google serves the principal at ``…/user`` but events live under ``…/events``
    — the ``/user`` resource holds no VEVENTs. The `caldav` library's
    principal→home-set discovery does not reliably enumerate calendars from
    Google's ``/user`` endpoint, so the sync falls into the "treat the URL as a
    single calendar" fallback below. Pointed at ``/user`` that fallback issues
    every calendar-query REPORT against the principal, which returns a clean but
    empty 200 for all date ranges — the calendar shows no events even though
    auth succeeded (issue #2507).

    Both Google CalDAV endpoint forms are handled, since some accounts only
    authenticate against one of them:
      - newer:  ``https://apidata.googleusercontent.com/caldav/v2/<id>/user``
      - legacy: ``https://www.google.com/calendar/dav/<id>/user``

    Returns the events URL for a recognised Google principal URL, else None so
    the caller keeps the original URL unchanged.
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if not path.endswith("/user"):
        return None
    is_google = (
        host.endswith("googleusercontent.com")                       # newer /caldav/v2 form
        or (host in ("www.google.com", "google.com") and "/calendar/dav/" in path)  # legacy form
    )
    if not is_google:
        return None
    new_path = path[: -len("/user")] + "/events"
    return urlunparse(parts._replace(path=new_path))


def _is_google_host(url: str) -> bool:
    """True for either Google CalDAV endpoint form (legacy www.google.com or
    the v2 apidata host)."""
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    return (
        host.endswith("googleusercontent.com")
        or (host in ("www.google.com", "google.com") and "/calendar/dav/" in parts.path)
    )

def _open_url_as_calendar(client, url: str):
    """Open ``url`` as a single calendar collection.

    Used when principal discovery yields no calendars. Google's principal URL
    is not an event collection, so map it to the events URL first
    (see ``_google_caldav_events_url``); other servers' URLs are used as-is.
    """
    target = _google_caldav_events_url(url) or url
    return client.calendar(url=target)


class _RawCalObj:
    """Minimal stand-in for a caldav ``CalendarObjectResource``: the sync loop
    only reads ``.data`` (the raw VCALENDAR text)."""
    __slots__ = ("data",)

    def __init__(self, data: str):
        self.data = data


def _google_report_events(client, events_url: str, start, end) -> list:
    """Fetch events from a Google CalDAV ``/events`` collection via a direct
    calendar-query REPORT.

    The caldav library's ``search``/``date_search`` is unusable against Google:
    caldav 3.2.x lazy-loads every matched object with a per-href GET that Google
    answers ``404 No events found.`` (python-caldav #310/#401, vdirsyncer #1007),
    so the high-level search raises ``NotFoundError`` and the calendar comes back
    empty. A single calendar-query REPORT that requests ``calendar-data`` inline
    returns the whole window in one 207 multistatus, which Google serves
    correctly. Auth (Bearer for OAuth, Basic for legacy) is reused from
    ``client``.

    Returns a list of objects exposing ``.data`` (raw VCALENDAR). An empty window
    — Google replies with a plain-text ``404 No events found.`` — yields an empty
    list, not an error. Non-404 failures raise so the caller records them.
    """
    from lxml import etree
    from caldav.lib.error import NotFoundError

    s = start.strftime("%Y%m%dT%H%M%SZ")
    e = end.strftime("%Y%m%dT%H%M%SZ")
    body = (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        '<D:prop><D:getetag/><C:calendar-data/></D:prop>'
        '<C:filter><C:comp-filter name="VCALENDAR">'
        '<C:comp-filter name="VEVENT">'
        f'<C:time-range start="{s}" end="{e}"/>'
        '</C:comp-filter></C:comp-filter></C:filter>'
        '</C:calendar-query>'
    )
    try:
        resp = client.request(
            events_url, "REPORT", body,
            {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        )
    except NotFoundError:
        return []  # empty window — Google's "No events found." quirk
    if resp.status == 404:
        return []
    if resp.status not in (200, 207):
        raise RuntimeError(f"Google CalDAV REPORT failed: HTTP {resp.status}")
    raw = resp.raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not raw or not raw.lstrip().startswith("<"):
        return []
    root = etree.fromstring(raw.encode("utf-8"))
    ns = {"C": "urn:ietf:params:xml:ns:caldav"}
    return [_RawCalObj(node.text) for node in root.findall(".//C:calendar-data", ns)
            if node.text and node.text.strip()]


def _build_dav_client(url: str, username: str, password: str, access_token: str | None = None):
    """Construct a CalDAV client with automatic redirects disabled.

    Basic auth (username/password) for most servers. When ``access_token`` is
    given (Google OAuth), authenticate with a Bearer token instead: set the
    Authorization header on the caldav client's own header set, which
    ``DAVClient._prepare_request`` merges into every request. This is version-
    robust — unlike ``auth_type=\"bearer\"`` / ``caldav.requests.HTTPBearerAuth``,
    which exist only in caldav 2.x.

    Redirects are pinned to zero (set on the session, created in ``__init__``)
    so a validated public host cannot be redirected, at request time, into
    loopback/private space — the SSRF the host check closes.
    """
    import caldav

    kwargs = {} if access_token else {"username": username, "password": password}
    client = caldav.DAVClient(url=url, **kwargs)
    if access_token:
        client.headers["Authorization"] = f"Bearer {access_token}"
    client.session.max_redirects = 0
    return client


def _should_prune_window(seen_uids: set, parse_failed: bool) -> bool:
    """Whether the post-sync prune of vanished CalDAV events is safe to run.

    The prune deletes local ``origin=="caldav"`` rows in the window whose UID the
    server did not just return. Any parse failure (total or partial) makes
    ``seen_uids`` an incomplete view of the server, so pruning against it can
    delete events that still exist upstream but could not be read: a total
    failure wipes the whole window, a partial failure deletes just the
    unreadable ones. Only prune on a clean read. An empty ``seen_uids`` after a
    clean read is a genuinely empty window, which is safe to prune.
    """
    return not parse_failed


def _sync_blocking(owner: str, url: str, username: str, password: str,
                   account_id: str = "", access_token: str | None = None,
                   read_only: bool = False) -> dict:
    """The actual sync — synchronous, intended to run in a threadpool.
    Returns counts: {calendars, events, deleted, skipped, errors}."""
    # Lazy imports so a missing `caldav` dep doesn't break app startup —
    # the integrations form still works, sync just no-ops with an error.
    from caldav.lib.error import AuthorizationError, NotFoundError
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    result = {"calendars": 0, "events": 0, "deleted": 0, "skipped": 0, "errors": []}

    client = _build_dav_client(url, username, password, access_token)

    # Discovery: try principal → calendars first; if the server doesn't
    # support discovery (or the URL points directly at a calendar), fall
    # back to treating the URL as a single calendar.
    calendars = []
    try:
        principal = client.principal()
        calendars = principal.calendars()
    except (AuthorizationError, NotFoundError) as e:
        if access_token is None and _is_google_host(url):
            result["errors"].append(
                "Google rejected the app password (v2 CalDAV requires OAuth). "
                "Use 'Connect Google Calendar'.")
        else:
            result["errors"].append(f"Discovery failed: {e}")
        return result
    except Exception as e:
        logger.info(f"CalDAV principal discovery failed, trying URL as calendar: {e}")
        try:
            calendars = [_open_url_as_calendar(client, url)]
        except Exception as e2:
            result["errors"].append(f"Could not open URL as calendar: {e2}")
            return result

    if not calendars:
        try:
            calendars = [_open_url_as_calendar(client, url)]
        except Exception as e:
            result["errors"].append(f"No calendars and URL fallback failed: {e}")
            return result

    start = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
    end = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)

    db = SessionLocal()
    try:
        for remote_cal in calendars:
            try:
                remote_url = str(remote_cal.url)
                cal_id = _stable_cal_id(remote_url, owner=owner, account_id=account_id)
                display_name = (remote_cal.name or "").strip() or "CalDAV"

                local_cal = db.query(CalendarCal).filter(
                    CalendarCal.id == cal_id,
                    CalendarCal.owner == owner,
                ).first()
                if not local_cal:
                    local_cal = CalendarCal(
                        id=cal_id,
                        owner=owner,
                        name=display_name,
                        color="#5b8abf",
                        source="caldav",
                        account_id=account_id or None,
                        caldav_base_url=remote_url,
                    )
                    db.add(local_cal)
                    db.commit()
                else:
                    # Refresh display name and stamp CalDAV metadata if missing.
                    changed = False
                    if local_cal.name != display_name:
                        local_cal.name = display_name
                        changed = True
                    if account_id and not local_cal.account_id:
                        local_cal.account_id = account_id
                        changed = True
                    if local_cal.caldav_base_url != remote_url:
                        local_cal.caldav_base_url = remote_url
                        changed = True
                    if changed:
                        db.commit()
                result["calendars"] += 1

                # Fetch events in window. `date_search` returns CalendarObject
                # resources; each may contain one VEVENT (most servers) or
                # several (rare).
                from icalendar import Calendar as iCal

                seen_uids = set()
                # Track events added to the session but not yet committed so
                # duplicate UIDs within the same batch are updated, not re-inserted
                # (which would violate the UNIQUE constraint on commit).
                pending: dict = {}
                parse_failed = False
                try:
                    if access_token and _is_google_host(remote_url):
                        # OAuth Google: caldav 3.2.x search() lazy-loads each
                        # object with a per-href GET that Google 404s ("No events
                        # found."); fetch via a direct REPORT with inline
                        # calendar-data. Basic-auth Google stays on date_search
                        # below so its "needs OAuth" diagnostic still fires.
                        objs = _google_report_events(client, remote_url, start, end)
                    else:
                        objs = remote_cal.date_search(start=start, end=end, expand=False)
                except NotFoundError as e:
                    if access_token is None and _is_google_host(url):
                        result["errors"].append(
                            f"{display_name}: primary calendar rejected basic-auth CalDAV — "
                            "this Google Workspace account needs OAuth. Use 'Connect Google Calendar'.")
                    else:
                        result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue
                except Exception as e:
                    result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue

                for obj in objs:
                    try:
                        ical = iCal.from_ical(obj.data)
                    except Exception as e:
                        result["errors"].append(f"{display_name}: parse failed ({e})")
                        parse_failed = True
                        continue

                    # Group this object's VEVENTs by uid before writing:
                    # a recurring series ships its master plus optional
                    # RECURRENCE-ID override components under ONE uid, and
                    # processing them independently made whichever walked
                    # last overwrite the other's row — an override (no
                    # RRULE) silently collapsed the whole series to a
                    # single occurrence (#3762). Grouping also makes the
                    # reconciliation independent of component order.
                    obj_masters: dict = {}
                    obj_overrides: dict = {}
                    for comp in ical.walk():
                        if comp.name != "VEVENT":
                            continue
                        uid_val = str(comp.get("uid", "")) or str(uuid.uuid4())
                        if comp.get("recurrence-id") is not None:
                            obj_overrides.setdefault(uid_val, []).append(comp)
                        elif uid_val not in obj_masters:
                            obj_masters[uid_val] = comp

                    def _upsert(row_uid: str, fields: dict):
                        existing = _find_existing_event(db, pending, row_uid, local_cal.id)
                        if existing:
                            # Read-only calendars are remote-authoritative: a
                            # local edit can never be pushed, so honouring its
                            # pending flag would strand the divergence forever.
                            # Overwrite from the server and clear the flag.
                            if existing.caldav_sync_pending in {"create", "update"} and not read_only:
                                result["events"] += 1
                                return
                            existing.calendar_id = local_cal.id
                            for key, value in fields.items():
                                setattr(existing, key, value)
                            existing.origin = "caldav"
                            existing.remote_href = str(getattr(obj, "url", "") or "") or None
                            existing.remote_etag = _event_etag(obj) or None
                            existing.caldav_sync_pending = None
                        else:
                            # Same VEVENT uid already stored under another
                            # calendar (shared/subscribed calendar, or one
                            # account that also sees it). uid is the global PK,
                            # so we cannot insert a second copy; doing so raises
                            # IntegrityError on commit, which is caught only
                            # per-calendar and rolls back every event in this
                            # calendar. Skip the duplicate instead.
                            if _uid_taken_by_other_calendar(db, row_uid):
                                result["skipped"] += 1
                                return
                            new_ev = CalendarEvent(
                                uid=row_uid,
                                calendar_id=local_cal.id,
                                origin="caldav",
                                remote_href=str(getattr(obj, "url", "") or "") or None,
                                remote_etag=_event_etag(obj) or None,
                                **fields,
                            )
                            db.add(new_ev)
                            pending[row_uid] = new_ev
                        result["events"] += 1

                    for uid_val, comp in obj_masters.items():
                        fields = _event_fields_from_component(comp)
                        if fields is None:
                            continue
                        seen_uids.add(uid_val)
                        # Persist exclusions next to the RRULE content; the
                        # expansion side (rrulestr) parses the combined block
                        # into an exclusion-aware rruleset.
                        if fields["rrule"]:
                            ex_lines = _exdate_lines(comp, obj_overrides.get(uid_val, []))
                            if ex_lines:
                                fields["rrule"] = "\n".join([fields["rrule"], *ex_lines])
                        _upsert(uid_val, fields)

                    for uid_val, comps in obj_overrides.items():
                        for comp in comps:
                            fields = _event_fields_from_component(comp)
                            if fields is None:
                                continue
                            # An override is a standalone occurrence, never a
                            # series of its own.
                            fields["rrule"] = ""
                            row_uid = _override_uid(uid_val, comp)
                            seen_uids.add(row_uid)
                            # The base uid must survive the prune even when the
                            # master sits outside the sync window.
                            seen_uids.add(uid_val)
                            _upsert(row_uid, fields)
                db.commit()

                # Prune locally-cached CalDAV events that vanished
                # upstream (only within our sync window — events outside
                # the window aren't in `objs`, so we'd false-delete them).
                # Only rows we previously pulled from the server (origin=="caldav")
                # are prunable; locally-created events (agent / email triage / a
                # UI event whose write-back failed) carry origin NULL and must
                # never be deleted just because the server didn't return them.
                # Skip the prune on any parse failure: seen_uids is then an
                # incomplete view of the server, so pruning against it would
                # delete events that still exist upstream but could not be read
                # (the empty-seen_uids case wipes the whole window; a partial
                # failure deletes just the unreadable rows).
                if _should_prune_window(seen_uids, parse_failed):
                    stale = db.query(CalendarEvent).filter(
                        CalendarEvent.calendar_id == local_cal.id,
                        CalendarEvent.origin == "caldav",
                        CalendarEvent.dtstart >= start,
                        CalendarEvent.dtstart <= end,
                        CalendarEvent.remote_href.isnot(None),
                        # Read-only: prune diverged local rows too (incl. ones
                        # an edit moved out of the window) — server wins.
                        *([] if read_only else [CalendarEvent.caldav_sync_pending.is_(None)]),
                        ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
                    ).all()
                    for ev in stale:
                        db.delete(ev)
                    result["deleted"] += len(stale)
                    db.commit()
            except Exception as e:
                logger.exception("CalDAV sync failed for one calendar")
                result["errors"].append(str(e)[:200])
                db.rollback()
    finally:
        db.close()

    return result


def _event_payload(ev) -> dict:
    return {
        "uid": ev.uid,
        "summary": ev.summary,
        "description": ev.description,
        "location": ev.location,
        "dtstart": ev.dtstart,
        "dtend": ev.dtend,
        "all_day": ev.all_day,
        "is_utc": ev.is_utc,
        "rrule": ev.rrule or "",
    }


def _load_event_for_writeback(owner: str, uid: str) -> tuple[str, str, dict] | None:
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        ev = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "caldav":
            return None
        return ev.calendar.source, ev.calendar.id, _event_payload(ev)
    finally:
        db.close()


def _load_delete_for_writeback(owner: str, uid: str) -> tuple[str, str, dict] | None:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        tombstone = db.query(CalendarDeletedEvent).filter(
            CalendarDeletedEvent.uid == uid,
            CalendarDeletedEvent.owner == owner,
        ).first()
        if tombstone:
            return "caldav", tombstone.calendar_id, {"uid": uid}

        ev = (
            db.query(CalendarEvent)
            .join(CalendarCal)
            .filter(CalendarEvent.uid == uid, CalendarCal.owner == owner)
            .first()
        )
        if not ev or not ev.calendar or ev.calendar.source != "caldav":
            return None
        return ev.calendar.source, ev.calendar.id, {"uid": uid}
    finally:
        db.close()


def _pending_writeback_uids(owner: str) -> tuple[list[str], list[str]]:
    from core.database import CalendarCal, CalendarDeletedEvent, CalendarEvent, SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(CalendarEvent.uid)
            .join(CalendarCal)
            .filter(
                CalendarCal.owner == owner,
                CalendarCal.source == "caldav",
                CalendarEvent.status != "cancelled",
                (
                    (CalendarEvent.caldav_sync_pending.isnot(None))
                    | (CalendarEvent.remote_href.is_(None))
                ),
            )
            .all()
        )
        delete_rows = (
            db.query(CalendarDeletedEvent.uid)
            .filter(CalendarDeletedEvent.owner == owner)
            .all()
        )
        return [row[0] for row in rows], [row[0] for row in delete_rows]
    finally:
        db.close()


def _load_caldav_accounts(owner: str) -> list:
    """Return the list of CalDAV accounts for *owner*, auto-migrating the legacy
    single-account ``caldav`` key to the new ``caldav_accounts`` list on first call.

    The save step is best-effort: if ``_save_for_user`` is unavailable (e.g. in a
    test with a minimal prefs mock) the migrated accounts are still returned; the
    next real call will just re-run the cheap migration again.
    """
    import uuid as _uuid
    from routes.prefs_routes import _load_for_user

    prefs = _load_for_user(owner) or {}
    if "caldav_accounts" in prefs:
        return list(prefs["caldav_accounts"] or [])
    # Migrate legacy single-account config to the list format.
    legacy = prefs.get("caldav", {}) or {}
    if legacy.get("url"):
        accounts = [{
            "id": str(_uuid.uuid4()),
            "label": "CalDAV",
            "url": legacy["url"],
            "username": legacy.get("username", ""),
            "password": legacy.get("password", ""),
        }]
        prefs["caldav_accounts"] = accounts
        prefs.pop("caldav", None)
        try:
            from routes.prefs_routes import _save_for_user
            _save_for_user(owner, prefs)
        except (ImportError, AttributeError):
            pass  # best-effort; next call re-migrates from the still-present legacy key
        return accounts
    return []

# Refresh slightly before expiry so an in-flight sync doesn't race the cutoff.
_TOKEN_EXPIRY_SKEW_SECONDS = 60


def _refresh_google_caldav_token(owner: str, account_id: str) -> str | None:
    """Exchange the account's stored refresh token for a new access token and
    persist the encrypted token + expiry back into the owner's prefs entry."""
    from routes.prefs_routes import _load_for_user, _save_for_user
    from src.secret_storage import decrypt as _dec, encrypt as _enc
    from src.google_oauth import exchange_refresh_token, google_client_credentials

    client_id, client_secret = google_client_credentials()
    if not (client_id and client_secret):
        return None
    prefs = _load_for_user(owner) or {}
    accounts = list(prefs.get("caldav_accounts") or [])
    idx = next((i for i, a in enumerate(accounts) if a.get("id") == account_id), None)
    if idx is None:
        return None
    acc = accounts[idx]
    try:
        refresh_token = _dec(acc.get("oauth_refresh_token") or "")
    except Exception:
        refresh_token = ""
    if not refresh_token:
        return None
    try:
        data = exchange_refresh_token(client_id, client_secret, refresh_token)
        access_token = data["access_token"]
    except Exception:
        logger.warning("Google CalDAV token refresh failed for account %s", account_id)
        return None
    acc["oauth_access_token"] = _enc(access_token)
    acc["oauth_token_expiry"] = str(int(time.time()) + data.get("expires_in", 3600))
    accounts[idx] = acc
    prefs["caldav_accounts"] = accounts
    try:
        _save_for_user(owner, prefs)
    except Exception:
        logger.warning("Persisting refreshed CalDAV token failed for account %s", account_id)
    return access_token


def _resolve_google_caldav_token(owner: str, account: dict) -> str | None:
    """Return a valid Google access token for an OAuth CalDAV account,
    refreshing (and persisting) when the cached token is missing or expiring."""
    from src.secret_storage import decrypt as _dec

    try:
        access_token = _dec(account.get("oauth_access_token") or "")
    except Exception:
        access_token = ""
    expiry_raw = account.get("oauth_token_expiry") or ""
    if access_token and expiry_raw:
        try:
            if int(expiry_raw) - _TOKEN_EXPIRY_SKEW_SECONDS > time.time():
                return access_token
        except (ValueError, TypeError):
            pass
    return _refresh_google_caldav_token(owner, account.get("id") or "")

async def sync_caldav(owner: str) -> dict:
    """Pull CalDAV state into local DB for `owner` across all configured accounts.
    Returns aggregated counts + per-account errors."""
    from src.secret_storage import decrypt

    accounts = _load_caldav_accounts(owner)
    if not accounts:
        return {
            "calendars": 0, "events": 0, "deleted": 0,
            "errors": ["CalDAV is not configured"],
        }

    totals: dict = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    for acc in accounts:
        url = (acc.get("url") or "").strip()
        user = (acc.get("username") or "").strip()
        account_id = acc.get("id") or ""
        label = acc.get("label") or url or account_id
        auth_mode = (acc.get("auth_mode") or "basic").lower()
        access_token = None
        pw = ""
        if auth_mode == "oauth":
            access_token = _resolve_google_caldav_token(owner, acc)
            if not access_token:
                totals["errors"].append(
                    f"{label}: Google authorization expired — reconnect Google Calendar")
                continue
            if not (url and user):
                totals["errors"].append(f"{label}: missing URL or account email")
                continue
        else:
            pw = acc.get("password") or ""
            try:
                pw = decrypt(pw)
            except Exception:
                pass
            if not (url and user and pw):
                totals["errors"].append(f"{label}: missing URL, username, or password")
                continue
        try:
            url = validate_caldav_url(url)
            result = await asyncio.to_thread(
                _sync_blocking, owner, url, user, pw, account_id, access_token,
                bool(acc.get("read_only")))
        except ValueError as e:
            result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)]}
        except Exception as e:
            logger.exception("CalDAV sync raised for account %s", label)
            result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)[:200]]}
        totals["calendars"] += result.get("calendars", 0)
        totals["events"] += result.get("events", 0)
        totals["deleted"] += result.get("deleted", 0)
        for err in result.get("errors", []):
            totals["errors"].append(f"{label}: {err}")
    return totals


async def push_event_create(owner: str, uid: str) -> dict:
    loaded = _load_event_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    source, calendar_id, payload = loaded
    from src.caldav_writeback import writeback_event
    return await writeback_event(owner, source, calendar_id, payload)


async def push_event_update(owner: str, uid: str) -> dict:
    return await push_event_create(owner, uid)


async def push_event_delete(owner: str, uid: str) -> dict:
    loaded = _load_delete_for_writeback(owner, uid)
    if not loaded:
        return {"ok": True, "skipped": True}
    source, calendar_id, payload = loaded
    from src.caldav_writeback import writeback_event
    return await writeback_event(owner, source, calendar_id, payload, delete=True)


async def push_pending_events(owner: str) -> dict:
    result = {"events": 0, "errors": []}
    uids, delete_uids = _pending_writeback_uids(owner)
    for event_uid in uids:
        try:
            out = await push_event_update(owner, event_uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{event_uid}: {str(out.get('error') or out)[:160]}")
        except Exception as e:
            logger.warning("CalDAV pending push failed for uid=%s: %s", event_uid, e)
            result["errors"].append(f"{event_uid}: {str(e)[:160]}")
    for event_uid in delete_uids:
        try:
            out = await push_event_delete(owner, event_uid)
            if out.get("ok"):
                result["events"] += 1
            elif not out.get("skipped"):
                result["errors"].append(f"{event_uid}: {str(out.get('error') or out)[:160]}")
        except Exception as e:
            logger.warning("CalDAV pending delete failed for uid=%s: %s", event_uid, e)
            result["errors"].append(f"{event_uid}: {str(e)[:160]}")
    return result


async def sync_caldav_direction(owner: str, direction: str = "pull") -> dict:
    direction = (direction or "pull").strip().lower()
    if direction == "pull":
        return await sync_caldav(owner)
    if direction == "push":
        return await push_pending_events(owner)
    if direction == "both":
        pushed = await push_pending_events(owner)
        pulled = await sync_caldav(owner)
        return {"push": pushed, "pull": pulled}
    return {
        "calendars": 0,
        "events": 0,
        "deleted": 0,
        "errors": [f"Unsupported CalDAV sync direction: {direction}"],
    }
