Finds the earliest common available meeting time across participants
by fetching their ICS calendar feeds, with timezone support.

Each participant registers their calendar's ICS subscription URL with
the bot. Timezone is detected automatically from the calendar feed.
When scheduling, the bot fetches each participant's ICS feed, extracts
busy events (converted to UTC), inverts them against each person's local
working hours (09:00-17:00) converted to UTC, intersects free time across
all participants over a 2-week window, and replies with the earliest slot
that fits the requested meeting duration.

## Commands

### Register your calendar

Get your calendar's ICS subscription URL:

- [Google Calendar](https://www.onecal.io/blog/how-to-get-an-ics-url-for-your-calendar) (only public calendars are supported; use the "See only free/busy" option)
- [Outlook](https://www.onecal.io/blog/how-to-get-an-ics-url-for-your-calendar)
- [Apple iCloud](https://www.onecal.io/blog/how-to-get-an-ics-url-for-your-calendar)
- [Proton](https://proton.me/support/share-calendar-via-link)

Then DM the bot or @ mention it with the ICS URL:

```
https://calendar.google.com/calendar/ical/.../basic.ics
```

The ICS URL is a persistent feed — you only need to register once.
**Everyone** mentioned in a schedule request must be registered,
otherwise the bot can't check their availability.

### Schedule a meeting

```
@**Scheduler** schedule meeting name 30 @**Alice** @**Bob**
```

Example:

```
@**Scheduler** schedule Weekly Review 45 @**Alice** @**Bob**
```

The bot finds the earliest slot where everyone is free and sends a
button to create the meeting with a video link and calendar invites.

### Help

```
@**Scheduler** help
```

## Setup

1. Install dependencies:

   ```
   pip install icalendar requests countrystatecity-timezones
   ```

2. Configure the bot with a valid `zuliprc` file.

3. Each participant registers their ICS URL by DMing or @ mentioning
   the bot with it.

## Notes

- Default working hours: 09:00-17:00 in each participant's local timezone
- Scheduling window: 14 days from today
- Events marked as "Free" (TRANSP:TRANSPARENT) are ignored
- All-day events are skipped
- DST is handled automatically via `zoneinfo`
- ICS feeds may take up to 24 hours to reflect calendar changes
  (provider-dependent)
