Finds the earliest common available meeting time across participants
by fetching their ICS calendar feeds, with timezone support.

Each participant registers their calendar's ICS subscription URL and
their timezone with the bot. When scheduling, the bot fetches each
participant's ICS feed, extracts busy events (converted to UTC), inverts
them against each person's local working hours (09:00-17:00) converted
to UTC, intersects free time across all participants over a 2-week
window, and replies with the earliest slot that fits the requested
meeting duration.

## Commands

### Register your calendar

```
@**Scheduler** register <your_ics_url> [timezone]
```

Timezone is optional (defaults to UTC). Use IANA timezone names:
`America/New_York`, `America/Los_Angeles`, `Europe/Paris`, etc.

Get your ICS URL from your calendar provider:

- **Google Calendar**: Settings → Integrate calendar → Secret address in iCal format
- **Proton Calendar**: Settings → Calendars → Share with anyone → Create link
- **Apple Calendar**: Calendar → Share Calendar → Public link

The ICS URL is a persistent feed — you only need to register once.

### Schedule a meeting

```
@**Scheduler** schedule <duration_minutes> @**Alice** @**Bob**
```

Example:

```
@**Scheduler** schedule 30 @**Alice** @**Bob**
```

The bot will reply with the earliest 30-minute slot where Alice and
Bob are both free within the next 2 weeks. Times are displayed in UTC.

### Help

```
@**Scheduler** help
```

## Setup

1. Install the `icalendar` Python package in the bot's environment.

2. Configure the bot with a valid `zuliprc` file.

3. Each participant registers their ICS URL and timezone:
   `@**Scheduler** register <ics_url> [timezone]`

## Notes

- Default working hours: 09:00-17:00 in each participant's local timezone
- Scheduling window: 14 days from today
- Events marked as "Free" (TRANSP:TRANSPARENT) are ignored
- All-day events are skipped
- DST is handled automatically via `zoneinfo`
- ICS feeds may take up to 24 hours to reflect calendar changes
  (provider-dependent)
