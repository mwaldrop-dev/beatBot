"""
Fetches band calendar events from one or more public Google Calendar iCal
feeds (CALENDAR_ICAL_URLS) and normalizes them for indexing alongside
newsletters.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar

from src.config import CALENDAR_ICAL_URLS, CALENDAR_TIMEZONE

logger = logging.getLogger(__name__)

CALENDAR_TZ = ZoneInfo(CALENDAR_TIMEZONE)


@dataclass
class CalendarEvent:
    uid: str
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
