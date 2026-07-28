"""SQLite storage for the bot: debts, reminders, settings, an edit trail,
and the Google Calendar sync bookkeeping (linked calendars + event mappings).

Everything lives in one file (debts.db) so the ledger survives reboots and
power cuts on the Pi. The traffic here is tiny (a personal server), so the
standard-library sqlite3 module is more than enough - no ORM needed.
"""

import os
import re
import sqlite3
from datetime import datetime

# The database sits next to this file unless DB_PATH says otherwise.
DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "debts.db"),
)


def _connect() -> sqlite3.Connection:
    """Open a connection with dict-like row access."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def _now_iso() -> str:
    """Current local time as a sortable ISO string."""
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column to an existing table if it isn't there yet.

    Lets an older debts.db pick up new fields without losing any data - the
    existing rows just get NULL for the new column.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init() -> None:
    """Create tables on first run. Safe to call every startup."""
    with _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS debts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL CHECK (direction IN ('they_owe_me', 'i_owe_them')),
                person_name TEXT NOT NULL,        -- who the debt is with
                person_id INTEGER,                -- their Discord ID, if known
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT '₱',
                description TEXT NOT NULL DEFAULT '',
                channel_id INTEGER,               -- where it was recorded (for server-mode reminders)
                source_message_id INTEGER,        -- chat message that created it (duplicate guard)
                due_date TEXT,                    -- YYYY-MM-DD promised pay date, if any
                created_at TEXT NOT NULL,
                paid_at TEXT,                     -- NULL = still unpaid
                last_reminded TEXT                -- when we last nagged about it
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_text TEXT NOT NULL,
                due_at TEXT NOT NULL,             -- YYYY-MM-DDTHH:MM local time
                requester_name TEXT NOT NULL,     -- who asked for the reminder
                requester_id INTEGER,
                channel_id INTEGER,
                source_message_id INTEGER,
                created_at TEXT NOT NULL,
                delivered_at TEXT,                -- NULL = not yet fired
                repeat_rule TEXT                  -- NULL = one-off; else daily/weekly/monthly/yearly
            )"""
        )
        # Databases created before repeating reminders existed need the new
        # column added in place - CREATE TABLE IF NOT EXISTS won't do it.
        _ensure_column(conn, "reminders", "repeat_rule", "TEXT")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS edit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_type TEXT NOT NULL,        -- 'debt' or 'reminder'
                record_id INTEGER NOT NULL,
                field TEXT NOT NULL,              -- what changed (due_date, amount, paid...)
                old_value TEXT,
                new_value TEXT,
                source_message_id INTEGER,        -- chat message that caused the change, if any
                changed_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS linked_calendars (
                user_id INTEGER PRIMARY KEY,      -- one calendar per Discord user; re-link replaces
                user_name TEXT,                   -- for logs and /calendar status
                calendar_id TEXT NOT NULL,        -- Google Calendar ID they shared with the bot
                linked_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS event_sync (
                discord_event_id INTEGER NOT NULL,
                calendar_id TEXT NOT NULL,        -- which Google Calendar this copy lives on
                gcal_event_id TEXT NOT NULL,      -- Google's id for that copy
                guild_id INTEGER NOT NULL,        -- server the Discord event belongs to
                event_name TEXT,                  -- for logs
                start_time TEXT,                  -- UTC ISO; decides history-vs-remove on delete
                content_hash TEXT,                -- lets reconciliation skip no-op updates
                last_synced_at TEXT,
                PRIMARY KEY (discord_event_id, calendar_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_memory (
                user_id INTEGER NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                context TEXT NOT NULL DEFAULT '',  -- optional: what this applies to (e.g., target user_id for nicknames)
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, memory_key, context)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS raffles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prize_description TEXT NOT NULL,
                prize_amount REAL NOT NULL DEFAULT 0.0,
                currency TEXT NOT NULL DEFAULT '₱',
                creator_name TEXT NOT NULL,
                creator_id INTEGER,
                channel_id INTEGER,
                guild_id INTEGER,
                source_message_id INTEGER,
                created_at TEXT NOT NULL,
                ends_at TEXT,                    -- When raffle ends
                ended_at TEXT,                   -- When actually ended
                winner_id INTEGER,               -- Winner's Discord ID
                winner_name TEXT,                -- Winner's display name
                max_participants INTEGER,        -- NULL = unlimited
                auto_join_role_id INTEGER,       -- Role ID for automatic participation (NULL = disabled)
                active BOOLEAN NOT NULL DEFAULT 1
            )"""
        )
        # Ensure new columns exist for backward compatibility
        _ensure_column(conn, "raffles", "ended_at", "TEXT")
        _ensure_column(conn, "raffles", "winner_id", "INTEGER")
        _ensure_column(conn, "raffles", "winner_name", "TEXT")
        # Migrate from entry_cost to prize_amount
        # Use index 1 (column name) from PRAGMA – works even if row_factory isn't set.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(raffles)")}
        if "entry_cost" in existing_cols and "prize_amount" not in existing_cols:
            conn.execute("ALTER TABLE raffles RENAME COLUMN entry_cost TO prize_amount")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS raffle_participants (
                raffle_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                joined_via TEXT NOT NULL DEFAULT 'command',  -- 'command', 'role', 'natural_language'
                PRIMARY KEY (raffle_id, user_id),
                FOREIGN KEY (raffle_id) REFERENCES raffles(id) ON DELETE CASCADE
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS user_balances (
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                balance REAL NOT NULL DEFAULT 0.0,
                currency TEXT NOT NULL DEFAULT '₱',
                PRIMARY KEY (user_id, guild_id)
            )"""
        )


# ---------------------------------------------------------------------------
# Debts
# ---------------------------------------------------------------------------

def add_debt(
    direction: str,
    person_name: str,
    amount: float,
    currency: str,
    description: str = "",
    person_id: int | None = None,
    channel_id: int | None = None,
    source_message_id: int | None = None,
    due_date: str | None = None,
) -> int:
    """Insert a new debt and return its id."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO debts (direction, person_name, person_id, amount, currency,
                                  description, channel_id, source_message_id, due_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                direction, person_name, person_id, amount, currency,
                description, channel_id, source_message_id, due_date, _now_iso(),
            ),
        )
        return cursor.lastrowid


def _open_debts_matching(
    conn: sqlite3.Connection, person_name: str, person_id: int | None
) -> list[sqlite3.Row]:
    """Find unpaid debts for a person, trying exact matches before loose ones.

    The AI might say "Alex" while the ledger says "alexsmith" (or the other
    way around), so after ID and exact-name matching we fall back to
    substring matching in both directions.
    """
    if person_id:
        rows = conn.execute(
            "SELECT * FROM debts WHERE paid_at IS NULL AND person_id = ? ORDER BY created_at DESC",
            (person_id,),
        ).fetchall()
        if rows:
            return rows
    rows = conn.execute(
        "SELECT * FROM debts WHERE paid_at IS NULL AND lower(person_name) = lower(?) "
        "ORDER BY created_at DESC",
        (person_name,),
    ).fetchall()
    if rows:
        return rows
    return conn.execute(
        """SELECT * FROM debts WHERE paid_at IS NULL AND
           (lower(person_name) LIKE '%' || lower(?) || '%'
            OR lower(?) LIKE '%' || lower(person_name) || '%')
           ORDER BY created_at DESC""",
        (person_name, person_name),
    ).fetchall()


def open_debts_for(person_name: str, person_id: int | None = None) -> list[sqlite3.Row]:
    """All unpaid debts with this person, newest first."""
    with _connect() as conn:
        return _open_debts_matching(conn, person_name, person_id)


def latest_open_debt(person_name: str, person_id: int | None = None) -> sqlite3.Row | None:
    """The most recent unpaid debt with this person, or None."""
    rows = open_debts_for(person_name, person_id)
    return rows[0] if rows else None


def get_debt(debt_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM debts WHERE id = ?", (debt_id,)).fetchone()


def mark_paid(debt_id: int, source_message_id: int | None = None) -> None:
    """Settle a debt and note it in the edit trail."""
    paid_time = _now_iso()
    with _connect() as conn:
        conn.execute("UPDATE debts SET paid_at = ? WHERE id = ?", (paid_time, debt_id))
    log_edit("debt", debt_id, "paid_at", None, paid_time, source_message_id)


def update_debt_field(
    debt_id: int, field: str, new_value: str | float, source_message_id: int | None = None
) -> None:
    """Change a debt's due_date or amount, recording old -> new in the edit trail."""
    if field not in ("due_date", "amount"):
        raise ValueError(f"Refusing to update unexpected debt field: {field}")
    with _connect() as conn:
        old_row = conn.execute("SELECT * FROM debts WHERE id = ?", (debt_id,)).fetchone()
        old_value = old_row[field] if old_row else None
        conn.execute(f"UPDATE debts SET {field} = ? WHERE id = ?", (new_value, debt_id))
    log_edit("debt", debt_id, field, old_value, str(new_value), source_message_id)


def mark_debt_reminded(debt_id: int, when_iso: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE debts SET last_reminded = ? WHERE id = ?", (when_iso, debt_id))


def unpaid_debts() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM debts WHERE paid_at IS NULL ORDER BY created_at"
        ).fetchall()


def list_debts(include_paid: bool = False) -> list[sqlite3.Row]:
    with _connect() as conn:
        if include_paid:
            return conn.execute(
                "SELECT * FROM debts ORDER BY paid_at IS NOT NULL, created_at"
            ).fetchall()
        return conn.execute(
            "SELECT * FROM debts WHERE paid_at IS NULL ORDER BY created_at"
        ).fetchall()


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

def add_reminder(
    reminder_text: str,
    due_at: str,
    requester_name: str,
    requester_id: int | None = None,
    channel_id: int | None = None,
    source_message_id: int | None = None,
    repeat_rule: str | None = None,
) -> int:
    """Insert a new reminder and return its id.

    repeat_rule is None for a one-off, or daily/weekly/monthly/yearly for one
    that reschedules itself after each delivery.
    """
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO reminders (reminder_text, due_at, requester_name, requester_id,
                                      channel_id, source_message_id, created_at, repeat_rule)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (reminder_text, due_at, requester_name, requester_id,
             channel_id, source_message_id, _now_iso(), repeat_rule),
        )
        return cursor.lastrowid


