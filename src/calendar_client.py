"""
Fetches band calendar events from one or more public Google Calendar iCal
feeds (CALENDAR_ICAL_URLS) and normalizes them for indexing alongside
newsletters.
"""

import base64
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from src.config import CALENDAR_ICAL_URLS, CALENDAR_TIMEZONE

logger = logging.getLogger(__name__)

CALENDAR_TZ = ZoneInfo(CALENDAR_TIMEZONE)

CALENDAR_ID_PATTERN = re.compile(r"/ical/([^/]+)/public")


@dataclass
class CalendarEvent:
    uid: str
    calendar_id: str
    summary: str
    start: datetime  # always tz-aware
    location: str
    description: str
    calendar_name: str
    all_day: bool


def _to_datetime(value) -> datetime:
    """iCal all-day events give a date, not a datetime — normalize to midnight."""
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day, tzinfo=CALENDAR_TZ)


def fetch_all_events() -> list[CalendarEvent]:
    """Fetch and parse every configured calendar. Best-effort per feed."""
    events = []

    for url in CALENDAR_ICAL_URLS:
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch calendar {url}: {e}")
            continue

        cal = Calendar.from_ical(resp.text)
        calendar_name = str(cal.get("X-WR-CALNAME", "Band Calendar"))
        calendar_id_match = CALENDAR_ID_PATTERN.search(url)
        calendar_id = calendar_id_match.group(1) if calendar_id_match else ""
        if not calendar_id:
            logger.warning(f"Could not extract calendar ID from {url!r} — event links will fall back to CALENDAR_INFO_URL")

        feed_count = 0
        for component in cal.walk():
            if component.name != "VEVENT":
                continue
            dtstart = component.get("dtstart")
            uid = str(component.get("uid", ""))
            if not dtstart or not uid:
                continue

            raw_start = dtstart.dt
            all_day = not isinstance(raw_start, datetime)

            events.append(CalendarEvent(
                uid=uid,
                calendar_id=calendar_id,
                summary=str(component.get("summary", "")).strip(),
                start=_to_datetime(raw_start),
                location=str(component.get("location", "")).strip(),
                description=str(component.get("description", "")).strip(),
                calendar_name=calendar_name,
                all_day=all_day,
            ))
            feed_count += 1

        logger.info(f"Fetched {feed_count} event(s) from {calendar_name!r}")

    return events


def format_event_date(event: CalendarEvent) -> str:
    local = event.start.astimezone(CALENDAR_TZ)
    if event.all_day:
        return local.strftime("%A, %B %d, %Y")
    return local.strftime("%A, %B %d, %Y at %I:%M %p")


# Matches UIDs Google Calendar generates natively for events created
# directly in it. Events synced in from an external calendar (e.g. an
# athletics schedule feed) instead carry a GUID-style UID, and the eid
# link trick below silently redirects to a generic Google Calendar
# marketing page rather than the event for those — verified against this
# calendar's real football-game entries, which are synced in this way.
NATIVE_UID_PATTERN = re.compile(r"^[a-z0-9]{20,}@google\.com$")


def event_url(event: CalendarEvent) -> str:
    """
    Direct link to this event's public Google Calendar page, for events
    where that's known to work. Falls back to "" (caller uses the general
    calendar URL instead) for externally-synced events, rather than risk
    showing a broken link.
    """
    if not event.calendar_id or not NATIVE_UID_PATTERN.match(event.uid):
        return ""
    event_id = event.uid.split("@")[0]
    raw = f"{event_id} {event.calendar_id}"
    eid = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"https://calendar.google.com/calendar/event?eid={eid}"
