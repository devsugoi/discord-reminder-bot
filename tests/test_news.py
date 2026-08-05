import unittest
from datetime import datetime

import news


SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Inquirer</title>
    <item>
      <title> Senate passes budget bill </title>
      <link>https://example.com/ph1</link>
    </item>
    <item>
      <title>Metro Manila weather alert</title>
      <link>https://example.com/ph2</link>
    </item>
  </channel>
</rss>
"""


class NewsParseTests(unittest.TestCase):
    def test_parse_rss_xml_extracts_titles_and_links(self) -> None:
        items = news._parse_feed_xml(SAMPLE_RSS, "https://example.com/feed", limit=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Senate passes budget bill")
        self.assertEqual(items[0].link, "https://example.com/ph1")
        self.assertEqual(items[1].title, "Metro Manila weather alert")
        self.assertEqual(items[1].link, "https://example.com/ph2")


class NewsFormatTests(unittest.TestCase):
    def test_format_digest_with_links_uses_markdown(self) -> None:
        ph = [news.HeadlineItem("Senate passes budget bill", "https://example.com/ph1")]
        world = [news.HeadlineItem("Global markets rally", "https://example.com/w1")]
        message = news.format_digest(
            ph,
            world,
            include_links=True,
            when=datetime(2026, 8, 5, 9, 0),
        )
        self.assertIn("[Senate passes budget bill](https://example.com/ph1)", message)
        self.assertIn("[Global markets rally](https://example.com/w1)", message)

    def test_format_digest_without_links_omits_urls(self) -> None:
        ph = [news.HeadlineItem("Senate passes budget bill", "https://example.com/ph1")]
        world = [news.HeadlineItem("Global markets rally", "https://example.com/w1")]
        message = news.format_digest(
            ph,
            world,
            include_links=False,
            when=datetime(2026, 8, 5, 9, 0),
        )
        self.assertIn("• Senate passes budget bill", message)
        self.assertNotIn("https://example.com/ph1", message)
        self.assertNotIn("[", message)

    def test_format_digest_truncates_without_splitting_links(self) -> None:
        long_title = "A" * 500
        ph = [
            news.HeadlineItem(long_title, "https://example.com/one"),
            news.HeadlineItem("Second headline", "https://example.com/two"),
        ]
        original_max = news.NEWS_MAX_CHARS
        try:
            news.NEWS_MAX_CHARS = 300
            message = news.format_digest(ph, [], include_links=True)
            self.assertLessEqual(len(message), 300)
            self.assertNotIn("https://example.com/one", message)
        finally:
            news.NEWS_MAX_CHARS = original_max


class NewsScheduleTests(unittest.TestCase):
    def test_should_deliver_now_uses_catch_up_rule(self) -> None:
        now = datetime(2026, 8, 5, 9, 5)
        self.assertTrue(news.should_deliver_now(now, "09:00"))
        self.assertFalse(news.should_deliver_now(now, "09:06"))

    def test_validate_news_time_rejects_invalid_values(self) -> None:
        self.assertTrue(news.validate_news_time("09:00"))
        self.assertFalse(news.validate_news_time("9:00"))
        self.assertFalse(news.validate_news_time("25:00"))

    def test_taglish_enabled_uses_env_default(self) -> None:
        original = news.NEWS_TAGLISH_DEFAULT
        try:
            news.NEWS_TAGLISH_DEFAULT = False
            self.assertFalse(news.taglish_enabled(""))
            self.assertTrue(news.taglish_enabled("taglish"))
            self.assertFalse(news.taglish_enabled("english"))
        finally:
            news.NEWS_TAGLISH_DEFAULT = original


if __name__ == "__main__":
    unittest.main()