def reschedule_reminder(reminder_id: int, new_due_at: str) -> None:
    """Move a repeating reminder to its next occurrence, leaving it pending."""
    with _connect() as conn:
        conn.execute(
            "UPDATE reminders SET due_at = ?, delivered_at = NULL WHERE id = ?",
            (new_due_at, reminder_id),
        )


def get_reminder(reminder_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()


def pending_reminders() -> list[sqlite3.Row]:
    """All reminders that have not fired yet, soonest first."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE delivered_at IS NULL ORDER BY due_at"
        ).fetchall()


def due_reminders(now_iso: str) -> list[sqlite3.Row]:
    """Reminders whose time has arrived (ISO strings sort chronologically)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE delivered_at IS NULL AND due_at <= ? ORDER BY due_at",
            (now_iso,),
        ).fetchall()


def mark_reminder_delivered(reminder_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE reminders SET delivered_at = ? WHERE id = ?", (_now_iso(), reminder_id)
        )


def update_reminder_due(
    reminder_id: int, new_due_at: str, source_message_id: int | None = None
) -> None:
    """Move a pending reminder to a new time, recording the change."""
    with _connect() as conn:
        old_row = conn.execute(
            "SELECT due_at FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        old_value = old_row["due_at"] if old_row else None
        conn.execute("UPDATE reminders SET due_at = ? WHERE id = ?", (new_due_at, reminder_id))
    log_edit("reminder", reminder_id, "due_at", old_value, new_due_at, source_message_id)


def delete_reminder(reminder_id: int) -> bool:
    """Remove a reminder entirely. Returns True if something was deleted."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return cursor.rowcount > 0


def latest_pending_reminder_for(
    requester_name: str | None, requester_id: int | None
) -> sqlite3.Row | None:
    """Most recent pending reminder from a person - used by 'update' events."""
    with _connect() as conn:
        if requester_id:
            row = conn.execute(
                "SELECT * FROM reminders WHERE delivered_at IS NULL AND requester_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (requester_id,),
            ).fetchone()
            if row:
                return row
        if requester_name:
            return conn.execute(
                "SELECT * FROM reminders WHERE delivered_at IS NULL AND "
                "lower(requester_name) LIKE '%' || lower(?) || '%' "
                "ORDER BY created_at DESC LIMIT 1",
                (requester_name,),
            ).fetchone()
        return None


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------

def _word_overlap_ratio(text_a: str, text_b: str) -> float:
    """Rough similarity between two short texts: shared words / smaller word set.

    Deliberately simple (no external NLP dependency) - good enough to catch
    "paalala bukas bayad jersey" vs "wag kalimutan bukas jersey payment".
    """
    words_a = set(re.findall(r"\w+", text_a.lower()))
    words_b = set(re.findall(r"\w+", text_b.lower()))
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def find_similar_reminder(due_date: str | None, reminder_text: str) -> sqlite3.Row | None:
    """A pending reminder on the same date with clearly overlapping wording."""
    for existing in pending_reminders():
        same_day = due_date is not None and existing["due_at"].startswith(due_date)
        similar_text = _word_overlap_ratio(existing["reminder_text"], reminder_text) >= 0.5
        if same_day and similar_text:
            return existing
        # Very high wording overlap counts even if the dates differ slightly
        # (people often re-state a reminder with a corrected date).
        if _word_overlap_ratio(existing["reminder_text"], reminder_text) >= 0.8:
            return existing
    return None


def find_similar_debt(
    person_name: str, person_id: int | None, amount: float | None
) -> sqlite3.Row | None:
    """An open debt with the same person for the same amount = likely duplicate."""
    if amount is None:
        return None
    for existing in open_debts_for(person_name, person_id):
        if abs(existing["amount"] - amount) < 0.01:
            return existing
    return None


# ---------------------------------------------------------------------------
# Edit trail + settings
# ---------------------------------------------------------------------------

def log_edit(
    record_type: str,
    record_id: int,
    field: str,
    old_value: str | None,
    new_value: str | None,
    source_message_id: int | None = None,
) -> None:
    """Append one change to the audit trail (never deleted)."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO edit_log (record_type, record_id, field, old_value, new_value,
                                     source_message_id, changed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (record_type, record_id, field,
             None if old_value is None else str(old_value),
             None if new_value is None else str(new_value),
             source_message_id, _now_iso()),
        )


