"""
Zulip bot that finds common available meeting times across participants
by fetching their ICS calendar feeds.

Users register their ICS subscription URL and timezone with the bot:

    @**Scheduler** register https://calendar.google.com/calendar/ical/.../basic.ics America/New_York

Then anyone can schedule a meeting:

    @**Scheduler** schedule 30 @**Alice** @**Bob**

The bot fetches each participant's ICS feed, extracts busy events (converted
to UTC), inverts them against each person's local working hours
(09:00-17:00) converted to UTC, intersects free time across all
participants over a 2-week window, and replies with the earliest slot
that fits the requested duration.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from zulip_bots.bots.scheduler.ics import BusyEvent, extract_timezone, fetch_ics_events, busy_to_free
from zulip_bots.bots.scheduler.range_utils import (
    TimeRange,
    find_earliest_slot,
    intersect_ranges,
    snap_to_granularity,
)
from zulip_bots.lib import AbstractBotHandler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WINDOW_DAYS = 14
DEFAULT_WORKING_HOURS = (9, 0, 17, 0)  # 09:00-17:00 local
DEFAULT_GRANULARITY_MINUTES = 30
DEFAULT_TIMEZONE = "UTC"
STORAGE_KEY_PREFIX = "ics_url"

UTC = ZoneInfo("UTC")

# Matches @**Name** or @**Name|user_id** (user_id present when duplicate names)
MENTION_RE = re.compile(r"@\*\*([^*|]+)(?:\|(\d+))?\*\*")
URL_RE = re.compile(r"https?://\S+")
TZ_RE = re.compile(r"[A-Za-z_]+/[A-Za-z_]+")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class Participant:
    events: List[BusyEvent]
    timezone: str


@dataclass
class Mention:
    name: str
    user_id: Optional[int] = None  # None when mention has no |user_id


@dataclass
class RegisterCommand:
    ics_url: str
    timezone: Optional[str] = None  # None means infer from ics


@dataclass
class ScheduleCommand:
    duration: int
    participants: List[Mention] = field(default_factory=list)


Command = Union[RegisterCommand, ScheduleCommand]


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------


def working_hours_to_utc(
    day: date,
    working_hours: Tuple[int, int, int, int],
    tz_name: str,
) -> TimeRange:
    """
    Convert local working hours to UTC minutes-from-midnight for a given date.

    If the end time wraps past UTC midnight, it is clamped to 1440.
    """
    sh, sm, eh, em = working_hours
    tz = ZoneInfo(tz_name)
    local_start = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
    local_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
    utc_start = local_start.astimezone(UTC)
    utc_end = local_end.astimezone(UTC)
    s = utc_start.hour * 60 + utc_start.minute
    e = utc_end.hour * 60 + utc_end.minute
    if utc_end.date() > utc_start.date():
        e = 1440
    return (s, e)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def find_common_slot(
    participants: List[Participant],
    duration_minutes: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
    working_hours: Tuple[int, int, int, int] = DEFAULT_WORKING_HOURS,
    start_date: Optional[date] = None,
    now: Optional[datetime] = None,
) -> Optional[Tuple[date, TimeRange]]:
    """
    Given a list of participants (busy events + timezone), find the earliest
    date+time slot within *window_days* days where everyone is free for at
    least *duration_minutes*.

    Returns ``(date, (start_minutes, end_minutes))`` in UTC or ``None``.
    """
    if not participants:
        return None

    if start_date is None:
        start_date = date.today()

    if now is None:
        now = datetime.now(UTC)

    now_min = None
    if now.date() == start_date:
        now_min = now.hour * 60 + now.minute

    for offset in range(window_days):
        day = start_date + timedelta(days=offset)

        daily_free: List[List[TimeRange]] = []
        for p in participants:
            day_start, day_end = working_hours_to_utc(day, working_hours, p.timezone)
            free = busy_to_free(p.events, day, day_start, day_end)
            if not free:
                daily_free = []
                break
            daily_free.append(free)

        if not daily_free:
            continue

        common = snap_to_granularity(
            intersect_ranges(daily_free), DEFAULT_GRANULARITY_MINUTES
        )

        # Clip to current time on the first day
        if now_min is not None and offset == 0:
            common = [(s, e) for s, e in common if e > now_min]
            common = [(max(s, now_min), e) for s, e in common]

        slot = find_earliest_slot(common, duration_minutes)
        if slot is not None:
            return (day, slot)

    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def minutes_to_time(total_minutes: int) -> str:
    """Convert minutes-from-midnight to ``HH:MM`` string."""
    h, m = divmod(total_minutes, 60)
    return f"{h:02d}:{m:02d}"


def format_slot_multi_tz(
    day: date, start_min: int, end_min: int, timezones: List[str]
) -> str:
    """
    Format a UTC slot as a string showing the time in each unique timezone.

    Example: "8:00 AM EDT / 5:00 AM PDT"
    """
    seen_offsets: set = set()
    parts: List[str] = []
    for tz_name in timezones:
        tz = ZoneInfo(tz_name)
        local_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC)
        local_start = local_start + timedelta(minutes=start_min)
        local_start = local_start.astimezone(tz)
        offset = local_start.utcoffset()
        if offset in seen_offsets:
            continue
        seen_offsets.add(offset)
        local_end = local_start + timedelta(minutes=end_min - start_min)
        parts.append(
            f"{local_start.strftime('%-I:%M %p %Z').strip()} – "
            f"{local_end.strftime('%-I:%M %p %Z').strip()}"
        )
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------


def parse_command(content: str) -> Optional[Command]:
    """
    Parse the bot-facing message content into a typed command.

    Returns ``RegisterCommand``, ``ScheduleCommand``, or ``None``.
    """
    content = content.strip()
    if not content:
        return None

    lower = content.lower()
    if lower.startswith("register "):
        rest = content[len("register "):].strip()
        url_match = URL_RE.search(rest)
        if not url_match:
            return None
        url = url_match.group(0)
        remainder = rest[url_match.end():].strip()
        if remainder and TZ_RE.match(remainder):
            return RegisterCommand(ics_url=url, timezone=remainder)
        return RegisterCommand(ics_url=url)

    if not lower.startswith("schedule "):
        return None
    content = content[len("schedule "):].strip()

    parts = content.split(None, 1)
    duration_str = parts[0]
    try:
        duration = int(duration_str)
    except ValueError:
        return None
    if duration <= 0:
        return None

    remainder = parts[1] if len(parts) > 1 else ""
    mentioned: List[Mention] = []
    for match in MENTION_RE.finditer(remainder):
        name = match.group(1).strip()
        uid = int(match.group(2)) if match.group(2) else None
        mentioned.append(Mention(name=name, user_id=uid))

    return ScheduleCommand(duration=duration, participants=mentioned)


# ---------------------------------------------------------------------------
# Bot handler
# ---------------------------------------------------------------------------


class SchedulerHandler:
    """
    Bot that finds common meeting times by fetching participants' ICS feeds,
    extracting busy events, and intersecting their free time (with timezone support).
    """

    def usage(self) -> str:
        return (
            "I find common meeting times based on participants' ICS calendar feeds.\n\n"
            "**Register your calendar:**\n"
            "```\n"
            "@**Scheduler** register <your_ics_url> [timezone]\n"
            "```\n"
            "Timezone is optional (inferred from your ICS feed if omitted). "
            "Examples: America/New_York, America/Los_Angeles, Europe/Paris.\n\n"
            "Get your ICS URL from your calendar provider:\n"
            "• Google: Settings → Integrate calendar → Secret address in iCal format\n"
            "• Proton: Settings → Calendars → Share with anyone → Create link\n"
            "• Apple: Calendar → Share Calendar → Public link\n\n"
            "**Schedule a meeting:**\n"
            "```\n"
            "@**Scheduler** schedule <duration_minutes> @**Alice** @**Bob**\n"
            "```\n"
            "Example: @**Scheduler** schedule 30 @**Alice** @**Bob**\n"
            "  → finds the earliest 30-minute slot where Alice and Bob are both free.\n\n"
            "Default working hours: 09:00-17:00 local time. Scheduling window: 14 days."
        )

    def initialize(self, bot_handler: AbstractBotHandler) -> None:
        self._client = getattr(bot_handler, "_client", None)
        self._storage = getattr(bot_handler, "storage", None)

    def _storage_key(self, user_id: int) -> str:
        return f"{STORAGE_KEY_PREFIX}:{user_id}"

    def _get_user_id_by_name(self, name: str) -> Optional[int]:
        """Look up a user ID by full name (case-insensitive)."""
        if self._client is None:
            return None
        response = self._client.get_users()
        if response.get("result") != "success":
            return None
        name_lower = name.lower()
        for member in response.get("members", []):
            if member["full_name"].lower() == name_lower:
                return member["user_id"]
        return None

    def _get_registration(self, user_id: int) -> Optional[Dict[str, str]]:
        """Retrieve a registered ICS URL + timezone for a user, or None."""
        if self._storage is None:
            raise RuntimeError("storage not available")
        key = self._storage_key(user_id)
        if not self._storage.contains(key):
            return None
        return self._storage.get(key)

    def _register(self, user_id: int, url: str, tz: str) -> None:
        """Store an ICS URL and timezone for a user."""
        if self._storage is None:
            raise RuntimeError("storage not available")
        self._storage.put(
            self._storage_key(user_id), {"url": url, "tz": tz}
        )

    def handle_message(
        self, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        content = message.get("content", "")
        if content.strip().lower() == "help":
            bot_handler.send_reply(message, self.usage())
            return

        cmd = parse_command(content)
        if cmd is None:
            bot_handler.send_reply(
                message,
                "I didn't understand that. Type `help` for usage.\n\n"
                "Register: @**Scheduler** register <ics_url> [timezone]\n"
                "Schedule: @**Scheduler** schedule 30 @**Alice** @**Bob**",
            )
            return

        if isinstance(cmd, RegisterCommand):
            self._handle_register(cmd, message, bot_handler)
        elif isinstance(cmd, ScheduleCommand):
            self._handle_schedule(cmd, message, bot_handler)

    def _handle_register(
        self, cmd: RegisterCommand, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        sender_id = message.get("sender_id")
        if sender_id is None:
            bot_handler.send_reply(message, "Could not identify your user ID.")
            return

        if cmd.timezone is not None:
            tz = cmd.timezone
        else:
            try:
                tz = extract_timezone(cmd.ics_url)
            except Exception:
                bot_handler.send_reply(
                    message,
                    "Could not fetch your timezone from your calendar. "
                    "Please specify it explicitly:\n\n"
                    "@**Scheduler** register <ics_url> America/New_York",
                )
                return

        try:
            self._register(sender_id, cmd.ics_url, tz)
        except RuntimeError:
            bot_handler.send_reply(message, "Storage error — could not save your registration.")
            return
        bot_handler.send_reply(
            message,
            f"Calendar registered (timezone: {tz}). "
            "I'll use this ICS feed when scheduling meetings with you.",
        )

    def _handle_schedule(
        self, cmd: ScheduleCommand, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        if not cmd.participants:
            bot_handler.send_reply(
                message,
                "Please @mention at least one participant.\n\n"
                "Example: @**Scheduler** schedule 30 @**Alice** @**Bob**",
            )
            return

        participants_data: List[Participant] = []
        participant_tzs: List[str] = []
        missing: List[str] = []
        for m in cmd.participants:
            uid = m.user_id
            if uid is None:
                uid = self._get_user_id_by_name(m.name)
            if uid is None:
                missing.append(f"{m.name} (user not found)")
                continue
            try:
                reg = self._get_registration(uid)
            except RuntimeError:
                bot_handler.send_reply(message, "Storage error — could not retrieve registrations.")
                return
            if reg is None:
                missing.append(f"{m.name} (no ICS URL registered)")
                continue
            try:
                events = fetch_ics_events(reg["url"])
            except Exception:
                missing.append(f"{m.name} (could not fetch ICS feed)")
                continue
            participants_data.append(Participant(events=events, timezone=reg["tz"]))
            participant_tzs.append(reg["tz"])

        if missing:
            bot_handler.send_reply(
                message,
                "Could not retrieve calendar data for:\n"
                + "\n".join(f"• {m}" for m in missing)
                + "\n\nAsk them to register: @**Scheduler** register <ics_url> [timezone]",
            )
            return

        names = [m.name for m in cmd.participants]
        result = find_common_slot(participants_data, cmd.duration)
        if result is None:
            bot_handler.send_reply(
                message,
                f"No common {cmd.duration}-minute slot found in the next "
                f"{DEFAULT_WINDOW_DAYS} days for: {', '.join(names)}.",
            )
            return

        day, (start_min, end_min) = result
        time_str = format_slot_multi_tz(day, start_min, end_min, participant_tzs)
        bot_handler.send_reply(
            message,
            f"Earliest common slot: **{day.strftime('%A, %B %d')}** "
            f"**{time_str}** ({cmd.duration} min) with {', '.join(names)}.",
        )


handler_class = SchedulerHandler
