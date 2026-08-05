import os
import unittest
from unittest.mock import patch

import db
import url_reading
import video_understanding
from ai_parser import build_chat_payload
from tests.test_memory import TempDbTestCase


class ExtractUrlsTests(unittest.TestCase):
    def test_single_url(self) -> None:
        urls = url_reading.extract_urls("check https://example.com/article please")
        self.assertEqual(urls, ["https://example.com/article"])

    def test_multiple_urls(self) -> None:
        urls = url_reading.extract_urls(
            "see https://a.com and https://b.com/page."
        )
        self.assertEqual(urls, ["https://a.com", "https://b.com/page"])

    def test_strips_trailing_punctuation(self) -> None:
        urls = url_reading.extract_urls("read this: https://example.com/foo).")
        self.assertEqual(urls, ["https://example.com/foo"])

    def test_no_urls(self) -> None:
        self.assertEqual(url_reading.extract_urls("hello there"), [])


class SafeUrlTests(unittest.TestCase):
    def test_public_https_allowed(self) -> None:
        self.assertTrue(url_reading.is_safe_url("https://example.com/path"))

    def test_localhost_blocked(self) -> None:
        self.assertFalse(url_reading.is_safe_url("http://localhost/admin"))

    def test_private_ip_blocked(self) -> None:
        self.assertFalse(url_reading.is_safe_url("http://192.168.1.1/status"))

    def test_file_scheme_blocked(self) -> None:
        self.assertFalse(url_reading.is_safe_url("file:///etc/passwd"))


class PrepareUrlsTests(unittest.TestCase):
    def test_returns_urls_when_enabled(self) -> None:
        with patch.object(url_reading, "url_reading_enabled", return_value=True):
            result = url_reading.prepare_urls_for_gemini("https://example.com")
        self.assertEqual(result.urls, ["https://example.com"])
        self.assertTrue(result.uses_url_budget)

    def test_empty_when_disabled(self) -> None:
        with patch.object(url_reading, "url_reading_enabled", return_value=False):
            result = url_reading.prepare_urls_for_gemini("https://example.com")
        self.assertEqual(result.urls, [])
        self.assertFalse(result.uses_url_budget)

    def test_excludes_youtube_when_video_youtube_on(self) -> None:
        with patch.object(url_reading, "url_reading_enabled", return_value=True):
            with patch.object(video_understanding, "video_enabled", return_value=True):
                with patch.object(video_understanding, "youtube_enabled", return_value=True):
                    result = url_reading.prepare_urls_for_gemini(
                        "https://www.youtube.com/watch?v=abc123"
                    )
        self.assertEqual(result.urls, [])
        self.assertFalse(result.uses_url_budget)

    def test_rejects_unsafe_urls(self) -> None:
        with patch.object(url_reading, "url_reading_enabled", return_value=True):
            result = url_reading.prepare_urls_for_gemini("http://127.0.0.1/secret")
        self.assertEqual(result.urls, [])
        self.assertIn("http://127.0.0.1/secret", result.rejected)


class BuildChatPayloadUrlTests(unittest.TestCase):
    def test_includes_url_hint(self) -> None:
        payload = build_chat_payload(
            message_text="summarize this https://example.com",
            author_name="Alex",
            bot_name="Bot",
            context_lines=[],
            url_count=1,
        )
        self.assertIn("pasted 1 link", payload)


class ChatbotUrlReadingToggleTests(TempDbTestCase):
    def test_env_default_off(self) -> None:
        with patch.object(url_reading, "CHATBOT_URL_READING_ENABLED", False):
            self.assertFalse(url_reading.chatbot_url_reading_is_on())

    def test_db_override_on(self) -> None:
        with patch.object(url_reading, "CHATBOT_URL_READING_ENABLED", False):
            db.set_setting("chatbot_url_reading_enabled", "on")
            self.assertTrue(url_reading.chatbot_url_reading_is_on())


if __name__ == "__main__":
    unittest.main()