def get_setting(key: str, default: str) -> str:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Google Calendar sync (see calendar_sync.py)
# linked_calendars = who wants events on which calendar.
# event_sync = which Discord event became which Google event, per calendar.
# ---------------------------------------------------------------------------

def get_linked_calendar(user_id: int) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM linked_calendars WHERE user_id = ?", (user_id,)
        ).fetchone()


def save_linked_calendar(user_id: int, user_name: str, calendar_id: str) -> None:
    """Link (or re-link) a user's calendar - one per user, newest wins."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO linked_calendars (user_id, user_name, calendar_id, linked_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "user_name = excluded.user_name, calendar_id = excluded.calendar_id, "
            "linked_at = excluded.linked_at",
            (user_id, user_name, calendar_id, _now_iso()),
        )


def delete_linked_calendar(user_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM linked_calendars WHERE user_id = ?", (user_id,))


def all_linked_calendars() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM linked_calendars ORDER BY linked_at").fetchall()


def get_event_sync(discord_event_id: int, calendar_id: str) -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM event_sync WHERE discord_event_id = ? AND calendar_id = ?",
            (discord_event_id, calendar_id),
        ).fetchone()


def save_event_sync(
    discord_event_id: int,
    calendar_id: str,
    gcal_event_id: str,
    guild_id: int,
    event_name: str,
    start_time: str,
    content_hash: str,
) -> None:
    """Record (or refresh) one Discord-event -> Google-event mapping."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO event_sync (discord_event_id, calendar_id, gcal_event_id,
                                       guild_id, event_name, start_time, content_hash,
                                       last_synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(discord_event_id, calendar_id) DO UPDATE SET
                 gcal_event_id = excluded.gcal_event_id,
                 event_name = excluded.event_name,
                 start_time = excluded.start_time,
                 content_hash = excluded.content_hash,
                 last_synced_at = excluded.last_synced_at""",
            (discord_event_id, calendar_id, gcal_event_id, guild_id,
             event_name, start_time, content_hash, _now_iso()),
        )


def delete_event_sync(discord_event_id: int, calendar_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM event_sync WHERE discord_event_id = ? AND calendar_id = ?",
            (discord_event_id, calendar_id),
        )


def event_syncs_for_event(discord_event_id: int) -> list[sqlite3.Row]:
    """Every calendar copy of one Discord event (used when it's cancelled)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM event_sync WHERE discord_event_id = ?", (discord_event_id,)
        ).fetchall()


