import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import db
import smart_memory
from ai_parser import CHATBOT_SYSTEM_PROMPT, build_chat_payload, build_chat_system_prompt


class TempDbTestCase(unittest.TestCase):
    """Base class that gives each test an isolated SQLite database."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.temp_dir.name, "test-memory.db")
        db.init()

    def tearDown(self) -> None:
        import gc
        gc.collect()
        db.DB_PATH = self.original_db_path
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass


class ServerContextCrudTests(TempDbTestCase):
    def test_get_returns_empty_for_unknown_guild(self) -> None:
        self.assertEqual(db.get_server_context(99999), "")

    def test_set_and_get_round_trip(self) -> None:
        db.set_server_context(12345, "- Speak in English\n- Keep replies short")
        ctx = db.get_server_context(12345)
        self.assertIn("Speak in English", ctx)
        self.assertIn("Keep replies short", ctx)

    def test_set_overwrites_existing(self) -> None:
        db.set_server_context(12345, "- Old rule")
        db.set_server_context(12345, "- New rule")
        ctx = db.get_server_context(12345)
        self.assertIn("New rule", ctx)
        self.assertNotIn("Old rule", ctx)

    def test_set_enforces_character_limit(self) -> None:
        long_text = "- " + ("x" * 2000)
        db.set_server_context(12345, long_text)
        ctx = db.get_server_context(12345)
        self.assertLessEqual(len(ctx), db.SERVER_CONTEXT_MAX_CHARS)

    def test_clear_server_context(self) -> None:
        db.set_server_context(12345, "- Some rule")
        self.assertTrue(db.clear_server_context(12345))
        self.assertEqual(db.get_server_context(12345), "")
        self.assertFalse(db.clear_server_context(12345))

    def test_all_server_contexts_excludes_empty(self) -> None:
        db.set_server_context(1, "- Rule A")
        db.set_server_context(2, "")
        rows = db.all_server_contexts()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["guild_id"], 1)


class ContextFormattingTests(unittest.TestCase):
    def test_format_guild_memory_language(self) -> None:
        row = {
            "memory_key": "language_preference",
            "memory_value": "English",
        }
        self.assertIn("Speak in English", db._format_guild_memory_row(row))  # noqa: SLF001

    def test_format_user_memory_portfolio(self) -> None:
        row = {
            "user_id": 111,
            "memory_key": "portfolio_link",
            "memory_value": "https://example.com",
            "context": "",
        }
        text = db._format_user_memory_row(row)  # noqa: SLF001
        self.assertIn("<@111>", text)
        self.assertIn("https://example.com", text)

    def test_format_user_memory_nickname(self) -> None:
        row = {
            "user_id": 111,
            "memory_key": "nickname_preference",
            "memory_value": "DOY",
            "context": "222",
        }
        text = db._format_user_memory_row(row)  # noqa: SLF001
        self.assertIn("<@222>", text)
        self.assertIn("DOY", text)

    def test_merge_context_bullets_dedupes(self) -> None:
        merged = db._merge_context_bullets(  # noqa: SLF001
            ["Speak in English", "Keep it short"],
            ["Speak in English", "New rule"],
        )
        self.assertEqual(merged.count("Speak in English"), 1)
        self.assertIn("New rule", merged)
        self.assertTrue(merged.startswith("- "))

    def test_trim_context_prefers_complete_lines(self) -> None:
        lines = "\n".join(f"- line {i}" for i in range(200))
        trimmed = db.trim_context_to_limit(lines, limit=50)
        self.assertLessEqual(len(trimmed), 50)
        self.assertTrue(trimmed.startswith("- line"))


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = db.DB_PATH
        self.original_guild_id = os.environ.get("GUILD_ID")
        db.DB_PATH = os.path.join(self.temp_dir.name, "migrate-test.db")

    def tearDown(self) -> None:
        db.DB_PATH = self.original_db_path
        if self.original_guild_id is None:
            os.environ.pop("GUILD_ID", None)
        else:
            os.environ["GUILD_ID"] = self.original_guild_id
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            pass

    def _seed_legacy_db(self) -> None:
        conn = sqlite3.connect(db.DB_PATH)
        now = "2026-01-01T12:00:00"
        conn.execute(
            """CREATE TABLE guild_memory (
                guild_id INTEGER NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, memory_key, context)
            )"""
        )
        conn.execute(
            """CREATE TABLE user_memory (
                user_id INTEGER NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, memory_key, context)
            )"""
        )
        conn.execute(
            """INSERT INTO guild_memory VALUES (?, ?, ?, ?, ?, ?)""",
            (55555, "language_preference", "Tagalog", "", now, now),
        )
        conn.execute(
            """INSERT INTO user_memory VALUES (?, ?, ?, ?, ?, ?)""",
            (111, "portfolio_link", "https://dev.example.io", "", now, now),
        )
        conn.execute(
            """INSERT INTO user_memory VALUES (?, ?, ?, ?, ?, ?)""",
            (111, "nickname_preference", "DOY", "222", now, now),
        )
        conn.commit()
        conn.close()

    def test_migration_folds_legacy_data_into_server_context(self) -> None:
        self._seed_legacy_db()
        db.init()

        ctx = db.get_server_context(55555)
        self.assertIn("Tagalog", ctx)
        self.assertIn("https://dev.example.io", ctx)
        self.assertIn("DOY", ctx)
        self.assertIn("<@222>", ctx)

        conn = sqlite3.connect(db.DB_PATH)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        self.assertNotIn("user_memory", tables)
        self.assertNotIn("guild_memory", tables)
        self.assertIn("server_context", tables)

    def test_migration_duplicates_user_notes_to_all_known_guilds(self) -> None:
        self._seed_legacy_db()
        conn = sqlite3.connect(db.DB_PATH)
        now = "2026-01-01T12:00:00"
        conn.execute(
            """INSERT INTO guild_memory VALUES (?, ?, ?, ?, ?, ?)""",
            (66666, "custom_note", "Be friendly", "", now, now),
        )
        conn.commit()
        conn.close()

        db.init()

        for guild_id in (55555, 66666):
            ctx = db.get_server_context(guild_id)
            self.assertIn("https://dev.example.io", ctx, f"guild {guild_id}")

    def test_migration_is_idempotent(self) -> None:
        self._seed_legacy_db()
        db.init()
        first = db.get_server_context(55555)
        db.init()
        second = db.get_server_context(55555)
        self.assertEqual(first, second)


class SystemPromptTests(TempDbTestCase):
    def test_build_chat_system_prompt_without_guild(self) -> None:
        prompt = build_chat_system_prompt(CHATBOT_SYSTEM_PROMPT, guild_id=None)
        self.assertEqual(prompt, CHATBOT_SYSTEM_PROMPT)

    def test_build_chat_system_prompt_includes_server_context(self) -> None:
        db.set_server_context(77777, "- Always reply in Japanese")
        prompt = build_chat_system_prompt(CHATBOT_SYSTEM_PROMPT, guild_id=77777)
        self.assertIn("SERVER CONTEXT", prompt)
        self.assertIn("Always reply in Japanese", prompt)
        self.assertIn(CHATBOT_SYSTEM_PROMPT, prompt)

    def test_build_chat_payload_has_no_user_context_slot(self) -> None:
        payload = build_chat_payload(
            message_text="hello",
            author_name="Alice",
            bot_name="Bot",
            context_lines=["Bob: hi"],
        )
        self.assertIn("Alice", payload)
        self.assertIn("Recent conversation", payload)
        self.assertNotIn("User context:", payload)
        self.assertNotIn("Server context:", payload)


class DurableInstructionGateTests(unittest.TestCase):
    def test_server_language_instruction_matches(self) -> None:
        self.assertTrue(
            smart_memory.might_contain_durable_instruction(
                "Everyone speak English in this server from now on",
                "Sure!",
            )
        )

    def test_casual_chat_skipped(self) -> None:
        self.assertFalse(
            smart_memory.might_contain_durable_instruction("haha ok thanks", "welcome")
        )

    def test_remember_portfolio_matches(self) -> None:
        self.assertTrue(
            smart_memory.might_contain_durable_instruction(
                "tandaan mo portfolio ko https://example.com for this server",
                "Noted!",
            )
        )

    def test_nickname_instruction_matches(self) -> None:
        self.assertTrue(
            smart_memory.might_contain_durable_instruction(
                "call <@123456789> as DOY from now on",
                "Got it!",
            )
        )


class ServerContextUpdaterTests(TempDbTestCase):
    @patch("smart_memory._rewrite_server_context", new_callable=AsyncMock)
    def test_maybe_update_skips_casual_chat(self, mock_rewrite: AsyncMock) -> None:
        result = asyncio.run(
            smart_memory.maybe_update_server_context(
                guild_id=12345,
                user_message="lol nice",
                bot_reply="haha",
                author_name="TestUser",
            )
        )
        self.assertFalse(result)
        mock_rewrite.assert_not_called()

    @patch("smart_memory._rewrite_server_context", new_callable=AsyncMock)
    def test_maybe_update_skips_without_guild(self, mock_rewrite: AsyncMock) -> None:
        result = asyncio.run(
            smart_memory.maybe_update_server_context(
                guild_id=None,
                user_message="speak English everyone",
                bot_reply="ok",
                author_name="TestUser",
            )
        )
        self.assertFalse(result)
        mock_rewrite.assert_not_called()

    @patch("smart_memory._rewrite_server_context", new_callable=AsyncMock)
    def test_maybe_update_persists_when_rewrite_returns_new_context(
        self, mock_rewrite: AsyncMock
    ) -> None:
        mock_rewrite.return_value = "- Speak in English for this server"
        result = asyncio.run(
            smart_memory.maybe_update_server_context(
                guild_id=12345,
                user_message="Everyone speak English in this server",
                bot_reply="Sure!",
                author_name="Admin",
            )
        )
        self.assertTrue(result)
        ctx = db.get_server_context(12345)
        self.assertIn("Speak in English", ctx)

    @patch("smart_memory._rewrite_server_context", new_callable=AsyncMock)
    def test_maybe_update_no_op_when_rewrite_unchanged(
        self, mock_rewrite: AsyncMock
    ) -> None:
        existing = "- Existing rule"
        db.set_server_context(12345, existing)
        mock_rewrite.return_value = existing
        result = asyncio.run(
            smart_memory.maybe_update_server_context(
                guild_id=12345,
                user_message="Everyone speak English in this server",
                bot_reply="Sure!",
                author_name="Admin",
            )
        )
        self.assertFalse(result)

    @patch("ai_parser._client_for")
    @patch("ai_parser.chat_keys", return_value=["fake-key"])
    def test_rewrite_server_context_parses_llm_json(
        self, _mock_keys: MagicMock, mock_client_for: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"update": true, "context": "- Speak in Tagalog\\n- Keep replies brief"}'
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_client_for.return_value = mock_client

        result = asyncio.run(
            smart_memory._rewrite_server_context(  # noqa: SLF001
                current_context="",
                user_message="Speak Tagalog in this server",
                bot_reply="Sige!",
                author_name="User",
            )
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("Tagalog", result)
        self.assertIn("brief", result)

    @patch("ai_parser._client_for")
    @patch("ai_parser.chat_keys", return_value=["fake-key"])
    def test_rewrite_server_context_respects_no_update(
        self, _mock_keys: MagicMock, mock_client_for: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"update": false}'
        mock_client = MagicMock()
        mock_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
        mock_client_for.return_value = mock_client

        result = asyncio.run(
            smart_memory._rewrite_server_context(  # noqa: SLF001
                current_context="- Existing",
                user_message="haha ok",
                bot_reply="lol",
                author_name="User",
            )
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
