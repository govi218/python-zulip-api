"""
Zulip bot that finds common available meeting times across participants
by fetching their ICS calendar feeds.

Users register their ICS subscription URL with the bot:

    @**Scheduler** register https://calendar.google.com/calendar/ical/.../basic.ics

or simply DM the bot a URL:

    https://calendar.google.com/calendar/ical/.../basic.ics

Then anyone can schedule a meeting:

    @**Scheduler** schedule standup 30 @**Alice** @**Bob**

The bot fetches each participant's ICS feed, extracts busy events (converted
to UTC), inverts them against each person's local working hours
(09:00-17:00) converted to UTC, intersects free time across all
participants over a 2-week window, and replies with the earliest slot
that fits the requested duration.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from countrystatecity_timezones import get_timezone_by_zone_name

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
STORAGE_KEY_PREFIX = "ics_url"

UTC = ZoneInfo("UTC")

# Matches @**Name** or @**Name|user_id** (user_id present when duplicate names)
MENTION_RE = re.compile(r"@\*\*([^*|]+)(?:\|(\d+))?\*\*")
URL_RE = re.compile(r"https?://\S+")

# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------


def get_country_name(tz_name: str) -> Optional[str]:
    """Get the country name for an IANA timezone."""
    try:
        info = get_timezone_by_zone_name(tz_name)
        return info.countryName if info else None
    except Exception:
        return None


def tz_abbreviation(tz_name: str) -> str:
    """Return the current timezone abbreviation (e.g. EDT, EST) for an IANA zone."""
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        return now.strftime("%Z")
    except Exception:
        return tz_name


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


@dataclass
class ScheduleCommand:
    duration: int
    name: str
    participants: List[Mention] = field(default_factory=list)


@dataclass
class ConfirmCommand:
    key: str  # "<name>-<date>" storage key


Command = Union[RegisterCommand, ScheduleCommand, ConfirmCommand]


# ---------------------------------------------------------------------------
# Working hours
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
    if now is not None:
        start_utc = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
        elapsed_min = int((now - start_utc).total_seconds() / 60)
        if 0 <= elapsed_min < 48 * 60:
            now_min = elapsed_min

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


def sanitize_meeting_name(name: str) -> str:
    """Sanitize a meeting name for use in a meet.jit.si URL."""
    return re.sub(r"[^a-zA-Z0-9]", "", name)


def format_schedule_widget(
    name: str, day: date, start_min: int, end_min: int, timezones: List[str]
) -> Tuple[str, str]:
    """Build a zform JSON payload with a button to confirm the meeting slot.

    Returns (content, widget_json) — content is a non-empty markdown string
    to satisfy Zulip's message requirement.
    """
    time_str = format_slot_multi_tz(day, start_min, end_min, timezones)
    heading = f"{day.strftime('%A, %B %d')} — {time_str}"
    key = f"{name}-{day.isoformat()}"
    reply = f"confirm {key}"
    widget_content = {
        "widget_type": "zform",
        "extra_data": {
            "type": "choices",
            "heading": heading,
            "choices": [
                {
                    "type": "multiple_choice",
                    "short_name": f"Create meeting: {name}",
                    "long_name": f"Create meeting: {name}",
                    "reply": reply,
                }
            ],
        },
    }
    return heading, json.dumps(widget_content)


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------


def parse_command(content: str) -> Optional[Command]:
    """
    Parse the bot-facing message content into a typed command.

    Returns ``RegisterCommand``, ``ScheduleCommand``, ``ConfirmCommand``, or ``None``.
    A bare URL (with no command prefix) is treated as a register command.
    """
    content = content.replace("\xa0", " ").strip()
    if not content:
        return None

    lower = content.lower()

    # confirm <key>  (sent by zform button click; key is "<name>-<date>")
    if lower.startswith("confirm "):
        key = content[len("confirm "):].strip()
        if not key:
            return None
        return ConfirmCommand(key=key)

    if lower.startswith("register "):
        rest = content[len("register "):].strip()
        url_match = URL_RE.search(rest)
        if not url_match:
            return None
        url = url_match.group(0)
        return RegisterCommand(ics_url=url)

    # Bare URL in a DM = register
    url_match = URL_RE.search(content)
    if url_match and url_match.start() == 0:
        return RegisterCommand(ics_url=url_match.group(0))

    if not lower.startswith("schedule "):
        return None
    content = content[len("schedule "):].strip()

    parts = content.split(None, 2)
    if len(parts) < 2:
        return None
    name = parts[0]
    duration_str = parts[1]
    try:
        duration = int(duration_str)
    except ValueError:
        return None
    if duration <= 0:
        return None

    remainder = parts[2] if len(parts) > 2 else ""
    mentioned: List[Mention] = []
    for match in MENTION_RE.finditer(remainder):
        mname = match.group(1).strip()
        uid = int(match.group(2)) if match.group(2) else None
        mentioned.append(Mention(name=mname, user_id=uid))

    return ScheduleCommand(duration=duration, name=name, participants=mentioned)


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
            "@**Scheduler** register <your_ics_url>\n"
            "```\n"
            "Or just DM me a URL directly.\n"
            "Timezone is inferred automatically from your calendar feed.\n\n"
            "Get your ICS URL from your calendar provider:\n"
            "• Google: Settings → Integrate calendar → Secret address in iCal format\n"
            "• Proton: Settings → Calendars → Share with anyone → Create link\n"
            "• Apple: Calendar → Share Calendar → Public link\n\n"
            "**Schedule a meeting:**\n"
            "```\n"
            "@**Scheduler** schedule <name> <duration_minutes> @**Alice** @**Bob**\n"
            "```\n"
            "Example: @**Scheduler** schedule standup 30 @**Alice** @**Bob**\n"
            "  → finds the earliest 30-minute slot and sends a button to create the meeting.\n\n"
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
        # Normalize non-breaking spaces (mobile clients sometimes insert them)
        content = content.replace("\xa0", " ")
        # Strip leading bot mention if present (e.g. in DMs where framework
        # doesn't strip it, or when mention isn't at the start for stream msgs)
        content = re.sub(r"^@\*\*[^*]+(?:\|\d+)?\*\*\s*", "", content)
        if content.strip().lower() == "help":
            bot_handler.send_reply(message, self.usage())
            return

        cmd = parse_command(content)
        if cmd is None:
            bot_handler.send_reply(
                message,
                "I didn't understand that. Type `help` for usage.\n\n"
                "Register: @**Scheduler** register <ics_url>\n"
                "Schedule: @**Scheduler** schedule <name> 30 @**Alice** @**Bob**",
            )
            return

        if isinstance(cmd, RegisterCommand):
            self._handle_register(cmd, message, bot_handler)
        elif isinstance(cmd, ScheduleCommand):
            self._handle_schedule(cmd, message, bot_handler)
        elif isinstance(cmd, ConfirmCommand):
            self._handle_confirm(cmd, message, bot_handler)

    def _handle_register(
        self, cmd: RegisterCommand, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        sender_id = message.get("sender_id")
        if sender_id is None:
            bot_handler.send_reply(message, "Could not identify your user ID.")
            return

        try:
            tz = extract_timezone(cmd.ics_url)
        except Exception:
            bot_handler.send_reply(
                message,
                "Could not fetch your calendar. "
                "Please check that your calendar ICS URL is correct and publicly accessible.",
            )
            return

        try:
            self._register(sender_id, cmd.ics_url, tz)
        except RuntimeError:
            bot_handler.send_reply(message, "Storage error — could not save your registration.")
            return

        country = get_country_name(tz) or ""
        country_str = f", {country}" if country else ""
        tz_abbr = tz_abbreviation(tz)
        bot_handler.send_reply(
            message,
            f"Calendar registered — timezone: **{tz_abbr}**{country_str}. "
            "I'll use this calendar to check your availabity.\n"
            "You can change the calendar anytime by sending me another URL.",
        )

    def _handle_schedule(
        self, cmd: ScheduleCommand, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        if not cmd.participants:
            bot_handler.send_reply(
                message,
                "Please @mention at least one participant.\n\n"
                "Example: @**Scheduler** schedule standup 30 @**Alice** @**Bob**",
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
                missing.append(f"{m.name} (could not fetch calendar URL)")
                continue
            participants_data.append(Participant(events=events, timezone=reg["tz"]))
            participant_tzs.append(reg["tz"])

        if missing:
            bot_handler.send_reply(
                message,
                "Could not retrieve calendar data for:\n"
                + "\n".join(f"• {m}" for m in missing)
                + "\n\nAsk them to register: @**Scheduler** register <ics_url>",
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
        heading, widget = format_schedule_widget(cmd.name, day, start_min, end_min, participant_tzs)
        # Save meeting details to storage for confirm lookup
        key = f"{cmd.name}-{day.isoformat()}"
        if self._storage is not None:
            self._storage.put(f"meeting:{key}", {
                "name": cmd.name,
                "day": day.isoformat(),
                "start": start_min,
                "end": end_min,
                "participants": names,
            })

        bot_handler.send_reply(message, heading, widget)

    def _handle_confirm(
        self, cmd: ConfirmCommand, message: Dict[str, Any], bot_handler: AbstractBotHandler
    ) -> None:
        if self._storage is None:
            bot_handler.send_reply(message, "Storage error — cannot retrieve meeting details.")
            return
        key = f"meeting:{cmd.key}"
        if not self._storage.contains(key):
            bot_handler.send_reply(message, "Meeting not found. It may have expired.")
            return
        details = self._storage.get(key)
        day = date.fromisoformat(details["day"])
        room = sanitize_meeting_name(details["name"])
        jitsi = f"https://meet.jit.si/{room}"
        bot_handler.send_reply(
            message,
            f"**{details['name']}** — {day.strftime('%A, %B %d')} at "
            f"{details['start'] // 60:02d}:{details['start'] % 60:02d}–"
            f"{details['end'] // 60:02d}:{details['end'] % 60:02d} UTC\n"
            f"Join: {jitsi}",
        )


handler_class = SchedulerHandler