def event_syncs_for_calendar(calendar_id: str) -> list[sqlite3.Row]:
    """Every event copy on one calendar (used by /calendar unlink cleanup)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM event_sync WHERE calendar_id = ?", (calendar_id,)
        ).fetchall()


def all_event_syncs() -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM event_sync").fetchall()


# ---------------------------------------------------------------------------
# User Memory (context, preferences, nicknames)
# ---------------------------------------------------------------------------

def save_user_memory(
    user_id: int,
    memory_key: str,
    memory_value: str,
    context: str = ""
) -> None:
    """Store or update a user-specific memory.

    Args:
        user_id: Discord user ID who owns this memory
        memory_key: Type of memory (e.g., "nickname_preference", "language_preference")
        memory_value: The actual value (e.g., "DOY", "Tagalog")
        context: Optional context (e.g., target user_id for nicknames)
    """
    with _connect() as conn:
        now = _now_iso()
        conn.execute(
            """INSERT INTO user_memory (user_id, memory_key, memory_value, context, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, memory_key, context) DO UPDATE SET
                 memory_value = excluded.memory_value,
                 updated_at = excluded.updated_at""",
            (user_id, memory_key, memory_value, context, now, now),
        )


def get_user_memory(user_id: int, memory_key: str, context: str = "") -> str | None:
    """Retrieve a specific memory for a user."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT memory_value FROM user_memory WHERE user_id = ? AND memory_key = ? AND context = ?",
            (user_id, memory_key, context),
        ).fetchone()
        return row["memory_value"] if row else None


