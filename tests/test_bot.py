import unittest
from datetime import datetime
from unittest.mock import patch

import bot


class BotDateParsingTests(unittest.TestCase):
    def test_strict_iso_datetime_parses(self) -> None:
        parsed = bot.parse_due_datetime("2027-01-01 00:01")
        self.assertEqual(parsed, datetime(2027, 1, 1, 0, 1))

    def test_strict_iso_date_only_uses_default_hour(self) -> None:
        parsed = bot.parse_due_datetime("2027-01-01")
        self.assertEqual(parsed, datetime(2027, 1, 1, 9, 0))

    @patch.object(bot, "DEFAULT_REMINDER_HOUR", 9)
    def test_month_name_datetime_is_accepted(self) -> None:
        parsed = bot.parse_due_datetime("January 1, 2003 12:01am")
        self.assertEqual(parsed, datetime(2003, 1, 1, 0, 1))

    def test_12_hour_iso_format_is_accepted(self) -> None:
        parsed = bot.parse_due_datetime("2027-01-01 12:01am")
        self.assertEqual(parsed, datetime(2027, 1, 1, 0, 1))

    @patch.object(bot, "DEFAULT_REMINDER_HOUR", 9)
    def test_month_name_date_only_uses_default_hour(self) -> None:
        parsed = bot.parse_due_datetime("December 24, 2050")
        self.assertEqual(parsed, datetime(2050, 12, 24, 9, 0))

    def test_slash_date_with_time_is_accepted(self) -> None:
        parsed = bot.parse_due_datetime("12/24/2050 23:45")
        self.assertEqual(parsed, datetime(2050, 12, 24, 23, 45))

    def test_garbage_date_is_rejected(self) -> None:
        self.assertIsNone(bot.parse_due_datetime("not a date"))


class DetectionRafflePrescanTests(unittest.TestCase):
    def test_raffle_keyword_flags_raffle(self) -> None:
        from detection import prescan
        self.assertIn("raffle", prescan("let's do a raffle tonight"))

    def test_bingo_at_start_does_not_false_flag_without_money(self) -> None:
        from detection import prescan
        # "bingo" alone is not a strong signal anymore
        self.assertNotIn("raffle", prescan("bingo night is fun"))

    def test_no_plus_number_does_not_false_flag_raffle(self) -> None:
        from detection import prescan
        self.assertNotIn("raffle", prescan("no, it's 25 minutes"))

    def test_lucky_draw_flags_raffle(self) -> None:
        from detection import prescan
        self.assertIn("raffle", prescan("we're holding a lucky draw"))

    def test_join_plus_money_flags_weak_raffle(self) -> None:
        from detection import prescan
        self.assertIn("raffle", prescan("join for 100 pesos"))


if __name__ == "__main__":
    unittest.main()
