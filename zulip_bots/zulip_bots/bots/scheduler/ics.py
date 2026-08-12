"""ICS feed fetching and busy-to-free conversion."""

from collections import Counter
from datetime import date, datetime
from typing import List, Tuple

import requests
from icalendar import Calendar
from zoneinfo import ZoneInfo

from zulip_bots.bots.scheduler.range_utils import TimeRange, subtract_ranges

UTC = ZoneInfo("UTC")

BusyEvent = Tuple[datetime, datetime]  # timezone-aware (UTC after conversion)


def extract_timezone(url: str, timeout: int = 30) -> str:
    """
    Fetch an ICS feed and infer the dominant timezone from event TZIDs.

    Returns the most common TZID across all VEVENTs, falling back to
    VTIMEZONE blocks, then to "UTC" if nothing is found.
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.text)

    tzids: Counter[str] = Counter()

    for event in cal.walk("VEVENT"):
        start_prop = event.get("DTSTART")
        if start_prop is None:
            continue
        tzid = start_prop.params.get("TZID")
        if tzid:
            tzids[str(tzid)] += 1

    if not tzids:
        for tz in cal.walk("VTIMEZONE"):
            tzid = tz.get("TZID")
            if tzid:
                tzids[str(tzid)] += 1

    if tzids:
        return tzids.most_common(1)[0][0]
    return "UTC"


def fetch_ics_events(url: str, timeout: int = 30) -> List[BusyEvent]:
    """
    Fetch an ICS feed over HTTP and return a list of (start, end) datetime
    tuples (converted to UTC) for busy (opaque) events.

    Skips:
      - Events with TRANSP:TRANSPARENT (marked as "free")
      - All-day events (date-only, no time component)
    """
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    cal = Calendar.from_ical(resp.text)
    events: List[BusyEvent] = []
    for event in cal.walk("VEVENT"):
        transp = event.get("TRANSP")
        if transp and str(transp).upper() == "TRANSPARENT":
            continue
        start_prop = event.get("DTSTART")
        if start_prop is None:
            continue
        start_dt = start_prop.dt
        if not isinstance(start_dt, datetime):
            continue
        end_prop = event.get("DTEND", event.get("DTSTART"))
        end_dt = end_prop.dt if end_prop else start_dt
        if not isinstance(end_dt, datetime):
            end_dt = datetime.combine(end_dt, datetime.min.time())
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=UTC)
        else:
            start_dt = start_dt.astimezone(UTC)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=UTC)
        else:
            end_dt = end_dt.astimezone(UTC)
        events.append((start_dt, end_dt))
    return events


def busy_to_free(
    busy_events: List[BusyEvent],
    day: date,
    day_start: int,
    day_end: int,
) -> List[TimeRange]:
    """
    Given busy events (in UTC) and a specific UTC date, return free time
    ranges within [day_start, day_end] (minutes from midnight UTC).

    Events are clipped to the day boundary and to the working-hours window.
    """
    busy_minutes: List[TimeRange] = []
    for start_dt, end_dt in busy_events:
        event_date = start_dt.date()
        end_date = end_dt.date()
        if event_date > day or end_date < day:
            continue
        if start_dt.date() == day:
            s = start_dt.hour * 60 + start_dt.minute
        else:
            s = 0
        if end_dt.date() == day:
            e = end_dt.hour * 60 + end_dt.minute
        else:
            e = 24 * 60
        s = max(s, day_start)
        e = min(e, day_end)
        if s < e:
            busy_minutes.append((s, e))

    available = [(day_start, day_end)]
    return subtract_ranges(available, busy_minutes)