def get_all_user_memories(user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    """Get recent memories for a user (limited to save tokens).

    Returns most recently updated memories first.
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM user_memory WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def delete_user_memory(user_id: int, memory_key: str, context: str = "") -> bool:
    """Delete a specific memory. Returns True if something was deleted."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM user_memory WHERE user_id = ? AND memory_key = ? AND context = ?",
            (user_id, memory_key, context),
        )
        return cursor.rowcount > 0


def build_user_context(user_id: int, mentioned_user_ids: list[int] | None = None) -> str:
    """Build a compact context string from relevant memories.

    Only includes memories relevant to the current conversation to minimize tokens.
    Returns empty string if no relevant memories exist.
    """
    memories = []

    # Get user's general preferences first
    all_memories = get_all_user_memories(user_id, limit=20)

    for mem in all_memories:
        if mem["memory_key"] == "nickname_preference":
            # Only include nickname if that user is mentioned in this conversation
            target_id_str = mem["context"]
            if not mentioned_user_ids or int(target_id_str) in mentioned_user_ids:
                memories.append(f"Call <@{target_id_str}> as '{mem['memory_value']}'")
        elif mem["memory_key"] == "portfolio_link":
            memories.append(f"User's portfolio: {mem['memory_value']}")
        elif mem["memory_key"] == "github_link":
            memories.append(f"User's GitHub: {mem['memory_value']}")
        elif mem["memory_key"] == "linkedin_link":
            memories.append(f"User's LinkedIn: {mem['memory_value']}")
        elif mem["memory_key"] == "work_link":
            memories.append(f"User's work link: {mem['memory_value']}")
        elif mem["memory_key"] == "work_role":
            memories.append(f"User works as: {mem['memory_value']}")
        elif mem["memory_key"] == "user_preference":
            memories.append(f"User prefers: {mem['memory_value']}")
        elif mem["memory_key"] == "language_preference":
            memories.append(f"User prefers {mem['memory_value']} language")
        elif mem["memory_key"] == "formality_level":
            memories.append(f"Use {mem['memory_value']} tone with this user")
        elif mem["memory_key"] == "custom_note":
            # Generic notes about the user's preferences or context
            memories.append(mem["memory_value"])

    if not memories:
        return ""

    # Hard limit to 20 items to keep token usage reasonable
    return "User context:\n" + "\n".join(f"- {m}" for m in memories[:20])


