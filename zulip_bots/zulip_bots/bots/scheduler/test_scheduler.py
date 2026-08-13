from datetime import date, datetime, timedelta
from typing import List, Tuple
from unittest import mock

from zulip_bots.bots.scheduler.ics import busy_to_free, extract_timezone, fetch_ics_events
from zulip_bots.bots.scheduler.range_utils import snap_to_granularity
from zulip_bots.bots.scheduler.scheduler import (
    Mention,
    RegisterCommand,
    ScheduleCommand,
    Participant,
    find_common_slot,
    parse_command,
    working_hours_to_utc,
)
from zulip_bots.test_lib import BotTestCase


class TestWorkingHoursToUtc(BotTestCase):
    bot_name = "scheduler"

    def test_utc(self) -> None:
        result = working_hours_to_utc(date(2024, 1, 1), (9, 0, 17, 0), "UTC")
        self.assertEqual(result, (540, 1020))

    def test_est(self) -> None:
        result = working_hours_to_utc(date(2024, 1, 1), (9, 0, 17, 0), "America/New_York")
        self.assertEqual(result, (840, 1320))

    def test_edt(self) -> None:
        result = working_hours_to_utc(date(2024, 7, 1), (9, 0, 17, 0), "America/New_York")
        self.assertEqual(result, (780, 1260))

    def test_pst(self) -> None:
        result = working_hours_to_utc(date(2024, 1, 1), (9, 0, 17, 0), "America/Los_Angeles")
        self.assertEqual(result, (1020, 1440))

    def test_pdt(self) -> None:
        result = working_hours_to_utc(date(2024, 7, 1), (9, 0, 17, 0), "America/Los_Angeles")
        self.assertEqual(result, (960, 1440))


class TestSnapToGranularity(BotTestCase):
    bot_name = "scheduler"

    def test_snaps_up(self) -> None:
        self.assertEqual(
            snap_to_granularity([(901, 1020)], 30),
            [(930, 1020)],
        )


class TestBusyToFree(BotTestCase):
    bot_name = "scheduler"

    def test_one_event_in_middle(self) -> None:
        day = date(2024, 1, 1)
        events: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 1, 11, 0, tzinfo=None), datetime(2024, 1, 1, 12, 0, tzinfo=None)),
        ]
        self.assertEqual(
            busy_to_free(events, day, 540, 1020),
            [(540, 660), (720, 1020)],
        )

    def test_event_on_different_day(self) -> None:
        day = date(2024, 1, 1)
        events: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 2, 10, 0, tzinfo=None), datetime(2024, 1, 2, 11, 0, tzinfo=None)),
        ]
        self.assertEqual(
            busy_to_free(events, day, 540, 1020),
            [(540, 1020)],
        )


