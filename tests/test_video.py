import os
import unittest
from unittest.mock import patch

import db
import video_understanding
from ai_parser import build_chat_payload
from tests.test_memory import TempDbTestCase


class ExtractYoutubeUrlTests(unittest.TestCase):
    def test_watch_url(self) -> None:
        url = video_understanding.extract_youtube_url(
            "check this https://www.youtube.com/watch?v=dQw4w9WgXcQ please"
        )
        self.assertEqual(url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_short_url(self) -> None:
        url = video_understanding.extract_youtube_url("https://youtu.be/abc123")
        self.assertEqual(url, "https://youtu.be/abc123")

    def test_non_youtube_ignored(self) -> None:
        self.assertIsNone(video_understanding.extract_youtube_url("https://example.com/video"))


class VideoAttachmentDetectionTests(unittest.TestCase):
    def test_content_type(self) -> None:
        self.assertTrue(video_understanding.is_video_attachment("video/mp4", "clip.mp4"))

    def test_extension_fallback(self) -> None:
        self.assertTrue(video_understanding.is_video_attachment(None, "clip.webm"))

    def test_image_rejected(self) -> None:
        self.assertFalse(video_understanding.is_video_attachment("image/png", "pic.png"))


class PrepareVideoTests(unittest.TestCase):
    def test_inline_for_small_clip(self) -> None:
        with patch.multiple(
            video_understanding,
            inline_enabled=lambda: True,
            files_api_enabled=lambda: False,
            frames_enabled=lambda: False,
            any_attachment_method_enabled=lambda: True,
            get_video_duration_sec=lambda _b, _m: 5.0,
        ):
            prepared = video_understanding.prepare_video_for_gemini(
                b"x" * 1024,
                "video/mp4",
                "clip.mp4",
            )
        self.assertEqual(prepared.mode, "inline")
        self.assertTrue(prepared.uses_video_budget)

    def test_rejects_oversized_clip(self) -> None:
        with patch.object(video_understanding, "any_attachment_method_enabled", return_value=True):
            big = b"x" * (video_understanding.CHATBOT_VIDEO_MAX_SIZE_MB * 1024 * 1024 + 1)
            prepared = video_understanding.prepare_video_for_gemini(big, "video/mp4", "big.mp4")
        self.assertIsNotNone(prepared.error_message)

    def test_frames_fallback(self) -> None:
        inline_limit = video_understanding.CHATBOT_VIDEO_INLINE_MAX_MB * 1024 * 1024 + 1
        frames = [(b"frame", "image/jpeg")]
        with patch.multiple(
            video_understanding,
            inline_enabled=lambda: True,
            files_api_enabled=lambda: False,
            frames_enabled=lambda: True,
            any_attachment_method_enabled=lambda: True,
            get_video_duration_sec=lambda _b, _m: 5.0,
            ffmpeg_available=lambda: True,
            extract_key_frames=lambda _b, _m: frames,
        ):
            prepared = video_understanding.prepare_video_for_gemini(
                b"x" * inline_limit,
                "video/mp4",
                "clip.mp4",
            )
        self.assertEqual(prepared.mode, "frames")
        self.assertEqual(prepared.frames, frames)

    def test_files_api_when_inline_too_large(self) -> None:
        inline_limit = video_understanding.CHATBOT_VIDEO_INLINE_MAX_MB * 1024 * 1024 + 1
        with patch.multiple(
            video_understanding,
            inline_enabled=lambda: True,
            files_api_enabled=lambda: True,
            frames_enabled=lambda: False,
            any_attachment_method_enabled=lambda: True,
            get_video_duration_sec=lambda _b, _m: 5.0,
        ):
            prepared = video_understanding.prepare_video_for_gemini(
                b"x" * inline_limit,
                "video/mp4",
                "clip.mp4",
            )
        self.assertEqual(prepared.mode, "files_api")


class BuildChatPayloadVideoTests(unittest.TestCase):
    def test_includes_video_hint(self) -> None:
        payload = build_chat_payload(
            message_text="what is this?",
            author_name="Alex",
            bot_name="Bot",
            context_lines=[],
            video_mode="inline",
        )
        self.assertIn("short video clip", payload)

    def test_frames_hint(self) -> None:
        payload = build_chat_payload(
            message_text="what happens?",
            author_name="Alex",
            bot_name="Bot",
            context_lines=[],
            image_count=3,
            video_mode="frames",
        )
        self.assertIn("extracted still frame", payload)


class ChatbotVideoToggleTests(TempDbTestCase):
    def test_env_default_off(self) -> None:
        with patch.object(video_understanding, "CHATBOT_VIDEO_ENABLED", False):
            self.assertFalse(video_understanding.chatbot_video_is_on())

    def test_db_override_on(self) -> None:
        with patch.object(video_understanding, "CHATBOT_VIDEO_ENABLED", False):
            db.set_setting("chatbot_video_enabled", "on")
            self.assertTrue(video_understanding.chatbot_video_is_on())

    def test_db_override_off(self) -> None:
        with patch.object(video_understanding, "CHATBOT_VIDEO_ENABLED", True):
            db.set_setting("chatbot_video_enabled", "off")
            self.assertFalse(video_understanding.chatbot_video_is_on())


class EnvBoolParsingTests(unittest.TestCase):
    def test_false_values(self) -> None:
        with patch.dict(os.environ, {"TEST_BOOL": "false"}, clear=False):
            self.assertFalse(video_understanding._env_bool("TEST_BOOL", True))

    def test_true_values(self) -> None:
        with patch.dict(os.environ, {"TEST_BOOL": "yes"}, clear=False):
            self.assertTrue(video_understanding._env_bool("TEST_BOOL", False))


if __name__ == "__main__":
    unittest.main()