# ---------------------------------------------------------------------------
# Raffles
# ---------------------------------------------------------------------------


def create_raffle(
    prize_description: str,
    prize_amount: float,
    currency: str,
    creator_name: str,
    creator_id: int,
    channel_id: int,
    guild_id: int,
    source_message_id: int | None = None,
    ends_at: str | None = None,
    max_participants: int | None = None,
    auto_join_role_id: int | None = None,
    active: bool = True,
) -> int:
    """Insert a new raffle and return its id."""
    with _connect() as conn:
        cursor = conn.execute(
            """INSERT INTO raffles (
                prize_description, prize_amount, currency, creator_name, creator_id,
                channel_id, guild_id, source_message_id, created_at, ends_at,
                max_participants, auto_join_role_id, active
            )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prize_description, prize_amount, currency, creator_name, creator_id,
                channel_id, guild_id, source_message_id, _now_iso(), ends_at,
                max_participants, auto_join_role_id, active,
            ),
        )
        return cursor.lastrowid


def get_raffle(raffle_id: int) -> sqlite3.Row | None:
    """Get a raffle by its ID."""
    with _connect() as conn:
        return conn.execute("SELECT * FROM raffles WHERE id = ?", (raffle_id,)).fetchone()


def _raffle_prize(raffle: sqlite3.Row) -> float:
    """Safely read prize_amount, falling back to entry_cost for unmigrated databases."""
    try:
        return raffle["prize_amount"]
    except KeyError:
        return raffle["entry_cost"]


def get_active_raffles(channel_id: int | None = None, guild_id: int | None = None) -> list[sqlite3.Row]:
    """Get active raffles, optionally filtered by channel and/or guild."""
    with _connect() as conn:
        if channel_id is not None and guild_id is not None:
            return conn.execute(
                "SELECT * FROM raffles WHERE active = 1 AND channel_id = ? AND guild_id = ? ORDER BY created_at",
                (channel_id, guild_id),
            ).fetchall()
        elif channel_id is not None:
            return conn.execute(
                "SELECT * FROM raffles WHERE active = 1 AND channel_id = ? ORDER BY created_at",
                (channel_id,),
            ).fetchall()
        elif guild_id is not None:
            return conn.execute(
                "SELECT * FROM raffles WHERE active = 1 AND guild_id = ? ORDER BY created_at",
                (guild_id,),
            ).fetchall()
        else:
            return conn.execute(
                "SELECT * FROM raffles WHERE active = 1 ORDER BY created_at"
            ).fetchall()


def join_raffle(
    raffle_id: int,
    user_id: int,
    user_name: str,
    joined_via: str = "command",
) -> bool:
    """Add a participant to a raffle. Returns True if joined, False if already participated."""
    with _connect() as conn:
        try:
            conn.execute(
                """INSERT INTO raffle_participants (raffle_id, user_id, user_name, joined_at, joined_via)
                   VALUES (?, ?, ?, ?, ?)""",
                (raffle_id, user_id, user_name, _now_iso(), joined_via),
            )
            return True
        except sqlite3.IntegrityError:
            # Already participated (UNIQUE constraint violation)
            return False


def leave_raffle(raffle_id: int, user_id: int) -> bool:
    """Remove a participant from a raffle. Returns True if removed, False if not found."""
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM raffle_participants WHERE raffle_id = ? AND user_id = ?",
            (raffle_id, user_id),
        )
        return cursor.rowcount > 0


def get_raffle_participants(raffle_id: int) -> list[sqlite3.Row]:
    """Get all participants for a raffle."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM raffle_participants WHERE raffle_id = ? ORDER BY joined_at",
            (raffle_id,),
        ).fetchall()