class TestFindCommonSlot(BotTestCase):
    bot_name = "scheduler"

    def test_basic_same_tz(self) -> None:
        day = date(2024, 1, 1)
        person_a: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 1, 10, 0, tzinfo=None), datetime(2024, 1, 1, 11, 0, tzinfo=None)),
        ]
        person_b: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 1, 11, 0, tzinfo=None), datetime(2024, 1, 1, 12, 0, tzinfo=None)),
        ]
        result = find_common_slot(
            [Participant(person_a, "UTC"), Participant(person_b, "UTC")],
            60, start_date=day,
        )
        self.assertIsNotNone(result)
        result_day, (start, end) = result  # type: ignore[misc]
        self.assertEqual(result_day, day)
        self.assertEqual(start, 540)
        self.assertEqual(end, 600)

    def test_different_tz(self) -> None:
        day = date(2024, 1, 1)
        person_a: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 1, 14, 0, tzinfo=None), datetime(2024, 1, 1, 15, 0, tzinfo=None)),
        ]
        person_b: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 1, 15, 0, tzinfo=None), datetime(2024, 1, 1, 16, 0, tzinfo=None)),
        ]
        result = find_common_slot(
            [Participant(person_a, "America/New_York"), Participant(person_b, "UTC")],
            60, start_date=day,
        )
        self.assertIsNotNone(result)
        result_day, (start, end) = result  # type: ignore[misc]
        self.assertEqual(result_day, day)
        self.assertEqual(start, 960)
        self.assertEqual(end, 1020)

    def test_no_common_slot(self) -> None:
        day = date(2024, 1, 1)
        person_a: List[Tuple[datetime, datetime]] = []
        for i in range(14):
            d = day + timedelta(days=i)
            person_a.append(
                (datetime(d.year, d.month, d.day, 9, 0, tzinfo=None),
                 datetime(d.year, d.month, d.day, 17, 0, tzinfo=None))
            )
        result = find_common_slot(
            [Participant(person_a, "UTC"), Participant([], "UTC")],
            60, start_date=day,
        )
        self.assertIsNone(result)

    def test_finds_next_day(self) -> None:
        day = date(2024, 1, 1)
        person_a: List[Tuple[datetime, datetime]] = [
            (datetime(2024, 1, 1, 9, 0, tzinfo=None), datetime(2024, 1, 1, 17, 0, tzinfo=None)),
        ]
        result = find_common_slot(
            [Participant(person_a, "UTC"), Participant([], "UTC")],
            60, start_date=day,
        )
        self.assertIsNotNone(result)
        result_day, (start, end) = result  # type: ignore[misc]
        self.assertEqual(result_day, date(2024, 1, 2))
        self.assertEqual(start, 540)
        self.assertEqual(end, 600)

    def test_empty_participants(self) -> None:
        result = find_common_slot([], 60, start_date=date(2024, 1, 1))
        self.assertIsNone(result)

    def test_no_events_all_free(self) -> None:
        day = date(2024, 1, 1)
        result = find_common_slot(
            [Participant([], "UTC"), Participant([], "UTC")],
            60, start_date=day,
        )
        self.assertIsNotNone(result)
        result_day, (start, end) = result  # type: ignore[misc]
        self.assertEqual(result_day, day)
        self.assertEqual(start, 540)
        self.assertEqual(end, 600)


class TestParseCommand(BotTestCase):
    bot_name = "scheduler"

    def test_register(self) -> None:
        cmd = parse_command(
            "register https://calendar.google.com/calendar/ical/abc/basic.ics"
        )
        assert isinstance(cmd, RegisterCommand)
        self.assertEqual(cmd.ics_url, "https://calendar.google.com/calendar/ical/abc/basic.ics")

    def test_schedule_with_user_id(self) -> None:
        cmd = parse_command("schedule 30 @**Alice|1** @**Bob|2**")
        assert isinstance(cmd, ScheduleCommand)
        self.assertEqual(cmd.duration, 30)
        self.assertEqual(len(cmd.participants), 2)
        self.assertEqual(cmd.participants[0], Mention(name="Alice", user_id=1))
        self.assertEqual(cmd.participants[1], Mention(name="Bob", user_id=2))

    def test_schedule_name_only(self) -> None:
        cmd = parse_command("schedule 30 @**Alice** @**Bob**")
        assert isinstance(cmd, ScheduleCommand)
        self.assertEqual(cmd.duration, 30)
        self.assertEqual(len(cmd.participants), 2)
        self.assertEqual(cmd.participants[0], Mention(name="Alice", user_id=None))
        self.assertEqual(cmd.participants[1], Mention(name="Bob", user_id=None))

    def test_schedule_mixed(self) -> None:
        cmd = parse_command("schedule 30 @**Alice** @**Bob|2**")
        assert isinstance(cmd, ScheduleCommand)
        self.assertEqual(len(cmd.participants), 2)
        self.assertEqual(cmd.participants[0], Mention(name="Alice", user_id=None))
        self.assertEqual(cmd.participants[1], Mention(name="Bob", user_id=2))

    def test_schedule_no_duration(self) -> None:
        cmd = parse_command("schedule @**Alice**")
        self.assertIsNone(cmd)

    def test_schedule_missing_keyword(self) -> None:
        cmd = parse_command("30 @**Alice** @**Bob**")
        self.assertIsNone(cmd)

    def test_empty(self) -> None:
        cmd = parse_command("")
        self.assertIsNone(cmd)