def end_raffle(raffle_id: int) -> dict | None:
    """End a raffle, select a random winner, and return the winner's info and raffle stats.
    Returns None if raffle has no participants or is already ended."""
    with _connect() as conn:
        # Check if raffle exists and is active
        raffle = conn.execute(
            "SELECT * FROM raffles WHERE id = ? AND active = 1", (raffle_id,)
        ).fetchone()
        if not raffle:
            return None

        # Get participants
        participants = conn.execute(
            "SELECT user_id, user_name FROM raffle_participants WHERE raffle_id = ?",
            (raffle_id,),
        ).fetchall()

        if not participants:
            # No participants, mark as ended but no winner
            conn.execute(
                "UPDATE raffles SET active = 0, ended_at = ? WHERE id = ?",
                (_now_iso(), raffle_id),
            )
            return None

        # Select random winner
        import random
        winner = random.choice(participants)

        # Prize amount is the fixed prize the creator set
        # Fallback for databases that haven't been migrated yet
        try:
            prize_amount = raffle["prize_amount"]
        except KeyError:
            prize_amount = raffle.get("entry_cost", 0.0)

        # Update raffle with winner and end time
        conn.execute(
            """UPDATE raffles
               SET active = 0, ended_at = ?, winner_id = ?, winner_name = ?
               WHERE id = ?""",
            (_now_iso(), winner["user_id"], winner["user_name"], raffle_id),
        )

        # Update raffle with winner and end time
        conn.execute(
            """UPDATE raffles
               SET active = 0, ended_at = ?, winner_id = ?, winner_name = ?
               WHERE id = ?""",
            (_now_iso(), winner["user_id"], winner["user_name"], raffle_id),
        )

        # Capture values needed after closing this connection
        winner_id = winner["user_id"]
        winner_name = winner["user_name"]
        guild_id = raffle["guild_id"]
        currency = raffle["currency"]
        participant_count = len(participants)

    # Award the prize OUTSIDE the first connection to avoid database-locked errors
    update_user_balance(
        winner_id,
        guild_id,
        prize_amount,
        currency
    )

    # Return winner info and raffle stats
    return {
        "winner_id": winner_id,
        "winner_name": winner_name,
        "prize_pool": prize_amount,
        "participant_count": participant_count
    }


def get_expired_raffles() -> list[dict]:
    """Return active raffles whose ends_at has passed (not NULL)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM raffles WHERE active = 1 AND ends_at IS NOT NULL AND ends_at <= ?",
            (_now_iso(),),
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_balance(user_id: int, guild_id: int) -> tuple[float, str]:
    """Get a user's balance for a specific guild."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT balance, currency FROM user_balances WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ).fetchone()
        if row:
            return row["balance"], row["currency"]
        else:
            # Create default balance entry if it doesn't exist
            conn.execute(
                """INSERT OR IGNORE INTO user_balances (user_id, guild_id, balance, currency)
                   VALUES (?, ?, 0.0, '₱')""",
                (user_id, guild_id),
            )
            return 0.0, "₱"


def get_top_balances(guild_id: int, limit: int = 5) -> list[sqlite3.Row]:
    """Get the top N balances in a guild, ordered highest first."""
    with _connect() as conn:
        return conn.execute(
            "SELECT user_id, balance, currency FROM user_balances WHERE guild_id = ? ORDER BY balance DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()


def get_user_balance_rank(guild_id: int, user_id: int) -> int | None:
    """Return the 1-based rank of a user in the guild leaderboard, or None if not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT balance FROM user_balances WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ).fetchone()
        if not row:
            return None
        user_balance = row["balance"]
        # Count how many users have strictly higher balance, then add 1
        count = conn.execute(
            "SELECT COUNT(*) FROM user_balances WHERE guild_id = ? AND balance > ?",
            (guild_id, user_balance),
        ).fetchone()[0]
        return count + 1


def update_user_balance(
    user_id: int,
    guild_id: int,
    amount: float,
    currency: str = "₱",
) -> bool:
    """Update a user's balance by adding/subtracting amount. Returns False if would result in negative balance."""
    with _connect() as conn:
        # First, ensure the user has a balance entry
        conn.execute(
            """INSERT OR IGNORE INTO user_balances (user_id, guild_id, balance, currency)
               VALUES (?, ?, 0.0, ?)""",
            (user_id, guild_id, currency),
        )

        # Check current balance
        current = conn.execute(
            "SELECT balance FROM user_balances WHERE user_id = ? AND guild_id = ?",
            (user_id, guild_id),
        ).fetchone()

        current_balance = current["balance"] if current else 0.0
        new_balance = current_balance + amount

        # Prevent negative balance
        if new_balance < 0:
            return False

        # Then update the balance
        conn.execute(
            """UPDATE user_balances
               SET balance = ?
               WHERE user_id = ? AND guild_id = ?""",
            (new_balance, user_id, guild_id),
        )
        return True


def get_guild_auto_join_role(guild_id: int) -> int | None:
    """Get the configured default auto-join role ID for a guild."""
    return get_setting(f"raffle_autorole_role_{guild_id}", "")


def set_guild_auto_join_role(guild_id: int, role_id: int | None) -> None:
    """Set the default auto-join role ID for a guild."""
    if role_id is None:
        # Delete the setting to disable
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM settings WHERE key = ?",
                (f"raffle_autorole_role_{guild_id}",),
            )
            conn.commit()
        finally:
            conn.close()
    else:
        set_setting(f"raffle_autorole_role_{guild_id}", str(role_id))


def get_guild_auto_join_enabled(guild_id: int) -> bool:
    """Check if role-based auto-join is enabled for a guild."""
    return get_setting(f"raffle_autorole_enabled_{guild_id}", "false") == "true"


def set_guild_auto_join_enabled(guild_id: int, enabled: bool) -> None:
    """Enable or disable role-based auto-join for a guild."""
    set_setting(f"raffle_autorole_enabled_{guild_id}", "true" if enabled else "false")


# ---------------------------------------------------------------------------
# Helper functions for processing raffles with role-based auto-join
# ---------------------------------------------------------------------------


def get_raffles_with_auto_join_role(guild_id: int, role_id: int) -> list[sqlite3.Row]:
    """Get active raffles in a guild that are configured for auto-join via a specific role."""
    with _connect() as conn:
        return conn.execute(
            """SELECT * FROM raffles
               WHERE guild_id = ? AND active = 1 AND auto_join_role_id = ?""",
            (guild_id, role_id),
        ).fetchall()


def cancel_raffle(raffle_id: int) -> bool:
    """Cancel a raffle. Returns True if cancelled."""
    with _connect() as conn:
        # Check if raffle exists and is active
        raffle = conn.execute(
            "SELECT * FROM raffles WHERE id = ? AND active = 1", (raffle_id,)
        ).fetchone()
        if not raffle:
            return False

        # Mark raffle as cancelled (not active, no winner)
        conn.execute(
            "UPDATE raffles SET active = 0, ended_at = ? WHERE id = ?",
            (_now_iso(), raffle_id),
        )

        return True


def add_role_based_participants(
    raffle_id: int,
    user_ids: list[int],
    user_names: dict[int, str],  # Maps user_id to display name
) -> int:
    """Add multiple users as participants to a raffle (for role-based auto-join).
    Returns the number of new participants added."""
    added_count = 0
    with _connect() as conn:
        for user_id in user_ids:
            user_name = user_names.get(user_id, f"User-{user_id}")
            if join_raffle(raffle_id, user_id, user_name, "role"):
                added_count += 1
    return added_count


def get_user_raffles(user_id: int, guild_id: int, channel_id: int) -> list[sqlite3.Row]:
    """Get raffles the user has joined in a specific guild and channel."""
    with _connect() as conn:
        return conn.execute(
            """SELECT r.* FROM raffles r
               JOIN raffle_participants p ON p.raffle_id = r.id
               WHERE p.user_id = ? AND r.guild_id = ? AND r.channel_id = ? AND r.active = 1
               ORDER BY r.created_at DESC""",
            (user_id, guild_id, channel_id),
        ).fetchall()


def get_raffle_participant(raffle_id: int, user_id: int) -> sqlite3.Row | None:
    """Get a specific participant in a raffle, or None if not participating."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM raffle_participants WHERE raffle_id = ? AND user_id = ?",
            (raffle_id, user_id),
        ).fetchone()