class TestSchedulerBot(BotTestCase):
    bot_name = "scheduler"

    def test_register_success(self) -> None:
        bot, bot_handler = self._get_handlers()
        message = self.make_request_message(
            "register https://calendar.google.com/calendar/ical/abc/basic.ics"
        )
        message["sender_id"] = 12345
        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.extract_timezone",
            return_value="America/New_York",
        ), mock.patch(
            "zulip_bots.bots.scheduler.scheduler.tz_abbreviation",
            return_value="EDT",
        ):
            bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("registered", reply["content"].lower())
        self.assertIn("EDT", reply["content"])
        self.assertIn("United States", reply["content"])
        self.assertIn("run the register command again", reply["content"])
        stored = bot._storage.get("ics_url:12345")
        self.assertEqual(stored["url"], "https://calendar.google.com/calendar/ical/abc/basic.ics")
        self.assertEqual(stored["tz"], "America/New_York")

    def test_schedule_successful_slot_with_user_id(self) -> None:
        bot, bot_handler = self._get_handlers()
        bot._storage.put("ics_url:1", {"url": "https://example.com/alice.ics", "tz": "UTC"})
        bot._storage.put("ics_url:2", {"url": "https://example.com/bob.ics", "tz": "UTC"})

        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.fetch_ics_events",
            return_value=[],
        ):
            with mock.patch(
                "zulip_bots.bots.scheduler.scheduler.date"
            ) as mock_date:
                mock_date.today.return_value = date(2024, 1, 1)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                message = self.make_request_message("schedule 30 @**Alice|1** @**Bob|2**")
                bot_handler.reset_transcript()
                bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("Earliest common slot", reply["content"])
        self.assertIn("9:00 AM", reply["content"])
        self.assertIn("9:30 AM", reply["content"])
        self.assertIn("UTC", reply["content"])

    def test_schedule_successful_slot_name_only(self) -> None:
        bot, bot_handler = self._get_handlers()
        bot._client = mock.MagicMock()
        bot._client.get_users.return_value = {
            "result": "success",
            "members": [
                {"full_name": "Alice", "user_id": 1},
                {"full_name": "Bob", "user_id": 2},
            ],
        }
        bot._storage.put("ics_url:1", {"url": "https://example.com/alice.ics", "tz": "UTC"})
        bot._storage.put("ics_url:2", {"url": "https://example.com/bob.ics", "tz": "UTC"})

        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.fetch_ics_events",
            return_value=[],
        ):
            with mock.patch(
                "zulip_bots.bots.scheduler.scheduler.date"
            ) as mock_date:
                mock_date.today.return_value = date(2024, 1, 1)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                message = self.make_request_message("schedule 30 @**Alice** @**Bob**")
                bot_handler.reset_transcript()
                bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("Earliest common slot", reply["content"])
        self.assertIn("9:00 AM", reply["content"])
        self.assertIn("9:30 AM", reply["content"])

    def test_schedule_user_not_found(self) -> None:
        bot, bot_handler = self._get_handlers()
        bot._client = mock.MagicMock()
        bot._client.get_users.return_value = {
            "result": "success",
            "members": [],
        }
        message = self.make_request_message("schedule 30 @**Alice**")
        bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("user not found", reply["content"])

    def test_schedule_user_not_registered(self) -> None:
        bot, bot_handler = self._get_handlers()
        message = self.make_request_message("schedule 30 @**Alice|999**")
        bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("no ICS URL registered", reply["content"])

    def test_schedule_multi_tz_display(self) -> None:
        bot, bot_handler = self._get_handlers()
        bot._storage.put("ics_url:1", {"url": "https://example.com/alice.ics", "tz": "America/New_York"})
        bot._storage.put("ics_url:2", {"url": "https://example.com/bob.ics", "tz": "America/Los_Angeles"})

        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.fetch_ics_events",
            return_value=[],
        ):
            with mock.patch(
                "zulip_bots.bots.scheduler.scheduler.date"
            ) as mock_date:
                mock_date.today.return_value = date(2024, 1, 1)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                message = self.make_request_message("schedule 30 @**Alice|1** @**Bob|2**")
                bot_handler.reset_transcript()
                bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("Earliest common slot", reply["content"])
        # Both timezones should appear, no repeats
        self.assertIn("EST", reply["content"])
        self.assertIn("PST", reply["content"])

    def test_schedule_same_tz_no_repeat(self) -> None:
        bot, bot_handler = self._get_handlers()
        bot._storage.put("ics_url:1", {"url": "https://example.com/alice.ics", "tz": "America/New_York"})
        bot._storage.put("ics_url:2", {"url": "https://example.com/bob.ics", "tz": "America/New_York"})

        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.fetch_ics_events",
            return_value=[],
        ):
            with mock.patch(
                "zulip_bots.bots.scheduler.scheduler.date"
            ) as mock_date:
                mock_date.today.return_value = date(2024, 1, 1)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                message = self.make_request_message("schedule 30 @**Alice|1** @**Bob|2**")
                bot_handler.reset_transcript()
                bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("Earliest common slot", reply["content"])
        # Should appear only once (as one tz entry, not two)
        self.assertEqual(reply["content"].count("EST"), 2)  # start + end in one entry

    def test_schedule_alias_tz_dedup(self) -> None:
        """America/New_York and America/Toronto are the same offset — dedup."""
        bot, bot_handler = self._get_handlers()
        bot._storage.put("ics_url:1", {"url": "https://example.com/alice.ics", "tz": "America/New_York"})
        bot._storage.put("ics_url:2", {"url": "https://example.com/bob.ics", "tz": "America/Toronto"})

        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.fetch_ics_events",
            return_value=[],
        ):
            with mock.patch(
                "zulip_bots.bots.scheduler.scheduler.date"
            ) as mock_date:
                mock_date.today.return_value = date(2024, 1, 1)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                message = self.make_request_message("schedule 30 @**Alice|1** @**Bob|2**")
                bot_handler.reset_transcript()
                bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("Earliest common slot", reply["content"])
        # Both resolve to EST, should only show one entry
        self.assertEqual(reply["content"].count("EST"), 2)  # start + end in one entry

    def test_schedule_no_common_slot(self) -> None:
        bot, bot_handler = self._get_handlers()
        bot._storage.put("ics_url:1", {"url": "https://example.com/alice.ics", "tz": "UTC"})
        bot._storage.put("ics_url:2", {"url": "https://example.com/bob.ics", "tz": "UTC"})

        busy_events: List[Tuple[datetime, datetime]] = []
        for i in range(14):
            d = date(2024, 1, 1) + timedelta(days=i)
            busy_events.append(
                (datetime(d.year, d.month, d.day, 9, 0, tzinfo=None),
                 datetime(d.year, d.month, d.day, 17, 0, tzinfo=None))
            )

        with mock.patch(
            "zulip_bots.bots.scheduler.scheduler.fetch_ics_events",
            return_value=busy_events,
        ):
            with mock.patch(
                "zulip_bots.bots.scheduler.scheduler.date"
            ) as mock_date:
                mock_date.today.return_value = date(2024, 1, 1)
                mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
                message = self.make_request_message("schedule 30 @**Alice|1** @**Bob|2**")
                bot_handler.reset_transcript()
                bot.handle_message(message, bot_handler)
        reply = bot_handler.unique_reply()
        self.assertIn("No common", reply["content"])
