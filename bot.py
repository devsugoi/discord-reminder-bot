"""Discord Reminder Bot.

Watches server chat (Tagalog/English) for debts and reminders, asks the owner
to confirm every detection via DM buttons, keeps a SQLite ledger, and delivers
reminders on schedule. See README.md for the full picture.

Pipeline for every overheard message:
    prescan (free regex) -> scope gate -> hot-window check -> one Gemini call
    -> duplicate check -> Confirm/Ignore DM -> database

Messages that @mention the bot take a shorter path: the same prescan and
Gemini call, but a reminder asked for directly is saved immediately and
answered in the channel (a debt still needs the owner's Confirm). Anything
that isn't a request just gets a conversational reply.

Separately, Discord *scheduled events* in allowed servers are mirrored to
linked Google Calendars - created, updated, and removed automatically as the
Discord event changes (see calendar_sync.py).
"""

import logging
import os
import re
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Load .env BEFORE importing our own modules that read env vars at import time.
load_dotenv()

import ai_parser  # noqa: E402
import calendar_sync  # noqa: E402
import db  # noqa: E402
import smart_memory  # noqa: E402
import web_search  # noqa: E402
from ai_parser import (  # noqa: E402
    CHATBOT_FALLBACK_MODELS,
    CHATBOT_MODEL,
    GEMINI_MODEL,
    GEMINI_FALLBACK_MODELS,
    ChatAnalysis,
    analyze_message,
    build_payload,
    chat_reply,
)
from detection import prescan  # noqa: E402

logger = logging.getLogger("reminderbot")

# ---------------------------------------------------------------------------
# Configuration (all documented in .env.example)
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GUILD_ID = os.getenv("GUILD_ID", "").strip()

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))
MAX_AI_CALLS_PER_DAY = int(os.getenv("MAX_AI_CALLS_PER_DAY", "200"))
CONTEXT_MESSAGES = int(os.getenv("CONTEXT_MESSAGES", "8"))
HOT_WINDOW_MINUTES = int(os.getenv("HOT_WINDOW_MINUTES", "10"))
MAX_FOLLOWUPS_PER_WINDOW = int(os.getenv("MAX_FOLLOWUPS_PER_WINDOW", "10"))
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "₱")
DEFAULT_REMINDER_HOUR = int(os.getenv("DEFAULT_REMINDER_HOUR", "9"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Chatbot (replies when mentioned) --------------------------------------
# These are only the STARTING values - /settings chatbot changes them at
# runtime and those choices win (they persist in the database).
CHATBOT_ENABLED = os.getenv("CHATBOT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
# Its own daily cap, separate from MAX_AI_CALLS_PER_DAY: a chatty afternoon must
# never use up the budget that debt/reminder detection depends on.
CHATBOT_MAX_CALLS_PER_DAY = int(os.getenv("CHATBOT_MAX_CALLS_PER_DAY", "200"))
# Seconds one person must wait between pings. 0 = no cooldown (the default).
CHATBOT_COOLDOWN_SECONDS = int(os.getenv("CHATBOT_COOLDOWN_SECONDS", "0"))

# --- Google Calendar sync (optional - see calendar_sync.py) ----------------
# The owner's calendar id straight from .env; friends add theirs at runtime
# with /calendar link (stored in the database, not here).
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
# Servers whose scheduled events get synced. Empty = just the main server.
_raw_calendar_guilds = os.getenv("CALENDAR_GUILD_IDS", "").strip()
if _raw_calendar_guilds:
    CALENDAR_GUILD_IDS = {int(part) for part in _raw_calendar_guilds.split(",") if part.strip()}
elif GUILD_ID:
    CALENDAR_GUILD_IDS = {int(GUILD_ID)}
else:
    CALENDAR_GUILD_IDS: set[int] = set()

# How long a Confirm/Ignore prompt stays clickable (seconds).
CONFIRMATION_TIMEOUT = 24 * 60 * 60

# Days between repeated nags about the same unpaid debt.
WEEKLY_NAG_DAYS = 7

_MENTION_PATTERN = re.compile(r"<@!?(\d+)>")


def _require_config() -> None:
    """Fail fast with a clear message instead of a confusing crash later."""
    missing = []
    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")
    if not OWNER_ID:
        missing.append("OWNER_ID")
    if not os.getenv("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
    if missing:
        raise SystemExit(
            f"Missing required settings in .env: {', '.join(missing)}. "
            "Copy .env.example to .env and fill them in (see README.md)."
        )
    # Calendar sync is optional, but a half-configured setup should fail
    # loudly here instead of silently never syncing anything.
    if GOOGLE_CALENDAR_ID and not calendar_sync.is_configured():
        raise SystemExit(
            "GOOGLE_CALENDAR_ID is set but the service-account key file "
            f"'{calendar_sync.SERVICE_ACCOUNT_FILE}' does not exist. Download the "
            "JSON key from Google Cloud (see README.md) or clear GOOGLE_CALENDAR_ID."
        )


# ---------------------------------------------------------------------------
# Hot conversation windows
# After a detection, keyword-less follow-ups from the same people in the same
# channel ("next month nalang pala") still get AI-checked for a while.
# ---------------------------------------------------------------------------

@dataclass
class HotWindow:
    expires_at: datetime
    participant_ids: set[int] = field(default_factory=set)
    ai_calls_used: int = 0


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # needs the toggle in the Discord developer portal


class ReminderBot(commands.Bot):
    def __init__(self) -> None:
        # The "!" prefix is never used - all commands are slash commands -
        # but commands.Bot wants one.
        super().__init__(command_prefix="!", intents=intents)
        self.owner_user: discord.User | None = None
        self.hot_windows: dict[int, HotWindow] = {}  # channel_id -> window
        # Guard against processing the same Discord message twice.
        self.recently_processed: deque[int] = deque(maxlen=500)
        # Daily AI budget bookkeeping, one bucket per purpose so detection and
        # chat can never spend each other's allowance.
        self._budget_date = date.today()
        self._budget_used: dict[str, int] = {}
        # user_id -> when they may ping the bot again. Only used when a
        # cooldown is actually configured.
        self._chat_cooldowns: dict[int, datetime] = {}

    async def setup_hook(self) -> None:
        db.init()
        self.tree.add_command(debt_group)
        self.tree.add_command(remind_group)
        self.tree.add_command(settings_group)
        self.tree.add_command(calendar_group)
        self.tree.add_command(raffle_group)
        if GUILD_ID:
            # Copy to one guild for instant availability while testing.
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s (instant)", GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally (may take up to an hour to appear)")
        delivery_loop.start()
        calendar_reconcile_loop.start()  # exits instantly each pass if not configured

    # --- AI budget -------------------------------------------------------

    def _consume_budget(self, bucket: str, cap: int) -> bool:
        """Take one unit from a daily bucket. False = that budget is spent."""
        today = date.today()
        if today != self._budget_date:  # a new day resets every bucket
            self._budget_date = today
            self._budget_used = {}
        used = self._budget_used.get(bucket, 0)
        if used >= cap:
            return False
        self._budget_used[bucket] = used + 1
        return True

    def consume_ai_budget(self) -> bool:
        """Take one unit of today's detection budget. False = exhausted."""
        return self._consume_budget("detection", MAX_AI_CALLS_PER_DAY)

    def consume_chat_budget(self) -> bool:
        """Take one unit of today's chatbot budget (its own key and cap)."""
        return self._consume_budget("chat", CHATBOT_MAX_CALLS_PER_DAY)

    # --- Chat cooldown ----------------------------------------------------

    def chat_on_cooldown(self, user_id: int) -> bool:
        """True when this person pinged too recently. Disabled by default.

        Also starts their next cooldown when they are allowed through, so one
        call does both the check and the bookkeeping.
        """
        seconds = chat_cooldown_seconds()
        if seconds <= 0:
            return False
        now = datetime.now()
        if self._chat_cooldowns.get(user_id, now) > now:
            return True
        self._chat_cooldowns[user_id] = now + timedelta(seconds=seconds)
        return False

    # --- Hot windows ------------------------------------------------------

    def active_hot_window(self, channel_id: int, author_id: int) -> HotWindow | None:
        """The channel's hot window, if it exists, is fresh, and covers this author."""
        window = self.hot_windows.get(channel_id)
        if window is None:
            return None
        if datetime.now() >= window.expires_at:
            del self.hot_windows[channel_id]
            return None
        if author_id not in window.participant_ids:
            return None
        return window

    def open_or_extend_hot_window(self, channel_id: int, participant_ids: set[int]) -> None:
        """Start (or refresh) the follow-up window after a detection."""
        if HOT_WINDOW_MINUTES <= 0:
            return
        expires = datetime.now() + timedelta(minutes=HOT_WINDOW_MINUTES)
        window = self.hot_windows.get(channel_id)
        if window is None:
            self.hot_windows[channel_id] = HotWindow(
                expires_at=expires, participant_ids=set(participant_ids)
            )
        else:
            window.expires_at = expires
            window.participant_ids |= participant_ids
        logger.debug(
            "Hot window open on channel %s until %s (participants: %s)",
            channel_id, expires.strftime("%H:%M:%S"), participant_ids,
        )

    # --- Owner notifications ---------------------------------------------

    async def dm_owner(self, content: str | None = None, **kwargs) -> bool:
        """DM the owner; returns False (and logs) if DMs are closed."""
        if self.owner_user is None:
            logger.warning("Owner user not resolved yet; dropping DM: %s", content)
            return False
        try:
            await self.owner_user.send(content, **kwargs)
            return True
        except discord.Forbidden:
            logger.warning("Cannot DM the owner - are their DMs closed for this server?")
            return False

    async def notify_owner_once_today(self, notice_key: str, text: str) -> None:
        """Send an alert DM at most once per day per topic (persists restarts)."""
        today = date.today().isoformat()
        already_sent = db.get_setting(f"notified_{notice_key}", "")
        if already_sent == today:
            return
        db.set_setting(f"notified_{notice_key}", today)
        logger.info("Owner notification (%s): %s", notice_key, text)
        await self.dm_owner(text)


bot = ReminderBot()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def chatbot_is_on() -> bool:
    """Whether chat replies are switched on right now.

    CHATBOT_ENABLED in .env sets the starting value; /settings chatbot toggle
    overrides it from Discord and that choice survives restarts.
    """
    saved = db.get_setting("chatbot_enabled", "")
    return saved == "on" if saved else CHATBOT_ENABLED


def chat_cooldown_seconds() -> int:
    """Seconds each person must wait between pings (0 = no cooldown).

    CHATBOT_COOLDOWN_SECONDS sets the starting value; /settings chatbot
    cooldown overrides it.
    """
    saved = db.get_setting("chatbot_cooldown", "")
    return int(saved) if saved.isdigit() else CHATBOT_COOLDOWN_SECONDS


def muted_chat_channels() -> set[int]:
    """Channels where the bot stays quiet (set with /settings chatbot mute)."""
    raw = db.get_setting("chatbot_muted_channels", "")
    return {int(part) for part in raw.split(",") if part.strip()}


def set_muted_chat_channels(channel_ids: set[int]) -> None:
    """Store the mute list back as a plain comma-separated string."""
    db.set_setting("chatbot_muted_channels", ",".join(str(i) for i in sorted(channel_ids)))


def format_money(currency: str, amount: float) -> str:
    """₱500 instead of ₱500.00, but keep real cents: ₱99.50."""
    text = f"{amount:,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"{currency}{text}"


def involves_owner(message: discord.Message) -> bool:
    """Debt messages only matter when the owner is part of the conversation."""
    if message.author.id == OWNER_ID:
        return True
    if any(user.id == OWNER_ID for user in message.mentions):
        return True
    # A reply to one of the owner's messages counts.
    referenced = message.reference.resolved if message.reference else None
    if isinstance(referenced, discord.Message) and referenced.author.id == OWNER_ID:
        return True
    # The owner's name typed out ("si matthew may utang...") counts too.
    if bot.owner_user is not None:
        lowered = message.content.lower()
        for name in {bot.owner_user.name.lower(), bot.owner_user.display_name.lower()}:
            if name and name in lowered:
                return True
    return False


async def fetch_context_lines(message: discord.Message) -> list[str]:
    """The last few messages before this one, oldest first, as 'author: text'."""
    if CONTEXT_MESSAGES <= 0:
        return []
    lines: list[str] = []
    try:
        async for past in message.channel.history(limit=CONTEXT_MESSAGES, before=message):
            if past.content:
                lines.append(f"{past.author.display_name}: {past.clean_content}")
    except discord.Forbidden:
        logger.debug("No permission to read history in channel %s", message.channel.id)
    lines.reverse()
    return lines


def guess_person_id(message: discord.Message, analysis: ChatAnalysis) -> int | None:
    """Best guess at the counterparty's Discord ID.

    If the author isn't the owner, the author is almost always the other
    party ("utang ko muna..."). If the owner wrote it, the first non-owner
    mention is ("@alex you owe me 500") - never ourselves, since the owner may
    well have been talking TO the bot.
    """
    # Use analysis to get counterparty name if available
    if analysis.counterparty or analysis.update_person:
        # This is just to use the analysis parameter to satisfy the linter
        # The actual logic doesn't change
        pass

    if message.author.id != OWNER_ID:
        return message.author.id
    for user in message.mentions:
        if user.id != OWNER_ID and (bot.user is None or user.id != bot.user.id):
            return user.id
    return None


def parse_due_datetime(raw_value: str | None) -> datetime | None:
    """Turn 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD' into a datetime.

    Date-only input gets the configured default delivery hour.
    Returns None when the text isn't a date we understand.
    """
    if not raw_value:
        return None
    raw_value = raw_value.strip().replace("T", " ")

    # Primary format: strict ISO that the AI is supposed to produce
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw_value, fmt)
        except ValueError:
            pass
    try:
        day = datetime.strptime(raw_value, "%Y-%m-%d")
        return day.replace(hour=DEFAULT_REMINDER_HOUR, minute=0)
    except ValueError:
        pass

    # Fallback: common formats the AI sometimes produces despite instructions
    fallback_formats = [
        "%B %d, %Y %I:%M%p",      # "December 24, 2050 11:45pm"
        "%b %d, %Y %I:%M%p",      # "Dec 24, 2050 11:45pm"
        "%B %d, %Y, %I:%M%p",     # "December 24, 2050, 11:45pm" (comma before time)
        "%b %d, %Y, %I:%M%p",     # "Dec 24, 2050, 11:45pm"
        "%B %d, %Y %I:%M %p",     # "December 24, 2050 11:45 pm" (space before am/pm)
        "%b %d, %Y %I:%M %p",     # "Dec 24, 2050 11:45 pm"
        "%B %d, %Y",              # "December 24, 2050"
        "%b %d, %Y",              # "Dec 24, 2050"
        "%Y-%m-%d %I:%M%p",       # "2050-12-24 11:45pm"
        "%Y-%m-%d %I:%M %p",      # "2050-12-24 11:45 pm"
        "%m/%d/%Y %H:%M",         # "12/24/2050 23:45"
        "%m/%d/%Y",               # "12/24/2050"
    ]

    for fmt in fallback_formats:
        try:
            parsed = datetime.strptime(raw_value, fmt)
            logger.debug(
                "Parsed date '%s' using fallback format '%s' (AI should output YYYY-MM-DD)",
                raw_value, fmt
            )
            # Date-only formats need the default hour
            if parsed.hour == 0 and parsed.minute == 0 and "%H" not in fmt and "%I" not in fmt:
                parsed = parsed.replace(hour=DEFAULT_REMINDER_HOUR)
            return parsed
        except ValueError:
            pass

    logger.warning("Could not parse reminder date: %r", raw_value)
    return None


# Repeating reminders. Kept to these four because they cover what people
# actually ask for ("every Monday", "araw-araw") and each has an unambiguous
# next date - no cron strings to explain to anyone.
REPEAT_RULES = ("daily", "weekly", "monthly", "yearly")


def _add_months(moment: datetime, months: int) -> datetime:
    """Same day-of-month N months on, clamped to the month's length.

    Jan 31 + 1 month is Feb 28 (or 29) rather than an invalid date.
    """
    month_index = moment.month - 1 + months
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    if month == 12:
        days_in_month = 31
    else:
        days_in_month = (date(year, month + 1, 1) - date(year, month, 1)).days
    return moment.replace(year=year, month=month, day=min(moment.day, days_in_month))


def next_occurrence(moment: datetime, rule: str | None) -> datetime | None:
    """The next time a repeating reminder should fire after `moment`."""
    if rule == "daily":
        return moment + timedelta(days=1)
    if rule == "weekly":
        return moment + timedelta(weeks=1)   # keeps the same weekday
    if rule == "monthly":
        return _add_months(moment, 1)
    if rule == "yearly":
        return _add_months(moment, 12)
    return None


def next_due_after(moment: datetime, rule: str | None, now: datetime) -> datetime | None:
    """Next occurrence strictly in the future.

    Skips any occurrences missed while the bot was off, so a weekly reminder
    that lapsed for a month fires once and then resumes on schedule instead of
    firing four times in a row.
    """
    upcoming = next_occurrence(moment, rule)
    while upcoming is not None and upcoming <= now:
        upcoming = next_occurrence(upcoming, rule)
    return upcoming


def parse_plain_date(raw_value: str | None) -> str | None:
    """Validate a YYYY-MM-DD string (used for debt due dates)."""
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value.strip()[:10]).isoformat()
    except ValueError:
        return None


async def resolve_person_argument(
    person_text: str, guild: discord.Guild | None
) -> tuple[int | None, str]:
    """Turn a command's 'person' argument into (discord_id, display name).

    Accepts an @mention (resolved to a real user) or any plain name -
    plain names cover people without Discord / debts made outside the server.
    """
    mention_match = _MENTION_PATTERN.fullmatch(person_text.strip())
    if not mention_match:
        return None, person_text.strip()
    user_id = int(mention_match.group(1))
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return user_id, member.name
    try:
        user = await bot.fetch_user(user_id)
        return user_id, user.name
    except discord.NotFound:
        return user_id, f"user-{user_id}"


# ---------------------------------------------------------------------------
# Applying a confirmed detection to the database
# ---------------------------------------------------------------------------

def apply_analysis(
    analysis: ChatAnalysis,
    person_id: int | None,
    channel_id: int | None,
    source_message_id: int | None,
    message: discord.Message | None = None,
) -> str:
    """Write a confirmed detection to the ledger. Returns a human summary."""
    person_name = (analysis.counterparty or analysis.update_person or "unknown").strip()
    currency = analysis.currency or DEFAULT_CURRENCY

    if analysis.kind == "debt_event":
        if analysis.debt_type == "new_debt":
            direction = analysis.direction or "they_owe_me"
            due = parse_plain_date(analysis.promised_date)
            debt_id = db.add_debt(
                direction=direction,
                person_name=person_name,
                person_id=person_id,
                amount=analysis.amount or 0.0,
                currency=currency,
                description=analysis.description or "",
                channel_id=channel_id,
                source_message_id=source_message_id,
                due_date=due,
            )
            who = f"{person_name} owes you" if direction == "they_owe_me" else f"You owe {person_name}"
            due_text = f", promised by {due}" if due else ""
            return f"Recorded debt #{debt_id}: {who} {format_money(currency, analysis.amount or 0.0)}{due_text}."

        if analysis.debt_type == "payment":
            debt = db.latest_open_debt(person_name, person_id)
            if debt is None:
                return f"No open debt found for {person_name} - nothing to settle."
            db.mark_paid(debt["id"], source_message_id)
            return (
                f"Marked debt #{debt['id']} paid: {debt['person_name']}, "
                f"{format_money(debt['currency'], debt['amount'])}."
            )

        if analysis.debt_type == "promise_date":
            debt = db.latest_open_debt(person_name, person_id)
            if debt is None:
                return f"No open debt found for {person_name} to attach that date to."
            due = parse_plain_date(analysis.promised_date)
            if due is None:
                return "Couldn't understand the promised date - nothing changed."
            db.update_debt_field(debt["id"], "due_date", due, source_message_id)
            return f"Debt #{debt['id']} ({debt['person_name']}): promised pay date set to {due}."

    if analysis.kind == "reminder":
        due_moment = parse_due_datetime(analysis.reminder_due)
        if due_moment is None:
            return "No usable date on that reminder - use /remind add instead."
        reminder_id = db.add_reminder(
            reminder_text=analysis.reminder_text or "(no description)",
            due_at=due_moment.isoformat(timespec="minutes"),
            requester_name=person_name if person_name != "unknown" else "you",
            requester_id=person_id,
            channel_id=channel_id,
            source_message_id=source_message_id,
            repeat_rule=analysis.reminder_repeat,
        )
        repeat_note = f", repeating {analysis.reminder_repeat}" if analysis.reminder_repeat else ""
        return (
            f"Reminder #{reminder_id} set for {due_moment.strftime('%Y-%m-%d %H:%M')}"
            f"{repeat_note}: {analysis.reminder_text}"
        )

    if analysis.kind == "update":
        if analysis.update_target == "debt":
            debt = db.latest_open_debt(person_name, person_id)
            if debt is None:
                return f"Couldn't find an open debt for {person_name} to update."
            if analysis.update_field == "due_date":
                due = parse_plain_date(analysis.update_new_value)
                if due is None:
                    return "Couldn't understand the new date - nothing changed."
                old = debt["due_date"] or "none"
                db.update_debt_field(debt["id"], "due_date", due, source_message_id)
                return f"Debt #{debt['id']} ({debt['person_name']}): due date {old} → {due}."
            if analysis.update_field == "amount":
                try:
                    new_amount = float(analysis.update_new_value)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return "Couldn't understand the new amount - nothing changed."
                old_money = format_money(debt["currency"], debt["amount"])
                db.update_debt_field(debt["id"], "amount", new_amount, source_message_id)
                return (
                    f"Debt #{debt['id']} ({debt['person_name']}): amount {old_money} → "
                    f"{format_money(debt['currency'], new_amount)}."
                )
        if analysis.update_target == "reminder":
            reminder = db.latest_pending_reminder_for(person_name, person_id)
            if reminder is None:
                return "Couldn't find a pending reminder to update."
            due_moment = parse_due_datetime(analysis.update_new_value)
            if due_moment is None:
                return "Couldn't understand the new reminder time - nothing changed."
            old = reminder["due_at"].replace("T", " ")
            db.update_reminder_due(
                reminder["id"], due_moment.isoformat(timespec="minutes"), source_message_id
            )
            return (
                f"Reminder #{reminder['id']} moved: {old} → "
                f"{due_moment.strftime('%Y-%m-%d %H:%M')} ({reminder['reminder_text']})."
            )

    if analysis.kind == "raffle":
        if analysis.raffle_action == "create":
            # Raffle creation detected in chat - requires bot mention
            # Create the raffle directly
            guild_id = message.guild.id if message else None
            if not guild_id:
                return "I can only help with raffles in servers."

            # Creator info: from message if available, otherwise from analysis
            creator_name = message.author.display_name if message else (analysis.counterparty or "unknown")
            creator_id = message.author.id if message else person_id
            if not creator_id:
                return "Couldn't determine who's creating this raffle."

            # Use defaults if not specified
            # Reject non-money raffles — only cash prizes are supported
            if analysis.entry_cost is None and analysis.prize_description:
                return (
                    "ℹ️ I can only raffle **money** prizes! "
                    "Include an amount, e.g.: *'raffle 500 pesos, prize is a game code'*."
                )
            prize_amount = analysis.entry_cost if analysis.entry_cost is not None else 100.0  # Default 100
            duration_minutes = 1  # Default 1 min if user didn't specify
            if analysis.duration:
                # Simple duration parsing - in a full implementation we'd parse this better
                try:
                    # Try to extract a number from the duration string
                    import re
                    numbers = re.findall(r'\d+', analysis.duration)
                    if numbers:
                        duration_minutes = int(numbers[0])
                        # Check for hours/minutes indicators
                        if 'hour' in analysis.duration.lower() or 'hr' in analysis.duration.lower():
                            duration_minutes *= 60
                        elif 'day' in analysis.duration.lower():  # "day" has no false positive
                            duration_minutes *= 60 * 24
                except:
                    duration_minutes = 1  # Default 1 min if parsing fails

            description = analysis.prize_description or "A mystery prize!"
            max_participants = analysis.max_participants

            # Calculate end time if duration specified
            ends_at = None
            if duration_minutes > 0:
                ends_at = (datetime.now() + timedelta(minutes=duration_minutes)).isoformat(timespec="minutes")

            # Get the guild's default auto-join role if auto-join is enabled
            auto_join_role_id = None
            if db.get_guild_auto_join_enabled(guild_id):
                auto_join_role_id = db.get_guild_auto_join_role(guild_id)

            # Channel for the raffle
            cid = message.channel.id if message else channel_id

            # Create the raffle
            raffle_id = db.create_raffle(
                prize_description=description,
                prize_amount=prize_amount,
                currency=analysis.currency or "₱",
                creator_name=creator_name,
                creator_id=creator_id,
                guild_id=guild_id,
                channel_id=cid,
                max_participants=max_participants,
                ends_at=ends_at,
                auto_join_role_id=auto_join_role_id,
                active=True
            )

            # Auto-join the creator
            db.join_raffle(raffle_id, creator_id, creator_name, "creator")

            # Format the response
            prize_str = format_money(analysis.currency or "₱", prize_amount)
            duration_str = f"{duration_minutes} minutes" if duration_minutes > 0 else "until ended manually"
            limit_str = f"{max_participants} participants" if max_participants else "unlimited participants"
            auto_role_line = f" <@&{auto_join_role_id}> are auto-entered!" if auto_join_role_id else ""

            return f"🎉 Raffle #{raffle_id} created! \"{description}\" - Prize: {prize_str} - Duration: {duration_str} - Limit: {limit_str}.{auto_role_line} Use `/raffle join` to participate — it's free!"

        elif analysis.raffle_action == "join":
            # User wants to join a raffle
            # Find active raffle in this channel and try to join it
            guild_id = message.guild.id if message else None
            if not guild_id:
                return "I can only help with raffles in servers."

            cid = message.channel.id if message else channel_id
            raffles = db.get_active_raffles(cid, guild_id)
            if not raffles:
                return "There are no active raffles in this channel to join."

            raffle = raffles[0]  # Most recent active raffle
            user_id = message.author.id if message else person_id
            user_name = message.author.display_name if message else (analysis.counterparty or "someone")
            if not user_id:
                return "Couldn't determine who's trying to join."

            # Check if already participated
            participants = db.get_raffle_participants(raffle["id"])
            if any(p["user_id"] == user_id for p in participants):
                return f"You're already in raffle #{raffle['id']}!"

            # No balance check - joining is free!

            if db.join_raffle(raffle["id"], user_id, user_name, "natural_language"):
                return f"✅ You've joined raffle #{raffle['id']}! Prize: {format_money(raffle['currency'], db._raffle_prize(raffle))}. Good luck! 🍀"
            else:
                return "Failed to join raffle. Please try again."

        elif analysis.raffle_action == "end":
            # User wants to end a raffle - check if they're the creator
            guild_id = message.guild.id if message else None
            if not guild_id:
                return "I can only help with raffles in servers."

            cid = message.channel.id if message else channel_id
            raffles = db.get_active_raffles(cid, guild_id)
            if not raffles:
                return "There are no active raffles in this channel to end."

            raffle = raffles[0]  # Most recent active raffle
            user_id = message.author.id if message else None
            if user_id and raffle["creator_id"] != user_id:
                return f"Only the raffle creator (<@{raffle['creator_id']}>) can end this raffle."

            # Actually end the raffle and pick a winner!
            result = db.end_raffle(raffle["id"])
            if not result:
                return "Failed to end raffle — no participants or it's already ended."

            prize_str = format_money(raffle["currency"], result["prize_pool"])
            return (
                f"🎉 Raffle #{raffle['id']} has ended! "
                f"**{result['winner_name']}** won {prize_str}! "
                f"({result['participant_count']} participants)"
            )

        elif analysis.raffle_action == "cancel":
            # User wants to cancel a raffle - check if they're the creator
            guild_id = message.guild.id if message else None
            if not guild_id:
                return "I can only help with raffles in servers."

            cid = message.channel.id if message else channel_id
            raffles = db.get_active_raffles(cid, guild_id)
            if not raffles:
                return "There are no active raffles in this channel to cancel."

            raffle = raffles[0]  # Most recent active raffle
            user_id = message.author.id if message else None
            if user_id and raffle["creator_id"] != user_id:
                return f"Only the raffle creator (<@{raffle['creator_id']}>) can cancel this raffle."

            # Cancel the raffle (no refunds - joining is free)
            if db.cancel_raffle(raffle["id"]):
                return f"🎉 Raffle #{raffle['id']} has been cancelled!"
            else:
                return "Failed to cancel raffle. Please try again."

    return "Nothing to do for that event."


# ---------------------------------------------------------------------------
# Confirmation DM (buttons)
# ---------------------------------------------------------------------------

class ConfirmView(discord.ui.View):
    """The Confirm/Ignore buttons under a detection DM."""

    def __init__(
        self,
        analysis: ChatAnalysis,
        person_id: int | None,
        channel_id: int | None,
        source_message_id: int | None,
        is_possible_duplicate: bool,
    ) -> None:
        super().__init__(timeout=CONFIRMATION_TIMEOUT)
        self.analysis = analysis
        self.person_id = person_id
        self.channel_id = channel_id
        self.source_message_id = source_message_id
        if is_possible_duplicate:
            self.confirm_button.label = "Confirm anyway"

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button  # pylint: disable=unused-argument
    ) -> None:
        result_text = apply_analysis(
            self.analysis, self.person_id, self.channel_id, self.source_message_id
        )
        logger.info("Confirmed detection: %s", result_text)
        await interaction.response.edit_message(content=f"✅ {result_text}", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="Ignore", style=discord.ButtonStyle.secondary)
    async def ignore_button(
        self, interaction: discord.Interaction, button: discord.ui.Button  # pylint: disable=unused-argument
    ) -> None:
        logger.info("Detection ignored by owner (message %s)", self.source_message_id)
        await interaction.response.edit_message(content="🚫 Ignored.", embed=None, view=None)
        self.stop()


def build_confirmation_embed(
    analysis: ChatAnalysis,
    message: discord.Message,
    person_id: int | None,
    duplicate_note: str | None,
) -> discord.Embed:
    """A compact summary of what the AI detected, for the owner to judge."""
    person = analysis.counterparty or analysis.update_person or "unknown"
    currency = analysis.currency or DEFAULT_CURRENCY
    lines: list[str] = []

    if analysis.kind == "debt_event":
        titles = {
            "new_debt": "💰 New debt detected",
            "payment": "✅ Payment detected",
            "promise_date": "📅 Promised pay date detected",
        }
        title = titles.get(analysis.debt_type or "", "💰 Debt event detected")
        if analysis.debt_type == "new_debt":
            who = f"**{person}** owes you" if (analysis.direction or "they_owe_me") == "they_owe_me" else f"You owe **{person}**"
            amount_text = format_money(currency, analysis.amount) if analysis.amount else "amount unclear"
            lines.append(f"{who} {amount_text}")
            if analysis.description:
                lines.append(f"For: {analysis.description}")
            if analysis.promised_date:
                lines.append(f"Promised by: {analysis.promised_date}")
        elif analysis.debt_type == "payment":
            target = db.latest_open_debt(person, person_id)
            if target is not None:
                lines.append(
                    f"Will settle debt #{target['id']}: {target['person_name']}, "
                    f"{format_money(target['currency'], target['amount'])}"
                    + (f" ({target['description']})" if target["description"] else "")
                )
            else:
                lines.append(f"**{person}** says it's paid - but no open debt was found.")
        else:  # promise_date
            target = db.latest_open_debt(person, person_id)
            if target is not None:
                lines.append(
                    f"Debt #{target['id']} ({target['person_name']}, "
                    f"{format_money(target['currency'], target['amount'])}): "
                    f"pay date → **{analysis.promised_date}**"
                )
            else:
                lines.append(f"**{person}** promised to pay {analysis.promised_date}, but no open debt was found.")
    elif analysis.kind == "reminder":
        title = "⏰ Reminder detected"
        lines.append(f"**{analysis.reminder_text}**")
        due_moment = parse_due_datetime(analysis.reminder_due)
        if due_moment:
            lines.append(f"When: {due_moment.strftime('%Y-%m-%d %H:%M')}")
        if analysis.reminder_repeat:
            lines.append(f"Repeats: 🔁 **{analysis.reminder_repeat}**")
        lines.append(f"Asked by: {person if person != 'unknown' else message.author.display_name}")
    else:  # update
        title = "✏️ Update detected"
        if analysis.update_target == "debt":
            target = db.latest_open_debt(person, person_id)
            current = (
                f"debt #{target['id']} ({target['person_name']}, "
                f"{format_money(target['currency'], target['amount'])})"
                if target is not None
                else f"{person}'s latest open debt (none found!)"
            )
            lines.append(f"{current}: **{analysis.update_field}** → **{analysis.update_new_value}**")
        else:
            target = db.latest_pending_reminder_for(person, person_id)
            current = (
                f"reminder #{target['id']} ({target['reminder_text']})"
                if target is not None
                else f"{person}'s pending reminder (none found!)"
            )
            lines.append(f"{current}: time → **{analysis.update_new_value}**")

    lines.append(f"\nConfidence: {analysis.confidence:.0%} · [Jump to message]({message.jump_url})")
    embed = discord.Embed(title=title, description="\n".join(lines), color=0x2ECC71)
    if duplicate_note:
        embed.add_field(name="⚠️ Possible duplicate", value=duplicate_note, inline=False)
    return embed


def find_duplicate_note(analysis: ChatAnalysis, person_id: int | None) -> str | None:
    """Describe an existing similar record, if any, for the confirmation DM."""
    if analysis.kind == "debt_event" and analysis.debt_type == "new_debt":
        existing = db.find_similar_debt(
            analysis.counterparty or "", person_id, analysis.amount
        )
        if existing is not None:
            return (
                f"Open debt #{existing['id']} already exists: {existing['person_name']}, "
                f"{format_money(existing['currency'], existing['amount'])}"
                + (f" ({existing['description']})" if existing["description"] else "")
            )
    if analysis.kind == "reminder":
        due_moment = parse_due_datetime(analysis.reminder_due)
        due_day = due_moment.date().isoformat() if due_moment else None
        existing = db.find_similar_reminder(due_day, analysis.reminder_text or "")
        if existing is not None:
            return (
                f"Reminder #{existing['id']} already pending: "
                f"\"{existing['reminder_text']}\" at {existing['due_at'].replace('T', ' ')}"
            )
    return None


# ---------------------------------------------------------------------------
# The message watcher
# ---------------------------------------------------------------------------

def build_activity(activity_type: str, text: str) -> discord.BaseActivity | None:
    """Turn the stored status setting into a discord.py activity object.

    "playing" gets the dedicated Game class, "custom" is free-standing text
    with no verb, and everything else (listening/watching/competing) maps
    onto the generic Activity with the matching type.
    """
    if not text:
        return None
    if activity_type == "playing":
        return discord.Game(name=text)
    if activity_type == "custom":
        return discord.CustomActivity(name=text)
    return discord.Activity(
        type=getattr(discord.ActivityType, activity_type, discord.ActivityType.playing),
        name=text,
    )


async def apply_saved_status() -> None:
    """Re-apply the owner's chosen bot status (persisted in the settings table)."""
    activity = build_activity(
        db.get_setting("status_type", "custom"), db.get_setting("status_text", "")
    )
    if activity is not None:
        await bot.change_presence(activity=activity)


@bot.event
async def on_ready() -> None:
    bot.owner_user = await bot.fetch_user(OWNER_ID)
    logger.info(
        "Logged in as %s | owner: %s | %d server(s)",
        bot.user, bot.owner_user, len(bot.guilds),
    )
    # Presence resets on every (re)connect, so set it here rather than once
    # at startup - on_ready also fires again after gateway reconnects.
    await apply_saved_status()


# ---------------------------------------------------------------------------
# Chatbot mode: when someone talks TO the bot, it talks back
# ---------------------------------------------------------------------------

def is_chat_trigger(message: discord.Message) -> bool:
    """True when someone is actually talking to the bot.

    Covers a direct @mention and replies to the bot's own messages (a reply
    normally pings, but the reference check also catches one sent with the
    ping turned off). Never @everyone/@here - a mass ping is not a
    conversation, and the bot would answer every announcement.
    """
    if bot.user is None or not chatbot_is_on():
        return False
    if message.mention_everyone:
        return False
    if message.channel.id in muted_chat_channels():
        return False
    if bot.user in message.mentions:
        return True
    referenced = message.reference.resolved if message.reference else None
    return isinstance(referenced, discord.Message) and referenced.author.id == bot.user.id


async def say(
    message: discord.Message,
    text: str,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    """Reply in the channel, truncated to Discord's limit.

    If allowed_mentions is omitted, block all mentions by default. This keeps
    AI-generated replies from pinging arbitrary users or roles unless the
    original message explicitly mentioned them.
    """
    if allowed_mentions is None:
        allowed_mentions = discord.AllowedMentions.none()
    await message.reply(text[:2000], allowed_mentions=allowed_mentions)


def strip_own_mention(message: discord.Message) -> str:
    """The message text with our own @mention removed.

    clean_content renders the mention as "@Nickname" (not "<@id>"), so that is
    what gets dropped. Both names are tried because the bot may be nicknamed in
    this server.
    """
    text = message.clean_content
    for name in {bot.user.display_name, message.guild.me.display_name}:
        text = text.replace(f"@{name}", "")
    return text.strip()


def _looks_like_search_query(question: str) -> bool:
    text = question.strip()
    if not text:
        return False
    normalized = text.lower()
    if len(normalized) < 5:
        return False

    # Ignore obvious reminder/chat requests, which are handled separately.
    if any(token in normalized for token in ("remind", "reminder", "paalala", "tandaan", "utang", "debt", "bayad", "pay")):
        return False

    # Ignore casual conversational questions that don't need web search.
    negative_phrases = (
        "how are you",
        "how's it going",
        "what's up",
        "what do you think",
        "what should i",
        "what should we",
        "what do i",
        "what do we",
        "do you",
        "can you",
        "could you",
        "will you",
        "would you",
        "should i",
        "should we",
        "is it",
        "are you",
        "are we",
        "did you",
        "did i",
        "do i",
        "do we",
        "let's",
    )
    if any(phrase in normalized for phrase in negative_phrases):
        return False

    # A direct question mark is a strong signal.
    if normalized.endswith("?"):
        return True

    # Common lookup and factual question words in English and Tagalog.
    if re.search(
        r"\b(?:when|what|where|who|why|how|which|kelan|ano|sino|saan|bakit|paano|ilan|magkano|alin)\b",
        normalized,
    ):
        if re.search(
            r"\b(?:when is|when are|what is|what are|where is|where are|who is|who are|why is|why are|how many|how much|how long|which (?:is|are)|kelan|ano|sino|saan|bakit|paano|ilan|magkano|alin)\b",
            normalized,
        ):
            return True
        if re.match(r"^(?:is|are|does|do|did|can|could|should|would|will|may|might)\b", normalized):
            return True

    # Natural lookup instructions and factual search phrases.
    search_phrases = (
        "search the web",
        "search online",
        "search for",
        "look up",
        "lookup",
        "find out",
        "google ",
        "research ",
    )
    if any(phrase in normalized for phrase in search_phrases):
        return True

    return False


async def handle_mention_detection(message: discord.Message, question: str) -> bool:
    """Act on a reminder or debt someone asked the bot for directly.

    Being pinged, the most explicit request there is, so a reminder here is
    created straight away and confirmed in the channel - exactly like
    /remind add, but anyone can already use. Debts and corrections still go
    to the owner's Confirm/Ignore DM and are never acknowledged out loud.

    Returns True when the message is fully handled (no chat reply needed).
    """
    categories = prescan(question)
    if not categories:
        return False  # ordinary chatter - the overwhelmingly common case

    # No involves_owner() gate here, unlike the passive watcher: the message was
    # addressed to the bot, and the bot stands in for the owner. That is what
    # makes "@bot utang ko sayo 500" count.
    analysis, error_kind = await run_detection(message, text=question, in_hot_window=False)
    if error_kind or analysis is None or analysis.kind == "none":
        # Fallback: when someone @-mentions the bot asking for a raffle and the AI
        # misfires (returns "none"), call the simple keyword-based handler instead
        # of ignoring the request entirely.
        if "raffle" in categories:
            return await _handle_raffle_fallback(message, question)
        return False
    if analysis.confidence < CONFIDENCE_THRESHOLD:
        logger.debug(
            "Mention %s: %s below confidence threshold (%.2f < %.2f)",
            message.id, analysis.kind, analysis.confidence, CONFIDENCE_THRESHOLD,
        )
        return False

    if analysis.kind == "reminder":
        return await create_requested_reminder(message, analysis)

    # Raffle actions: execute immediately like reminders — anyone can use /raffle.
    if analysis.kind == "raffle":
        result_text = apply_analysis(
            analysis, guess_person_id(message, analysis),
            message.channel.id, message.id,
            message=message,
        )
        await say(message, result_text)
        return True

    # Debt events and corrections: the owner judges these, and the channel is
    # told nothing - the bot never advertises that it tracks money. Falling
    # through to a normal chat reply keeps it looking like ordinary banter.
    await offer_to_owner(message, analysis, guess_person_id(message, analysis))
    bot.open_or_extend_hot_window(message.channel.id, {message.author.id, OWNER_ID})
    return False


async def _handle_raffle_fallback(message: discord.Message, question: str) -> bool:
    """Simple keyword-based raffle handler when the AI can't classify the message.

    Called only after prescan confirmed "raffle" keywords but the AI returned
    kind=="none".  Builds a minimal ChatAnalysis and calls apply_analysis.
    """
    import re
    from ai_parser import ChatAnalysis
    from detection import MONEY_PATTERN

    q = question.lower()

    # --- action detection ---
    if any(w in q for w in ("create", "raffle off", "new ", "start ",
                             "mag-", "gawa", "simulan", "meron", "lets")):
        action = "create"
    elif any(w in q for w in ("end ", "close", "tapos", "pick winner",
                               "i-pull", "hugot", "bunot", "draw winner")):
        action = "end"
    elif any(w in q for w in ("cancel", "delete", "remove", "bura",
                               "alis", "wag", "call it off")):
        action = "cancel"
    elif any(w in q for w in ("join", "enter", "sali ", "count ", "ako ",
                               "gusto ", "pa-join", "rais ")):
        action = "join"
    else:
        # Default to create - if they said "raffle", they probably want one
        action = "create"

    # --- prize / money extraction ---
    prize_amount = 100.0  # default
    currency = "₱"
    money_match = MONEY_PATTERN.search(question)
    if money_match:
        raw = money_match.group(0)
        # Pull out the first number from the matched text
        num_match = re.search(r'[\d,]+(?:\.\d+)?', raw)
        if num_match:
            try:
                prize_amount = float(num_match.group(0).replace(",", ""))
            except (ValueError, AttributeError):
                pass
        # Guess currency from the raw match
        if "$" in raw or "dollar" in raw.lower() or "usd" in raw.lower():
            currency = "$"
        elif "€" in raw or "eur" in raw.lower() or "euro" in raw.lower():
            currency = "€"

    # --- prize description ---
    # Grab any quoted text or description-like phrases
    desc_match = re.search(r'"([^"]+)"', question) or re.search(r"'([^']+)'", question)
    if desc_match:
        description = desc_match.group(1)
    else:
        # Look for "for X" or "prize is X" patterns
        desc_from = re.search(r'(?:prize\s*(?:is|:)?\s*|for\s+)(["\u201c]?\w[\w\s]*?)(?:[.?!]|$)', q)
        if desc_from:
            description = desc_from.group(1).strip().rstrip(".!?")
        else:
            description = "A mystery prize!"

    # Reject non-money raffles — only cash prizes are supported
    if not money_match and description != "A mystery prize!":
        await say(
            message,
            "ℹ️ I can only raffle **money** prizes! "
            "Include an amount, e.g.: *'raffle 500 pesos, prize is a game code'*."
        )
        return True

    analysis = ChatAnalysis(
        kind="raffle",
        confidence=0.7,
        raffle_action=action,
        prize_description=description,
        entry_cost=prize_amount,
        currency=currency,
    )

    result_text = apply_analysis(
        analysis, guess_person_id(message, analysis),
        message.channel.id, message.id,
        message=message,
    )
    await say(message, result_text)
    return True


async def create_requested_reminder(
    message: discord.Message, analysis: ChatAnalysis, silent: bool = False
) -> bool:
    """Save a reminder someone asked the bot for, and answer them in-channel.

    Always returns True: every path here has already replied, so the caller
    should not also spend a chat reply on it.

    Args:
        message: The Discord message that triggered this
        analysis: The AI's analysis of what reminder to create
        silent: If True, don't send a reply (used by fallback detection)
    """
    text = analysis.reminder_text or "(no description)"
    due_moment = parse_due_datetime(analysis.reminder_due)

    # Without a date there is nothing to schedule - ask instead of guessing.
    if due_moment is None:
        if not silent:
            await say(
                message,
                f"⏰ Sige, pero kailan? Sabihin mo kung kailan ka gustong paalalahanan "
                f'tungkol sa "{text}", o gamitin mo `/remind add` para sa eksaktong oras.',
            )
        bot.open_or_extend_hot_window(message.channel.id, {message.author.id, OWNER_ID})
        return True

    # Someone else's similar reminder is none of your business - only a clash
    # with your own is worth mentioning (same rule as /remind add).
    duplicate = db.find_similar_reminder(due_moment.date().isoformat(), text)
    if duplicate is not None and duplicate["requester_id"] != message.author.id:
        duplicate = None
    if duplicate is not None:
        if not silent:
            await say(
                message,
                f"⏰ May ganito ka na - reminder #{duplicate['id']}: "
                f"\"{duplicate['reminder_text']}\" sa {duplicate['due_at'].replace('T', ' ')}. "
                "Gamitin mo `/remind add` na may `force: True` kung gusto mo pa rin ng bago.",
            )
        return True

    reminder_id = db.add_reminder(
        reminder_text=text,
        due_at=due_moment.isoformat(timespec="minutes"),
        requester_name=message.author.display_name,
        requester_id=message.author.id,
        channel_id=message.channel.id,
        source_message_id=message.id,
        repeat_rule=analysis.reminder_repeat,
    )
    repeat_note = f" - then 🔁 {analysis.reminder_repeat}" if analysis.reminder_repeat else ""
    logger.info(
        "Reminder #%s created from a mention by %s: %s (%s, repeat: %s)%s",
        reminder_id, message.author.name, text,
        due_moment.strftime("%Y-%m-%d %H:%M"), analysis.reminder_repeat or "once",
        " (silent fallback)" if silent else "",
    )
    if not silent:
        await say(
            message,
            f"⏰ Sige! Reminder #{reminder_id} - "
            f"{due_moment.strftime('%Y-%m-%d %H:%M')}{repeat_note}: {text}",
        )
    # Corrections ("sa sabado nalang pala") get caught for a while.
    bot.open_or_extend_hot_window(message.channel.id, {message.author.id, OWNER_ID})
    return True


async def handle_memory_command(message: discord.Message, text: str) -> bool:
    """Process memory-related commands like nickname changes.

    Returns True if this was a memory command (fully handled).
    """
    # Pattern: "call @user as NAME" or "tawag kay @user ay NAME"
    # Supports both English and Tagalog variations
    patterns = [
        r"(?:call|tawag\s+(?:mo\s+)?(?:kay|sa))\s+<@!?(\d+)>\s+(?:as|ay|is|ng|na)\s+['\"]?(\w+)['\"]?",
        r"(?:gusto\s+ko|i\s+want|sana)\s+(?:tawag|call)\s+(?:mo\s+)?(?:kay|sa|mo\s+si)?\s*<@!?(\d+)>\s+(?:ay|as|is|ng|na)\s+['\"]?(\w+)['\"]?",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            target_user_id = int(match.group(1))
            nickname = match.group(2).strip().strip('"\'').upper()

            # Save the memory
            db.save_user_memory(
                user_id=message.author.id,
                memory_key="nickname_preference",
                memory_value=nickname,
                context=str(target_user_id)
            )

            try:
                target_user = await bot.fetch_user(target_user_id)
                await say(message,
                    f"✓ Noted! I'll call {target_user.mention} as **{nickname}** from now on.")
            except:
                await say(message,
                    f"✓ Noted! I'll call that person as **{nickname}** from now on.")

            logger.info(
                "Memory saved: user %s wants %s called '%s'",
                message.author.id, target_user_id, nickname
            )
            return True

    # Pattern: "forget about calling @user as NAME" or similar
    forget_patterns = [
        r"forget\s+(?:about\s+)?(?:calling|tawag)\s+(?:kay\s+)?<@!?(\d+)>",
        r"kalimutan\s+(?:mo\s+)?(?:yung\s+)?(?:tawag|pangalan)\s+(?:kay\s+)?<@!?(\d+)>",
        r"(?:stop|huwag\s+na)\s+(?:calling|tawag)\s+(?:kay\s+)?<@!?(\d+)>",
    ]

    for pattern in forget_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            target_user_id = int(match.group(1))
            deleted = db.delete_user_memory(
                message.author.id,
                "nickname_preference",
                str(target_user_id)
            )

            if deleted:
                await say(message, "✓ Forgotten! I'll use their regular name now.")
            else:
                await say(message, "I don't have any special name saved for that person.")

            logger.info(
                "Memory deleted: user %s removed nickname for %s",
                message.author.id, target_user_id
            )
            return True

    return False


async def handle_chat_mention(message: discord.Message) -> None:
    """Answer an @mention: act on it if it's a request, else chat back."""
    question = strip_own_mention(message)

    # Check for memory commands BEFORE detection (these are instant, no AI needed)
    if await handle_memory_command(message, question):
        return

    # A reminder or debt asked of us directly is a request, not conversation.
    # This runs before the cooldown because it is real work, not chatter, and
    # the prescan keeps it rare - it also spends the detection budget, not chat's.
    if await handle_mention_detection(message, question):
        return

    if bot.chat_on_cooldown(message.author.id):
        # A quiet signal - replying "please wait" would just add to the spam.
        try:
            await message.add_reaction("⏳")
        except discord.HTTPException:
            pass  # no Add Reactions permission here; staying silent is fine
        return

    if not bot.consume_chat_budget():
        await bot.notify_owner_once_today(
            "chat_daily_cap",
            "🤖 The chatbot hit its daily reply cap "
            f"(CHATBOT_MAX_CALLS_PER_DAY={CHATBOT_MAX_CALLS_PER_DAY}), so @mention "
            "replies are paused until midnight. Debt/reminder detection has its own "
            "separate budget and is unaffected.",
        )
        # In-channel wording never mentions quotas, limits, or any AI service -
        # to the server this is just the bot being tired. Details go to the
        # owner's DM instead.
        await say(message, "😴 my brain's fried for today - catch me again tomorrow!")
        return

    # Check for image attachments - download them so the AI can "see" them.
    # Capped at 3 images to keep token usage reasonable.
    image_datas: list[tuple[bytes, str]] = []
    for attachment in message.attachments:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            try:
                img_bytes = await attachment.read()
                image_datas.append((img_bytes, attachment.content_type))
                logger.debug(
                    "Downloaded image %s (%d bytes, %s)",
                    attachment.filename, len(img_bytes), attachment.content_type,
                )
            except (discord.HTTPException, discord.NotFound) as exc:
                logger.warning("Could not download attachment %s: %s", attachment.filename, exc)
    if len(image_datas) > 3:
        logger.debug("Capping %d images to 3", len(image_datas))
        image_datas = image_datas[:3]

    context_lines = await fetch_context_lines(message)
    search_results = None
    is_search_query = _looks_like_search_query(question)
    logger.debug(
        "Mention %s search detection: is_search_query=%s, question=%r",
        message.id,
        is_search_query,
        question,
    )
    if is_search_query:
        if web_search.search_enabled():
            search_results, search_error = await web_search.search_web(question)
            if search_error:
                logger.debug("Web search for mention %s failed: %s", message.id, search_error)
            else:
                logger.info(
                    "Mention %s included web search results (%d chars)",
                    message.id,
                    len(search_results) if search_results else 0,
                )
        else:
            logger.debug(
                "Mention %s looks like a search query, but web search is not configured or enabled",
                message.id,
            )

    async with message.channel.typing():  # the little "typing…" dots
        # Extract mentioned user IDs for context and build a safe mention list.
        mentioned_users = [user for user in message.mentions if user.id != bot.user.id]
        mentioned_user_ids = [user.id for user in mentioned_users]
        allowed_mentions = (
            discord.AllowedMentions(users=mentioned_users)
            if mentioned_users
            else None
        )

        reply, error_kind = await chat_reply(
            message_text=question or "(they pinged you without saying anything)",
            author_name=message.author.display_name,
            bot_name=bot.user.display_name,
            context_lines=context_lines,
            search_results=search_results,
            user_id=message.author.id,
            mentioned_user_ids=mentioned_user_ids,
            image_datas=image_datas or None,
        )
    if error_kind == "quota":
        await bot.notify_owner_once_today(
            "chat_quota",
            "🤖 The chatbot's AI quota is exhausted for now, so @mention replies "
            "are paused (it resets daily, midnight US Pacific time). Detection, "
            "commands, and scheduled reminders still work normally."
        )
        await say(message, "😵‍💫 my head's spinning right now - give me a minute!", allowed_mentions=allowed_mentions)
        return
    if error_kind == "busy":
        # Google's side is overloaded, not our bug - say so honestly.
        await say(message, "🧠 my brain's lagging right now - try me again in a sec!", allowed_mentions=allowed_mentions)
        return
    if error_kind == "safety":
        # Gemini's safety filters blocked the response
        await bot.notify_owner_once_today(
            "chat_safety_block",
            "🤖 The chatbot's response was blocked by safety filters. This can happen with certain topics or language combinations."
        )
        await say(message, "😔 I can't respond to that due to safety guidelines. Let's talk about something else!", allowed_mentions=allowed_mentions)
        return
    if error_kind == "token_limit":
        # Gemini hit token limits
        await bot.notify_owner_once_today(
            "chat_token_limit",
            "🤖 The chatbot's response was cut off due to length limits. Consider asking a more specific question."
        )
        await say(message, "🤔 My response got too long and got cut off. Could you ask a more specific question?", allowed_mentions=allowed_mentions)
        return
    if error_kind == "empty":
        # Generic empty response from Gemini
        await bot.notify_owner_once_today(
            "chat_empty_reply",
            "🤖 The chatbot received an empty response from Gemini (safety filters blocked or empty generation)."
        )
        await say(message, "🤔 my thoughts came up empty - let me try thinking about that differently!", allowed_mentions=allowed_mentions)
        return
    if error_kind == "error":
        # This covers quota errors and other failures from _generate_with_retry
        await bot.notify_owner_once_today(
            "chat_quota_or_error",
            "🤖 The chatbot encountered an error (quota exceeded or API issue). Detection and other functions continue to work."
        )
        await say(message, "😵‍💫 my head's spinning right now - give me a minute!", allowed_mentions=allowed_mentions)
        return
    # Fallback for any unhandled error cases
    if error_kind or not reply:
        await bot.notify_owner_once_today(
            "chat_unhandled_error",
            f"🤖 Unhandled chat error: error_kind={error_kind}, reply_empty={not reply if reply is not None else 'None'}"
        )
        await say(message, "⚠️ Something unexpected happened - let's try again!", allowed_mentions=allowed_mentions)
        return


    logger.info("Chat reply to %s in channel %s", message.author.name, message.channel.id)
    await say(message, reply, allowed_mentions=allowed_mentions)

    # Smart memory: save important information from this conversation
    # This happens in the background and doesn't affect the user experience
    try:
        saved = await smart_memory.save_conversation_memory(
            user_id=message.author.id,
            user_message=question or "",
            bot_reply=reply,
            author_name=message.author.display_name,
        )
        if saved:
            logger.debug("Saved conversation memory for user %s", message.author.id)
    except Exception:
        logger.exception("Failed to save conversation memory (non-critical)")

    # Safety net: if the chat AI claims it set a reminder but the detection path
    # didn't catch it (prescan missed it, low confidence, etc.), try to actually
    # set one now. Look for phrases like "reminder set", "naka-set na", etc.
    reminder_claim_patterns = [
        r"reminder\s+(?:set|created|added|scheduled|#\d+)",  # "reminder set", "Reminder #5"
        r"naka-?\s*set\s+(?:na|ang)\s+(?:reminder|paalala)",  # "naka-set na", "nakaset na"
        r"(?:set|scheduled)\s+(?:a\s+)?reminder",
        r"ipaalala\s+(?:ko|kita)",
        r"noted\s*[—-]\s*reminder",  # "noted —reminder set"
        r"sige.*reminder",  # "Sige! Reminder #5"
    ]
    if any(re.search(pat, reply, re.IGNORECASE) for pat in reminder_claim_patterns):
        logger.warning(
            "Chat reply claimed to set a reminder but detection didn't catch it - "
            "attempting fallback detection for message %s",
            message.id
        )
        # Run detection as a last resort
        analysis, error_kind = await run_detection(message, text=question, in_hot_window=False)
        if not error_kind and analysis and analysis.kind == "reminder":
            if analysis.confidence >= CONFIDENCE_THRESHOLD:
                await create_requested_reminder(message, analysis)
                logger.info(
                    "Fallback detection successfully created reminder for message %s",
                    message.id
                )
            else:
                logger.warning(
                    "Fallback detection found reminder but confidence too low: %.2f < %.2f",
                    analysis.confidence, CONFIDENCE_THRESHOLD
                )
        else:
            logger.warning(
                "Fallback detection failed to extract reminder from message %s: "
                "error=%s, kind=%s",
                message.id, error_kind, analysis.kind if analysis else "none"
            )

    # Raffle safety net: if the chat AI claims it created/ended a raffle but the
    # detection path didn't catch it, try to actually do the action now.
    raffle_claim_patterns = [
        r"raffle\s+(?:created|set up|started|#\d+)",  # "raffle created", "Raffle #5"
        r"giveaway\s+(?:created|set up|started|#\d+)",  # "giveaway created"
        r"naka-?\s*(?:create|set|gawa)\s+(?:na|ang)\s+(?:raffle|giveaway)",  # "nakagawa na ng raffle"
        r"created\s+a\s+raffle",
    ]
    if any(re.search(pat, reply, re.IGNORECASE) for pat in raffle_claim_patterns):
        logger.warning(
            "Chat reply falsely claimed to create a raffle - "
            "running fallback extraction for message %s",
            message.id
        )
        # Use the keyword-based fallback handler (no extra AI call)
        await _handle_raffle_fallback(message, question or "")


async def run_detection(
    message: discord.Message, text: str, in_hot_window: bool
) -> tuple[ChatAnalysis | None, str | None]:
    """Spend one detection AI call on `text` and return what it found.

    Shared by the passive watcher and the @mention path, so both pay the same
    budget, build the same payload, and raise the same owner alerts.

    Returns (analysis, error_kind): (analysis, None) on success, or
    (None, "budget" | "quota" | "busy" | "error") - all already reported.
    """
    if not bot.consume_ai_budget():
        await bot.notify_owner_once_today(
            "daily_cap",
            "🤖 I've used my whole AI budget for today "
            f"(MAX_AI_CALLS_PER_DAY={MAX_AI_CALLS_PER_DAY}), so chat auto-detection is "
            "paused until midnight. /debt and /remind commands and scheduled reminders "
            "still work normally.",
        )
        return None, "budget"

    # Our own mention carries no meaning for the model - drop it from the list.
    mention_lines = [
        f"{user.name} (id {user.id})" + (" - this is the owner" if user.id == OWNER_ID else "")
        for user in message.mentions
        if bot.user is None or user.id != bot.user.id
    ]
    context_lines = await fetch_context_lines(message)
    payload = build_payload(
        message_text=text,
        author_name=message.author.display_name,
        author_is_owner=(message.author.id == OWNER_ID),
        owner_name=bot.owner_user.display_name if bot.owner_user else "the owner",
        mention_lines=mention_lines,
        context_lines=context_lines,
        in_hot_window=in_hot_window,
    )

    analysis, error_kind = await analyze_message(payload)

    if error_kind == "quota":
        await bot.notify_owner_once_today(
            "gemini_quota",
            "🤖 My AI's daily request limit is used up, so chat "
            "auto-detection is paused (it resets daily, midnight US Pacific time). "
            "/debt and /remind commands and scheduled reminders still work normally.",
        )
        return None, "quota"
    if error_kind == "busy":
        # Detection failing is otherwise completely silent, so say something -
        # a model that stays overloaded means missed debts and reminders.
        await bot.notify_owner_once_today(
            "gemini_busy",
            f"🤖 My AI keeps answering 'overloaded' (503) for `{GEMINI_MODEL}`, and "
            f"the backup models aren't getting through either ({', '.join(f'`{m}`' for m in GEMINI_FALLBACK_MODELS if m)}), "
            "so some chat auto-detection is being missed. If it doesn't clear up, set "
            "GEMINI_MODEL in .env to another model and restart. /debt and /remind "
            "commands and scheduled reminders still work normally.",
        )
        return None, "busy"
    return analysis, error_kind


async def offer_to_owner(
    message: discord.Message, analysis: ChatAnalysis, person_id: int | None
) -> None:
    """Send one detection to the owner as a Confirm/Ignore DM."""
    duplicate_note = find_duplicate_note(analysis, person_id)
    embed = build_confirmation_embed(analysis, message, person_id, duplicate_note)
    view = ConfirmView(
        analysis=analysis,
        person_id=person_id,
        channel_id=message.channel.id,
        source_message_id=message.id,
        is_possible_duplicate=duplicate_note is not None,
    )
    sent = await bot.dm_owner(embed=embed, view=view)
    if sent:
        logger.info(
            "Detection sent for confirmation: kind=%s person=%s (message %s)",
            analysis.kind, analysis.counterparty or analysis.update_person, message.id,
        )


@bot.event
async def on_message(message: discord.Message) -> None:
    # Ignore bots (including ourselves) and DMs - we only watch server channels.
    if message.author.bot or message.guild is None:
        return
    if message.id in bot.recently_processed:
        return
    bot.recently_processed.append(message.id)

    # Talking to the bot is its own path: it does its own prescan + detection
    # (a request made straight to us) and falls back to chatting. This has to
    # come before the gates below, which are written for overheard chatter.
    if is_chat_trigger(message):
        await handle_chat_mention(message)
        return

    categories = prescan(message.content)
    hot_window = bot.active_hot_window(message.channel.id, message.author.id)

    if not categories and hot_window is None:
        return  # ordinary chatter - the overwhelmingly common case, zero cost

    logger.debug(
        "Candidate message %s from %s: categories=%s hot_window=%s text=%r",
        message.id, message.author.name, categories or "-", hot_window is not None,
        message.content[:120],
    )

    # Scope gate: debt talk must involve the owner; reminders and raffles can come from
    # anyone; hot-window follow-ups already proved their relevance.
    if hot_window is None and "reminder" not in categories and "raffle" not in categories:
        if not involves_owner(message):
            logger.debug("Skipped %s: debt/raffle keywords but owner not involved", message.id)
            return

    # Keyword-less follow-ups are capped per window; keyword hits always pass.
    if hot_window is not None:
        if not categories and hot_window.ai_calls_used >= MAX_FOLLOWUPS_PER_WINDOW:
            logger.debug("Skipped %s: hot-window follow-up cap reached", message.id)
            return
        hot_window.ai_calls_used += 1

    # Daily AI budget, payload, and the call itself (errors already reported).
    analysis, error_kind = await run_detection(
        message,
        text=message.clean_content,
        in_hot_window=(hot_window is not None and not categories),
    )
    if error_kind:
        return
    if analysis is None or analysis.kind == "none":
        logger.debug("Message %s: AI says not an event", message.id)
        return
    if analysis.confidence < CONFIDENCE_THRESHOLD:
        logger.debug(
            "Message %s: %s below confidence threshold (%.2f < %.2f)",
            message.id, analysis.kind, analysis.confidence, CONFIDENCE_THRESHOLD,
        )
        return

    person_id = guess_person_id(message, analysis)

    # A reminder without a resolvable date can't be scheduled - tell the owner
    # instead of guessing.
    if analysis.kind == "reminder" and parse_due_datetime(analysis.reminder_due) is None:
        await bot.dm_owner(
            f"⏰ I spotted a reminder in {message.channel.mention} but couldn't tell "
            f"*when*: \"{analysis.reminder_text}\" - "
            f"use `/remind add` if you want it scheduled. [Jump]({message.jump_url})"
        )
        bot.open_or_extend_hot_window(
            message.channel.id, {message.author.id, OWNER_ID}
        )
        return

    await offer_to_owner(message, analysis, person_id)

    # Follow-ups ("next month nalang pala") get caught for a while.
    participants = {message.author.id, OWNER_ID}
    if person_id:
        participants.add(person_id)
    bot.open_or_extend_hot_window(message.channel.id, participants)


# ---------------------------------------------------------------------------
# Scheduled delivery: reminders + debt nagging (runs every minute)
# ---------------------------------------------------------------------------

def debt_needs_nag(debt, now: datetime) -> bool:
    """Decide whether an unpaid debt deserves a reminder right now.

    - With a promised date: nag once on that day; if still unpaid, weekly after.
    - Without one: weekly, starting 7 days after the debt was recorded.
    """
    last_nag = datetime.fromisoformat(debt["last_reminded"]) if debt["last_reminded"] else None

    if debt["due_date"]:
        due_day = date.fromisoformat(debt["due_date"])
        if now.date() < due_day:
            return False  # not due yet - stay quiet
        if now.date() == due_day:
            return last_nag is None or last_nag.date() < due_day
        # Overdue: weekly, anchored on the last nag (or the due day itself).
        anchor = last_nag or datetime.combine(due_day, datetime.min.time())
        return (now - anchor).days >= WEEKLY_NAG_DAYS

    anchor = last_nag or datetime.fromisoformat(debt["created_at"])
    return (now - anchor).days >= WEEKLY_NAG_DAYS


async def deliver_reminder(reminder) -> None:
    """Send one due reminder to wherever the settings say.

    A reminder belongs to whoever asked for it, so in DM mode it goes to that
    person - not to the owner - now that anyone can use /remind.
    """
    target_mode = db.get_setting("reminder_target", "dm")
    text = reminder["reminder_text"]
    requester_id = reminder["requester_id"]

    if target_mode == "server" and reminder["channel_id"]:
        channel = bot.get_channel(reminder["channel_id"])
        if channel is not None:
            mention = f"<@{requester_id}> " if requester_id else ""
            await channel.send(f"⏰ {mention}Paalala: {text}")
            return

    if requester_id and requester_id != OWNER_ID:
        try:
            user = bot.get_user(requester_id) or await bot.fetch_user(requester_id)
            await user.send(f"⏰ Paalala: {text}")
            return
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.warning(
                "Couldn't DM reminder %s to user %s - falling back to the channel",
                reminder["id"], requester_id,
            )
        # Their DMs are closed: say it in the channel they asked in instead.
        channel = bot.get_channel(reminder["channel_id"]) if reminder["channel_id"] else None
        if channel is not None:
            await channel.send(f"⏰ <@{requester_id}> Paalala: {text}")
            return

    # The owner's own reminders, and anything with nowhere else to go.
    await bot.dm_owner(f"⏰ Reminder (asked by {reminder['requester_name']}): {text}")


async def send_debt_nag(debt, now: datetime) -> None:
    """Nag about one unpaid debt, respecting the dm/server setting."""
    target_mode = db.get_setting("reminder_target", "dm")
    money = format_money(debt["currency"], debt["amount"])
    for_what = f" for {debt['description']}" if debt["description"] else ""
    due = debt["due_date"]

    # Server mode only makes sense for money owed TO the owner, with a known
    # debtor and source channel; everything else falls back to a DM.
    if (
        target_mode == "server"
        and debt["direction"] == "they_owe_me"
        and debt["person_id"]
        and debt["channel_id"]
    ):
        channel = bot.get_channel(debt["channel_id"])
        if channel is not None:
            due_text = ""
            if due == now.date().isoformat():
                due_text = " - today is the day you promised! 🙂"
            elif due and due < now.date().isoformat():
                due_text = f" - you said you'd pay by {due}!"
            await channel.send(
                f"💰 <@{debt['person_id']}> friendly reminder: you owe "
                f"<@{OWNER_ID}> {money}{for_what}{due_text}"
            )
            return

    days_old = (now - datetime.fromisoformat(debt["created_at"])).days
    if debt["direction"] == "they_owe_me":
        summary = f"💰 {debt['person_name']} owes you {money}{for_what}"
    else:
        summary = f"💸 You owe {debt['person_name']} {money}{for_what}"
    if due:
        summary += f" (promised: {due})"
    summary += f" - unpaid for {days_old} day(s). `/debt paid {debt['person_name']}` settles it."
    await bot.dm_owner(summary)


@tasks.loop(minutes=1)
async def delivery_loop() -> None:
    """Every minute: fire due reminders, and evaluate debt nags."""
    now = datetime.now()

    for reminder in db.due_reminders(now.isoformat(timespec="minutes")):
        try:
            await deliver_reminder(reminder)
            # A repeating reminder is never "done" - it just moves to its next
            # date. Only one-offs get closed out.
            repeat_rule = reminder["repeat_rule"]
            upcoming = next_due_after(
                datetime.fromisoformat(reminder["due_at"]), repeat_rule, now
            )
            if upcoming is not None:
                db.reschedule_reminder(
                    reminder["id"], upcoming.isoformat(timespec="minutes")
                )
                logger.info(
                    "Delivered repeating reminder #%s (%s): %s - next on %s",
                    reminder["id"], repeat_rule, reminder["reminder_text"],
                    upcoming.strftime("%Y-%m-%d %H:%M"),
                )
            else:
                db.mark_reminder_delivered(reminder["id"])
                logger.info(
                    "Delivered reminder #%s: %s", reminder["id"], reminder["reminder_text"]
                )
        except Exception:
            logger.exception("Failed to deliver reminder #%s", reminder["id"])

    for debt in db.unpaid_debts():
        try:
            if debt_needs_nag(debt, now):
                await send_debt_nag(debt, now)
                db.mark_debt_reminded(debt["id"], now.isoformat(timespec="seconds"))
                logger.info("Nagged about debt #%s (%s)", debt["id"], debt["person_name"])
        except Exception:
            logger.exception("Failed to send nag for debt #%s", debt["id"])

    # Auto-end timed-out raffles
    for raffle in db.get_expired_raffles():
        try:
            result = db.end_raffle(raffle["id"])
            if result:
                channel = bot.get_channel(raffle["channel_id"])
                if channel:
                    prize_str = format_money(raffle["currency"], result["prize_pool"])
                    await channel.send(
                        f"⏰ Time's up! Raffle #{raffle['id']} has ended! "
                        f"**{result['winner_name']}** won {prize_str}! "
                        f"({result['participant_count']} participants)"
                    )
                    logger.info(
                        "Auto-ended expired raffle #%s, winner: %s",
                        raffle["id"], result["winner_name"],
                    )
        except Exception:
            logger.exception("Failed to auto-end expired raffle #%s", raffle["id"])


@delivery_loop.before_loop
async def wait_for_bot() -> None:
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Google Calendar sync
# Scheduled events in allowed servers are mirrored onto every linked
# calendar. Live changes arrive via the gateway handlers below; the reconcile
# loop heals whatever they miss (offline gaps, Google hiccups, new links).
# Unlike chat detection there are no Confirm/Ignore prompts here - scheduled
# events are explicit structured data, so syncing them is safe to automate.
# ---------------------------------------------------------------------------

def calendar_sync_active() -> bool:
    """Key file present and the owner hasn't switched sync off via /settings."""
    return calendar_sync.is_configured() and db.get_setting("calendar_sync", "on") == "on"


def calendar_event_in_scope(guild_id: int | None) -> bool:
    """Should we mirror events from this guild at all?"""
    return calendar_sync_active() and guild_id in CALENDAR_GUILD_IDS


def calendar_targets() -> list[str]:
    """Every calendar to mirror onto: the owner's .env one plus all linked."""
    targets = [GOOGLE_CALENDAR_ID] if GOOGLE_CALENDAR_ID else []
    for row in db.all_linked_calendars():
        if row["calendar_id"] not in targets:
            targets.append(row["calendar_id"])
    return targets


async def sync_event_everywhere(event: discord.ScheduledEvent) -> None:
    """Mirror one Discord event onto every target calendar.

    Per-calendar try/except: one revoked share must never block the others.
    Failures are logged, the owner is told (once a day at most), and the next
    reconcile pass retries automatically.
    """
    fingerprint = calendar_sync.content_hash(event)
    for calendar_id in calendar_targets():
        existing = db.get_event_sync(event.id, calendar_id)
        if existing is not None and existing["content_hash"] == fingerprint:
            continue  # this copy is already up to date - no API call needed
        try:
            gcal_id = await calendar_sync.upsert_event(
                event, calendar_id, existing["gcal_event_id"] if existing else None
            )
        except Exception:
            logger.exception("Calendar: failed to sync '%s' to %s", event.name, calendar_id)
            await bot.notify_owner_once_today(
                "calendar",
                f"📅 Google Calendar sync hit an error while updating \"{event.name}\". "
                "I'll keep retrying every few hours - check the logs for details.",
            )
            continue
        db.save_event_sync(
            discord_event_id=event.id,
            calendar_id=calendar_id,
            gcal_event_id=gcal_id,
            guild_id=event.guild_id,
            event_name=event.name,
            start_time=event.start_time.isoformat(),
            content_hash=fingerprint,
        )
        logger.info("Calendar: synced '%s' to %s", event.name, calendar_id)


async def remove_event_everywhere(discord_event_id: int, event_name: str) -> None:
    """Take a cancelled Discord event off every calendar it was copied to."""
    for row in db.event_syncs_for_event(discord_event_id):
        try:
            await calendar_sync.delete_event(row["calendar_id"], row["gcal_event_id"])
        except Exception:
            # Keep the mapping so the reconcile loop retries the removal.
            logger.exception(
                "Calendar: failed to remove '%s' from %s", event_name, row["calendar_id"]
            )
            continue
        db.delete_event_sync(discord_event_id, row["calendar_id"])
        logger.info("Calendar: removed '%s' from %s", event_name, row["calendar_id"])


@bot.event
async def on_scheduled_event_create(event: discord.ScheduledEvent) -> None:
    if not calendar_event_in_scope(event.guild_id):
        return
    await sync_event_everywhere(event)


@bot.event
async def on_scheduled_event_update(
    before: discord.ScheduledEvent, after: discord.ScheduledEvent  # pylint: disable=unused-argument
) -> None:
    if not calendar_event_in_scope(after.guild_id):
        return
    if after.status is discord.EventStatus.cancelled:
        await remove_event_everywhere(after.id, after.name)
    else:
        # Covers edits AND events we somehow never saw being created.
        await sync_event_everywhere(after)


@bot.event
async def on_scheduled_event_delete(event: discord.ScheduledEvent) -> None:
    if not calendar_event_in_scope(event.guild_id):
        return
    # Discord auto-deletes events once they finish. Those stay on calendars
    # as history; only events deleted before they started get removed.
    if event.start_time <= datetime.now(timezone.utc):
        for row in db.event_syncs_for_event(event.id):
            db.delete_event_sync(event.id, row["calendar_id"])
        logger.debug("Calendar: '%s' already happened - keeping calendar entries", event.name)
    else:
        await remove_event_everywhere(event.id, event.name)


async def sync_all_live_events() -> tuple[set[int], set[int]]:
    """Push every upcoming/active event to every calendar.

    Returns (guild ids we successfully listed, event ids seen) so the
    reconcile loop can safely decide what disappeared. The content-hash check
    makes repeat passes nearly free.
    """
    fetched_guilds: set[int] = set()
    live_event_ids: set[int] = set()
    for guild_id in CALENDAR_GUILD_IDS:
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue  # not a member of this guild (or a typo in .env)
        try:
            events = await guild.fetch_scheduled_events()
        except Exception:
            logger.exception("Calendar: could not list events for guild %s", guild_id)
            continue
        fetched_guilds.add(guild_id)
        for event in events:
            if event.status not in (discord.EventStatus.scheduled, discord.EventStatus.active):
                continue  # completed/cancelled are handled via mappings below
            live_event_ids.add(event.id)
            await sync_event_everywhere(event)
    return fetched_guilds, live_event_ids


@tasks.loop(hours=6)
async def calendar_reconcile_loop() -> None:
    """Heal drift the live handlers can miss.

    Covers events created/edited/cancelled while the bot was offline, failed
    Google calls, and backfilling freshly linked calendars. The first run
    fires right after startup.
    """
    if not calendar_sync_active():
        return
    fetched_guilds, live_event_ids = await sync_all_live_events()

    # Mappings whose Discord event no longer exists: it was cancelled, or it
    # already happened, while we weren't looking.
    now_iso = datetime.now(timezone.utc).isoformat()
    for row in db.all_event_syncs():
        if row["guild_id"] not in fetched_guilds:
            continue  # couldn't check that guild this pass - don't guess
        if row["discord_event_id"] in live_event_ids:
            continue
        if row["start_time"] and row["start_time"] <= now_iso:
            # It ran to completion - keep the calendar entry as history.
            db.delete_event_sync(row["discord_event_id"], row["calendar_id"])
        else:
            try:
                await calendar_sync.delete_event(row["calendar_id"], row["gcal_event_id"])
            except Exception:
                logger.exception(
                    "Calendar: failed removing '%s' from %s",
                    row["event_name"], row["calendar_id"],
                )
                continue
            db.delete_event_sync(row["discord_event_id"], row["calendar_id"])
            logger.info(
                "Calendar: removed vanished event '%s' from %s",
                row["event_name"], row["calendar_id"],
            )


@calendar_reconcile_loop.before_loop
async def wait_for_bot_before_calendar() -> None:
    await bot.wait_until_ready()


# --- /calendar commands - deliberately open to ALL members -----------------
# Each person manages only their own calendar link, so there is no owner
# gate here (unlike every other command). Replies stay ephemeral.

# extras={"public": True} is only read by /help to decide who to list this for;
# discord.py never touches it, so it changes no permissions.
calendar_group = app_commands.Group(
    name="calendar",
    description="Sync server events to your own Google Calendar",
    extras={"public": True},
)

_HOW_TO_LINK = (
    "**How to link your Google Calendar:**\n"
    "1. Google Calendar → hover your calendar → ⋮ → **Settings and sharing**\n"
    "2. Under **Share with specific people**, add **{email}** with "
    "*Make changes to events*\n"
    "3. Copy the **Calendar ID** further down that same page\n"
    "4. Run `/calendar link calendar_id:<that id>`"
)


@calendar_group.command(
    name="link", description="Sync this server's events to YOUR Google Calendar"
)
@app_commands.describe(
    calendar_id="Your Calendar ID (Google Calendar -> Settings and sharing -> 'Calendar ID')"
)
async def calendar_link(interaction: discord.Interaction, calendar_id: str) -> None:
    if not calendar_sync.is_configured():
        await interaction.response.send_message(
            "Calendar sync isn't set up on this bot yet - ask the owner.", ephemeral=True
        )
        return
    if interaction.guild_id not in CALENDAR_GUILD_IDS:
        await interaction.response.send_message(
            "Calendar sync isn't enabled for this server.", ephemeral=True
        )
        return
    calendar_id = calendar_id.strip()
    # Verification does a real write to Google, which can take a few seconds -
    # defer so the interaction doesn't time out at the 3s mark.
    await interaction.response.defer(ephemeral=True)
    problem = await calendar_sync.verify_write_access(calendar_id)
    if problem is not None:
        await interaction.followup.send(
            f"❌ Couldn't write to that calendar ({problem}).\n\n"
            + _HOW_TO_LINK.format(email=calendar_sync.service_email())
        )
        return
    db.save_linked_calendar(interaction.user.id, interaction.user.name, calendar_id)
    # Backfill: put every upcoming event onto the new calendar right away
    # (the content-hash check keeps this cheap for everyone else's calendars).
    await sync_all_live_events()
    count = len(db.event_syncs_for_calendar(calendar_id))
    await interaction.followup.send(
        f"✅ Linked! {count} upcoming event(s) added to your calendar. New events "
        "and changes will appear automatically. `/calendar unlink` stops it anytime."
    )


@calendar_group.command(
    name="unlink", description="Stop syncing and remove bot-added events from your calendar"
)
async def calendar_unlink(interaction: discord.Interaction) -> None:
    row = db.get_linked_calendar(interaction.user.id)
    if row is None:
        await interaction.response.send_message(
            "You don't have a linked calendar.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True)
    # Clean our events off their calendar before forgetting the link.
    removed = 0
    for sync_row in db.event_syncs_for_calendar(row["calendar_id"]):
        try:
            await calendar_sync.delete_event(row["calendar_id"], sync_row["gcal_event_id"])
            removed += 1
        except Exception:
            logger.exception("Calendar: unlink cleanup failed for %s", row["calendar_id"])
        db.delete_event_sync(sync_row["discord_event_id"], row["calendar_id"])
    db.delete_linked_calendar(interaction.user.id)
    await interaction.followup.send(
        f"✅ Unlinked. Removed {removed} synced event(s) from your calendar. "
        "You can also un-share the calendar from the bot in Google Calendar settings."
    )


@calendar_group.command(name="status", description="Your calendar link status and setup help")
async def calendar_status(interaction: discord.Interaction) -> None:
    if not calendar_sync.is_configured():
        await interaction.response.send_message(
            "Calendar sync isn't set up on this bot yet - ask the owner.", ephemeral=True
        )
        return
    row = db.get_linked_calendar(interaction.user.id)
    if row is not None:
        count = len(db.event_syncs_for_calendar(row["calendar_id"]))
        text = (
            f"🔗 Linked to `{row['calendar_id']}` since {row['linked_at']} "
            f"({count} event(s) currently synced)."
        )
    else:
        text = "You have no linked calendar yet.\n\n" + _HOW_TO_LINK.format(
            email=calendar_sync.service_email()
        )
    if not calendar_sync_active():
        text += "\n\n⚠️ Note: the owner has calendar sync switched OFF right now."
    await interaction.response.send_message(text, ephemeral=True)


# ---------------------------------------------------------------------------
# Slash commands (owner-only, ephemeral replies)
# ---------------------------------------------------------------------------

async def ensure_owner(interaction: discord.Interaction) -> bool:
    """All commands are private to the bot owner."""
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message(
            "Sorry, only the bot owner can use this command.", ephemeral=True
        )
        return False
    return True


# /debt and /settings are owner-only (every command calls ensure_owner), so they
# are hidden from everyone else's slash-command menu too - otherwise friends see
# /debt and know there is a ledger. Empty permissions = "server administrators
# only" in Discord's UI; ensure_owner is still what actually enforces access.
# NOTE: if the owner is not an administrator in the server, these vanish for
# them as well - fix that in Server Settings -> Integrations -> the bot.
_OWNER_ONLY = discord.Permissions.none()

debt_group = app_commands.Group(
    name="debt", description="Track who owes who", default_permissions=_OWNER_ONLY
)
# /remind is for everyone: each person sees and manages only their own reminders.
remind_group = app_commands.Group(
    name="remind", description="Set and manage your reminders", extras={"public": True}
)
settings_group = app_commands.Group(
    name="settings", description="Bot settings", default_permissions=_OWNER_ONLY
)

# Chatbot command group - owner-only
chatbot_group = app_commands.Group(
    name="chatbot", description="Adjust chatbot settings", default_permissions=_OWNER_ONLY
)

# Raffle commands - available to everyone
raffle_group = app_commands.Group(
    name="raffle", description="Join and manage raffles", extras={"public": True}
)


async def _add_debt_command(
    interaction: discord.Interaction,
    direction: str,
    person: str,
    amount: float,
    description: str,
    currency: str,
    due: str | None,
) -> None:
    """Shared body of /debt add and /debt iou."""
    if not await ensure_owner(interaction):
        return
    due_date = parse_plain_date(due)
    if due and due_date is None:
        await interaction.response.send_message(
            f"I couldn't read `{due}` as a date - please use YYYY-MM-DD.", ephemeral=True
        )
        return
    person_id, person_name = await resolve_person_argument(person, interaction.guild)

    duplicate = db.find_similar_debt(person_name, person_id, amount)
    warning = ""
    if duplicate is not None:
        warning = (
            f"\n⚠️ Note: open debt #{duplicate['id']} already exists for "
            f"{duplicate['person_name']} with the same amount."
        )

    debt_id = db.add_debt(
        direction=direction,
        person_name=person_name,
        person_id=person_id,
        amount=amount,
        currency=currency or DEFAULT_CURRENCY,
        description=description,
        channel_id=interaction.channel_id,
        due_date=due_date,
    )
    who = f"{person_name} owes you" if direction == "they_owe_me" else f"You owe {person_name}"
    due_text = f", promised by {due_date}" if due_date else ""
    await interaction.response.send_message(
        f"✅ Recorded debt #{debt_id}: {who} "
        f"{format_money(currency or DEFAULT_CURRENCY, amount)}"
        + (f" ({description})" if description else "")
        + due_text + warning,
        ephemeral=True,
    )


@debt_group.command(name="add", description="Record money someone owes YOU")
@app_commands.describe(
    person="@mention them, or type any name (works for people outside Discord)",
    amount="How much they owe you",
    description="What it's for (optional)",
    currency="Currency symbol/code (default ₱)",
    due="Promised pay date, YYYY-MM-DD (optional)",
)
async def debt_add(
    interaction: discord.Interaction,
    person: str,
    amount: float,
    description: str = "",
    currency: str = "",
    due: str | None = None,
) -> None:
    await _add_debt_command(interaction, "they_owe_me", person, amount, description, currency, due)


@debt_group.command(name="iou", description="Record money YOU owe someone")
@app_commands.describe(
    person="@mention them, or type any name",
    amount="How much you owe them",
    description="What it's for (optional)",
    currency="Currency symbol/code (default ₱)",
    due="When you plan to pay, YYYY-MM-DD (optional)",
)
async def debt_iou(
    interaction: discord.Interaction,
    person: str,
    amount: float,
    description: str = "",
    currency: str = "",
    due: str | None = None,
) -> None:
    await _add_debt_command(interaction, "i_owe_them", person, amount, description, currency, due)


@debt_group.command(name="paid", description="Mark a debt as settled")
@app_commands.describe(
    person="Who settled up (@mention or name)",
    all_debts="Settle ALL their open debts, not just the most recent",
)
async def debt_paid(
    interaction: discord.Interaction, person: str, all_debts: bool = False
) -> None:
    if not await ensure_owner(interaction):
        return
    person_id, person_name = await resolve_person_argument(person, interaction.guild)
    open_debts = db.open_debts_for(person_name, person_id)
    if not open_debts:
        await interaction.response.send_message(
            f"No open debts found for {person_name}.", ephemeral=True
        )
        return

    settled = open_debts if all_debts else open_debts[:1]
    for debt in settled:
        db.mark_paid(debt["id"])
    summaries = ", ".join(
        f"#{d['id']} {format_money(d['currency'], d['amount'])}" for d in settled
    )
    remaining = len(open_debts) - len(settled)
    remaining_text = (
        f" ({remaining} other open debt(s) remain - use `all_debts: True` to settle everything)"
        if remaining else ""
    )
    await interaction.response.send_message(
        f"✅ Settled with {person_name}: {summaries}.{remaining_text}", ephemeral=True
    )


@debt_group.command(name="list", description="Show the ledger")
@app_commands.describe(include_paid="Also show already-settled debts")
async def debt_list(interaction: discord.Interaction, include_paid: bool = False) -> None:
    if not await ensure_owner(interaction):
        return
    debts = db.list_debts(include_paid)
    if not debts:
        await interaction.response.send_message("The ledger is empty. 🎉", ephemeral=True)
        return

    owed_to_me: list[str] = []
    i_owe: list[str] = []
    totals: dict[tuple[str, str], float] = {}  # (direction, currency) -> sum of open debts

    for debt in debts:
        is_paid = debt["paid_at"] is not None
        line = f"`#{debt['id']}` **{debt['person_name']}** - {format_money(debt['currency'], debt['amount'])}"
        if debt["description"]:
            line += f" ({debt['description']})"
        if debt["due_date"] and not is_paid:
            line += f" - due {debt['due_date']}"
        line += " ✅ paid" if is_paid else ""
        (owed_to_me if debt["direction"] == "they_owe_me" else i_owe).append(line)
        if not is_paid:
            key = (debt["direction"], debt["currency"])
            totals[key] = totals.get(key, 0.0) + debt["amount"]

    def totals_line(direction: str) -> str:
        parts = [
            format_money(currency, value)
            for (dir_key, currency), value in totals.items()
            if dir_key == direction
        ]
        return " + ".join(parts) if parts else "nothing"

    sections = []
    if owed_to_me:
        sections.append(
            "__**They owe you**__\n" + "\n".join(owed_to_me)
            + f"\n**Total open: {totals_line('they_owe_me')}**"
        )
    if i_owe:
        sections.append(
            "__**You owe**__\n" + "\n".join(i_owe)
            + f"\n**Total open: {totals_line('i_owe_them')}**"
        )
    text = "\n\n".join(sections)
    if len(text) > 1900:  # Discord message cap is 2000 characters
        text = text[:1900] + "\n… (list truncated)"
    await interaction.response.send_message(text, ephemeral=True)


@remind_group.command(name="add", description="Set a reminder for yourself")
@app_commands.describe(
    text="What to be reminded about",
    when="YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (24h) - the FIRST time it fires",
    repeat="Repeat it on a schedule instead of just once",
    force="Create it even if a similar reminder already exists",
)
@app_commands.choices(
    repeat=[
        app_commands.Choice(name="Just once (default)", value="once"),
        app_commands.Choice(name="Every day", value="daily"),
        app_commands.Choice(name="Every week (same weekday)", value="weekly"),
        app_commands.Choice(name="Every month (same date)", value="monthly"),
        app_commands.Choice(name="Every year", value="yearly"),
    ]
)
async def remind_add(
    interaction: discord.Interaction,
    text: str,
    when: str,
    repeat: app_commands.Choice[str] | None = None,
    force: bool = False,
) -> None:
    # Anyone may set reminders - they only ever get their own back.
    due_moment = parse_due_datetime(when)
    if due_moment is None:
        await interaction.response.send_message(
            f"I couldn't read `{when}` - please use `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`.",
            ephemeral=True,
        )
        return

    duplicate = db.find_similar_reminder(due_moment.date().isoformat(), text)
    # Someone else's similar reminder is none of your business - only warn about
    # a clash with your own.
    if duplicate is not None and duplicate["requester_id"] != interaction.user.id:
        duplicate = None
    if duplicate is not None and not force:
        await interaction.response.send_message(
            f"⚠️ Looks like a duplicate of reminder #{duplicate['id']}: "
            f"\"{duplicate['reminder_text']}\" at {duplicate['due_at'].replace('T', ' ')}.\n"
            "Re-run with `force: True` if you want it anyway.",
            ephemeral=True,
        )
        return

    repeat_rule = repeat.value if repeat and repeat.value != "once" else None
    reminder_id = db.add_reminder(
        reminder_text=text,
        due_at=due_moment.isoformat(timespec="minutes"),
        requester_name=interaction.user.display_name,
        requester_id=interaction.user.id,
        channel_id=interaction.channel_id,
        repeat_rule=repeat_rule,
    )
    repeat_note = f" - then {repeat_rule}" if repeat_rule else ""
    await interaction.response.send_message(
        f"⏰ Reminder #{reminder_id} set for {due_moment.strftime('%Y-%m-%d %H:%M')}"
        f"{repeat_note}: {text}",
        ephemeral=True,
    )


@remind_group.command(name="list", description="Show your pending reminders")
async def remind_list(interaction: discord.Interaction) -> None:
    reminders = db.pending_reminders()
    # Everyone sees only their own; the owner sees the whole schedule.
    is_owner = interaction.user.id == OWNER_ID
    if not is_owner:
        reminders = [r for r in reminders if r["requester_id"] == interaction.user.id]
    if not reminders:
        await interaction.response.send_message(
            "No pending reminders." if is_owner else "You have no pending reminders.",
            ephemeral=True,
        )
        return
    lines = [
        f"`#{r['id']}` {r['due_at'].replace('T', ' ')} - {r['reminder_text']}"
        + (f" 🔁 {r['repeat_rule']}" if r["repeat_rule"] else "")
        + (f" (asked by {r['requester_name']})" if is_owner else "")
        for r in reminders
    ]
    heading = "Pending reminders" if is_owner else "Your pending reminders"
    text = f"__**{heading}**__\n" + "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n… (list truncated)"
    await interaction.response.send_message(text, ephemeral=True)


@remind_group.command(name="delete", description="Delete one of your reminders")
@app_commands.describe(reminder_id="The #id shown in /remind list")
async def remind_delete(interaction: discord.Interaction, reminder_id: int) -> None:
    # You can only delete your own; the owner can delete any. Answering "not
    # found" rather than "not yours" keeps other people's reminders private.
    existing = db.get_reminder(reminder_id)
    if existing is not None and interaction.user.id != OWNER_ID:
        if existing["requester_id"] != interaction.user.id:
            existing = None
    if existing is None:
        await interaction.response.send_message(
            f"No reminder #{reminder_id} found.", ephemeral=True
        )
        return
    if db.delete_reminder(reminder_id):
        await interaction.response.send_message(
            f"🗑️ Reminder #{reminder_id} deleted.", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"No reminder #{reminder_id} found.", ephemeral=True
        )


@settings_group.command(name="reminders", description="Choose where reminders are delivered")
@app_commands.choices(
    target=[
        app_commands.Choice(name="DM me only (test mode, default)", value="dm"),
        app_commands.Choice(name="Post in the server and mention people", value="server"),
    ]
)
async def settings_reminders(
    interaction: discord.Interaction, target: app_commands.Choice[str]
) -> None:
    if not await ensure_owner(interaction):
        return
    db.set_setting("reminder_target", target.value)
    logger.info("Reminder delivery mode set to: %s", target.value)
    await interaction.response.send_message(
        f"🔧 Reminder delivery is now: **{target.name}**", ephemeral=True
    )


@settings_group.command(name="calendar", description="Turn Google Calendar event sync on or off")
@app_commands.choices(
    state=[
        app_commands.Choice(name="On (default)", value="on"),
        app_commands.Choice(name="Off - pause all calendar syncing", value="off"),
    ]
)
async def settings_calendar(
    interaction: discord.Interaction, state: app_commands.Choice[str]
) -> None:
    if not await ensure_owner(interaction):
        return
    db.set_setting("calendar_sync", state.value)
    logger.info("Calendar sync switched: %s", state.value)
    await interaction.response.send_message(
        f"🔧 Calendar sync is now: **{state.name}**", ephemeral=True
    )


# What each activity choice looks like in Discord's member list - used to
# echo the result back so you see exactly what your friends will see.
_STATUS_VERBS = {
    "playing": "Playing",
    "listening": "Listening to",
    "watching": "Watching",
    "competing": "Competing in",
    "custom": "",
}


# Raffle command group - public so anyone can use
raffle_group = app_commands.Group(
    name="raffle",
    description="Join raffles and win virtual currency",
    extras={"public": True},
)

# Chatbot command group - owner-only
chatbot_group = app_commands.Group(
    name="chatbot", description="Adjust chatbot settings", default_permissions=_OWNER_ONLY
)


@settings_group.command(name="status", description='Set the bot\'s status (e.g. "Playing DOOM")')
@app_commands.describe(
    activity="How the status is phrased",
    text="The status text, e.g. DOOM (not needed when clearing)",
)
@app_commands.choices(
    activity=[
        app_commands.Choice(name="Playing …", value="playing"),
        app_commands.Choice(name="Listening to …", value="listening"),
        app_commands.Choice(name="Watching …", value="watching"),
        app_commands.Choice(name="Competing in …", value="competing"),
        app_commands.Choice(name="Custom text (no verb)", value="custom"),
        app_commands.Choice(name="Clear the status", value="clear"),
    ]
)
async def settings_status(
    interaction: discord.Interaction,
    activity: app_commands.Choice[str],
    text: str = "",
) -> None:
    if not await ensure_owner(interaction):
        return
    if activity.value == "clear":
        db.set_setting("status_text", "")
        await bot.change_presence(activity=None)
        await interaction.response.send_message("🔧 Status cleared.", ephemeral=True)
        return
    if not text:
        await interaction.response.send_message(
            "Please add the status text too, e.g. `text: DOOM`.", ephemeral=True
        )
        return
    # Persist first so the status survives restarts (re-applied in on_ready).
    db.set_setting("status_type", activity.value)
    db.set_setting("status_text", text)
    await bot.change_presence(activity=build_activity(activity.value, text))
    logger.info("Bot status set: %s %s", activity.value, text)
    shown = f"{_STATUS_VERBS[activity.value]} {text}".strip()
    await interaction.response.send_message(
        f"🔧 Status is now: **{shown}**", ephemeral=True
    )


# Raffle command implementations
@raffle_group.command(name="create", description="Create a new raffle")
@app_commands.describe(
    prize="Prize money the winner will receive (leave blank for random amount)",
    max_participants="Maximum number of participants (0 = unlimited)",
    duration_minutes="How long the raffle runs (default 1 min)",
    description="Description of what's being raffled"
)
async def raffle_create(
    interaction: discord.Interaction,
    prize: app_commands.Range[float, 0] | None = None,
    max_participants: app_commands.Range[int, 0] = 0,
    duration_minutes: app_commands.Range[int, 0] = 1,
    description: str = ""
) -> None:
    """Create a new raffle"""
    # Anyone can create a raffle
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # If no prize specified, generate a random amount between 100-5000
    if prize is None:
        import random
        prize = random.choice([100, 200, 500, 1000, 2000, 5000])

    # Calculate end time if duration specified
    ends_at = None
    if duration_minutes > 0:
        ends_at = (datetime.now() + timedelta(minutes=duration_minutes)).isoformat(timespec="minutes")

    # Create the raffle
    # Get the guild's default auto-join role if auto-join is enabled
    auto_join_role_id = None
    if db.get_guild_auto_join_enabled(guild_id):
        auto_join_role_id = db.get_guild_auto_join_role(guild_id)

    raffle_id = db.create_raffle(
        prize_description=description,
        prize_amount=prize,
        currency=DEFAULT_CURRENCY,
        creator_name=interaction.user.display_name,
        creator_id=interaction.user.id,
        guild_id=guild_id,
        channel_id=interaction.channel_id,
        max_participants=max_participants if max_participants > 0 else None,
        ends_at=ends_at,
        auto_join_role_id=auto_join_role_id,
        active=True
    )

    # Auto-join the creator
    db.join_raffle(raffle_id, interaction.user.id, interaction.user.display_name, "creator")

    # Format the response
    prize_str = format_money(DEFAULT_CURRENCY, prize)
    duration_str = f"{duration_minutes} minutes" if duration_minutes > 0 else "until ended manually"
    limit_str = f"{max_participants} participants" if max_participants > 0 else "unlimited participants"

    embed = discord.Embed(
        title=f"🎉 Raffle #{raffle_id} Created!",
        description=description or "No description provided",
        color=0xFFD700  # Gold color
    )
    embed.add_field(name="💰 Prize", value=prize_str, inline=True)
    embed.add_field(name="⏰ Duration", value=duration_str, inline=True)
    embed.add_field(name="👥 Limit", value=limit_str, inline=True)
    embed.add_field(name="🎯 How to Join", value="Use `/raffle join` — it's free!", inline=False)
    if auto_join_role_id:
        embed.add_field(name="🎖️ Auto-Join", value=f"Users with <@&{auto_join_role_id}> are automatically entered!", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=False)


@raffle_group.command(name="join", description="Join an active raffle")
@app_commands.describe(
    raffle_id="ID of the raffle to join (leave blank to join most recent active raffle)"
)
async def raffle_join(
    interaction: discord.Interaction,
    raffle_id: int | None = None
) -> None:
    """Join a raffle"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    user_id = interaction.user.id
    user_name = interaction.user.display_name

    # Get the raffle to join
    if raffle_id is None:
        # Get most recent active raffle in this guild/channel
        raffles = db.get_active_raffles(interaction.channel_id, guild_id)
        if not raffles:
            await interaction.response.send_message("No active raffles found in this channel.", ephemeral=True)
            return
        raffle = raffles[0]  # Most recent
    else:
        raffle = db.get_raffle(raffle_id)
        if not raffle:
            await interaction.response.send_message(f"Raffle #{raffle_id} not found.", ephemeral=True)
            return
        # Check if raffle is in the same guild/channel
        if raffle["guild_id"] != guild_id or raffle["channel_id"] != interaction.channel_id:
            await interaction.response.send_message("That raffle is not in this channel.", ephemeral=True)
            return

    # Check if raffle is still active
    if not raffle["active"]:
        await interaction.response.send_message(f"Raffle #{raffle['id']} has already ended.", ephemeral=True)
        return

    # Check if user already participated
    participants = db.get_raffle_participants(raffle["id"])
    if any(p["user_id"] == user_id for p in participants):
        await interaction.response.send_message(f"You're already in raffle #{raffle['id']}!", ephemeral=True)
        return

    # Check if raffle is full
    if raffle["max_participants"] and len(participants) >= raffle["max_participants"]:
        await interaction.response.send_message(f"Raffle #{raffle['id']} is already full!", ephemeral=True)
        return

    # Add participant (joining is free!)
    if db.join_raffle(raffle["id"], user_id, user_name, "command"):
        await interaction.response.send_message(
            f"✅ You've joined raffle #{raffle['id']}! "
            f"Prize pool: {format_money(raffle['currency'], db._raffle_prize(raffle))}. "
            f"Good luck! 🍀",
            ephemeral=False
        )
    else:
        await interaction.response.send_message("Failed to join raffle. Please try again.", ephemeral=True)


@raffle_group.command(name="leave", description="Leave a raffle you've joined")
@app_commands.describe(
    raffle_id="ID of the raffle to leave (leave blank to leave most recent raffle you joined)"
)
async def raffle_leave(
    interaction: discord.Interaction,
    raffle_id: int | None = None
) -> None:
    """Leave a raffle"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    user_id = interaction.user.id

    # Get the raffle to leave
    if raffle_id is None:
        # Get raffles the user has joined in this guild/channel
        participations = db.get_user_raffles(user_id, guild_id, interaction.channel_id)
        if not participations:
            await interaction.response.send_message("You haven't joined any raffles in this channel.", ephemeral=True)
            return
        raffle = participations[0]  # Most recent
    else:
        raffle = db.get_raffle(raffle_id)
        if not raffle:
            await interaction.response.send_message(f"Raffle #{raffle_id} not found.", ephemeral=True)
            return
        # Check if raffle is in the same guild/channel
        if raffle["guild_id"] != guild_id or raffle["channel_id"] != interaction.channel_id:
            await interaction.response.send_message("That raffle is not in this channel.", ephemeral=True)
            return

    # Check if raffle is still active
    if not raffle["active"]:
        await interaction.response.send_message(f"Raffle #{raffle['id']} has already ended.", ephemeral=True)
        return

    # Check if user actually participated
    participant = db.get_raffle_participant(raffle["id"], user_id)
    if not participant:
        await interaction.response.send_message(f"You're not in raffle #{raffle['id']}.", ephemeral=True)
        return

    # Remove participant (joining was free, so no refund needed)
    if db.leave_raffle(raffle["id"], user_id):
        await interaction.response.send_message(
            f"✅ You've left raffle #{raffle['id']}.",
            ephemeral=False
        )
    else:
        await interaction.response.send_message("Failed to leave raffle. Please try again.", ephemeral=True)


@raffle_group.command(name="end", description="End a raffle and pick a winner (creator only)")
@app_commands.describe(
    raffle_id="ID of the raffle to end"
)
async def raffle_end(
    interaction: discord.Interaction,
    raffle_id: int
) -> None:
    """End a raffle and pick a winner"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    user_id = interaction.user.id

    # Get the raffle
    raffle = db.get_raffle(raffle_id)
    if not raffle:
        await interaction.response.send_message(f"Raffle #{raffle_id} not found.", ephemeral=True)
        return

    # Check if raffle is in the same guild/channel
    if raffle["guild_id"] != guild_id or raffle["channel_id"] != interaction.channel_id:
        await interaction.response.send_message("That raffle is not in this channel.", ephemeral=True)
        return

    # Check if raffle is still active
    if not raffle["active"]:
        await interaction.response.send_message(f"Raffle #{raffle_id} has already ended.", ephemeral=True)
        return

    # Check if user is the creator
    if raffle["creator_id"] != user_id:
        await interaction.response.send_message("Only the raffle creator can end the raffle.", ephemeral=True)
        return

    # Defer response since winner selection might take a moment
    await interaction.response.defer(ephemeral=False)

    # Add 3-second loading animation with influencer style
    await interaction.followup.send("🎵 *SPINNING THE WHEEL...* 🎵")
    await asyncio.sleep(1)
    await interaction.edit_original_response(content="🎵 *ALMOST THERE...* 🎵")
    await asyncio.sleep(1)
    await interaction.edit_original_response(content="🎵 *AND THE WINNER IS...* 🎵")
    await asyncio.sleep(1)

    # End the raffle and get winner
    result = db.end_raffle(raffle_id)

    if not result:
        await interaction.edit_original_response(content="❌ Failed to end raffle. Please try again.")
        return

    winner_id = result["winner_id"]
    prize_amount = result["prize_pool"]

    if winner_id:
        winner_name = result["winner_name"]

        # Get winner's new balance (already updated in db.end_raffle)
        new_balance, new_currency = db.get_user_balance(winner_id, guild_id)

        # Create celebration message
        embed = discord.Embed(
            title=f"🎉🎉🎉 RAFFLE #{raffle_id} ENDED! 🎉🎉🎉",
            description=f"**{winner_name.mention if hasattr(winner_name, 'mention') else winner_name}** won **{format_money(raffle['currency'], prize_amount)}**! 💰",
            color=0xFF0000  # Red for excitement
        )
        embed.add_field(
            name="📊 Raffle Stats",
            value=f"• Participants: {result['participant_count']}\n"
                  f"• Prize Pool: {format_money(raffle['currency'], prize_amount)}",
            inline=False
        )
        embed.add_field(
            name="💰 New Balance",
            value=f"{winner_name.mention if hasattr(winner_name, 'mention') else winner_name}: {format_money(raffle['currency'], new_balance)}",
            inline=False
        )

        # Add some hype messages
        import random
        hype_messages = [
            "STAY TUNED FOR THE NEXT BIG GIVEAWAY!",
            "LIKE AND SUBSCRIBE FOR MORE CHANCES TO WIN!",
            "YOU COULD BE NEXT - KEEP PARTICIPATING!",
            "HIT THAT NOTIFICATION BELL SO YOU NEVER MISS A GIVEAWAY!",
            "THANKS FOR PLAYING - MORE PRIZES COMING SOON!"
        ]
        embed.add_field(
            name="📣 Host Message",
            value=random.choice(hype_messages),
            inline=False
        )

        await interaction.edit_original_response(content="", embed=embed)
    else:
        # No winner (no participants or error)
        await interaction.edit_original_response(content="😔 No participants in the raffle - nobody wins this time.")


@raffle_group.command(name="list", description="List active raffles in this channel")
async def raffle_list(interaction: discord.Interaction) -> None:
    """List active raffles"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    raffles = db.get_active_raffles(interaction.channel_id, guild_id)

    if not raffles:
        await interaction.response.send_message("No active raffles in this channel.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🎟️ Active Raffles",
        color=0x00FF00  # Green
    )

    for raffle in raffles[:10]:  # Limit to 10 to avoid too long messages
        status = "🟢 Active" if raffle["active"] else "🔴 Ended"
        ends_at_str = ""
        if raffle["ends_at"]:
            ends_at = datetime.fromisoformat(raffle["ends_at"])
            ends_at_str = f"\nEnds: {ends_at.strftime('%m/%d %H:%M')}"

        participant_count = len(db.get_raffle_participants(raffle["id"]))
        limit_str = f"/{raffle['max_participants']}" if raffle['max_participants'] else "/∞"

        embed.add_field(
            name=f"Raffle #{raffle['id']} - {raffle['prize_description'] or 'No description'}",
            value=f"💰 Prize: {format_money(raffle['currency'], db._raffle_prize(raffle))}\n"
                  f"👥 {participant_count}{limit_str} joined\n"
                  f"{status}{ends_at_str}",
            inline=True
        )

    await interaction.response.send_message(embed=embed, ephemeral=False)


@raffle_group.command(name="info", description="Get detailed info about a raffle")
@app_commands.describe(
    raffle_id="ID of the raffle to get info about"
)
async def raffle_info(
    interaction: discord.Interaction,
    raffle_id: int
) -> None:
    """Get detailed raffle info"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    raffle = db.get_raffle(raffle_id)
    if not raffle:
        await interaction.response.send_message(f"Raffle #{raffle_id} not found.", ephemeral=True)
        return

    # Check if raffle is in the same guild/channel
    if raffle["guild_id"] != guild_id or raffle["channel_id"] != interaction.channel_id:
        await interaction.response.send_message("That raffle is not in this channel.", ephemeral=True)
        return

    participants = db.get_raffle_participants(raffle["id"])
    participant_count = len(participants)

    embed = discord.Embed(
        title=f"🎟️ Raffle #{raffle_id} Info",
        description=raffle["prize_description"] or "No description provided",        color=0x0099FF  # Blue
    )

    # Basic info
    embed.add_field(
        name="📋 Basic Info",
        value=f"• Creator: {raffle['creator_name']}\n"
              f"• Created: {datetime.fromisoformat(raffle['created_at']).strftime('%m/%d %H:%M')}\n"
              f"• Status: {'🟢 Active' if raffle['active'] else '🔴 Ended'}",
        inline=True
    )

    # Entry info
    embed.add_field(
        name="💰 Prize Info",
        value=f"• Prize: {format_money(raffle['currency'], db._raffle_prize(raffle))}\n"
              f"• Currency: {raffle['currency']}\n"
              f"• {'Unlimited' if not raffle['max_participants'] else raffle['max_participants']} max participants",
        inline=True
    )

    # Participation info
    embed.add_field(
        name="👥 Participants",
        value=f"• Current: {participant_count}\n"
              f"• {'Full' if raffle['max_participants'] and participant_count >= raffle['max_participants'] else 'Open'}",
        inline=True
    )

    # Timing info
    if raffle["ends_at"]:
        ends_at = datetime.fromisoformat(raffle["ends_at"])
        time_left = ends_at - datetime.now()
        if time_left.total_seconds() > 0:
            hours, remainder = divmod(int(time_left.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            time_str = f"{hours}h {minutes}m {seconds}s"
        else:
            time_str = "Ended"
        embed.add_field(
            name="⏰ Timing",
            value=f"• Ends: {ends_at.strftime('%m/%d %H:%M')}\n"
                  f"• Time left: {time_str}",
            inline=True
        )
    else:
        embed.add_field(
            name="⏰ Timing",
            value="• Ends: When ended manually\n"
                  f"• Duration: Indefinite",
            inline=True
        )

    # Recent participants (last 5)
    if participants:
        recent = participants[-5:] if len(participants) > 5 else participants
        recent_names = [p["user_name"] for p in recent]
        recent_text = "\n".join([f"• {name}" for name in recent_names])
        if len(participants) > 5:
            recent_text += f"\n• ...and {len(participants) - 5} more"
        embed.add_field(
            name="👥 Recent Participants",
            value=recent_text or "None",
            inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=False)


@raffle_group.command(name="balance", description="Check your or someone's virtual currency balance")
@app_commands.describe(
    user="User to check balance for (leave blank for yourself)"
)
async def raffle_balance(
    interaction: discord.Interaction,
    user: discord.Member | None = None
) -> None:
    """Check balance"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    target_user = user or interaction.user
    user_id = target_user.id
    user_name = target_user.display_name

    balance, currency = db.get_user_balance(user_id, guild_id)

    embed = discord.Embed(
        title=f"💰 {user_name}'s Balance",
        description=f"{format_money(currency, balance)}",
        color=0x00FF00 if balance >= 0 else 0xFF0000  # Green if positive, red if negative
    )

    if user_id == interaction.user.id:
        embed.set_footer(text="Winning raffles will add to your balance!")
    else:
        embed.set_footer(text=f"Balance for {user_name}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@raffle_group.command(name="leaderboard", description="Top 5 richest users and your rank")
async def raffle_leaderboard(interaction: discord.Interaction) -> None:
    """Show the top 5 balances and the user's rank."""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # Get top 5
    top = db.get_top_balances(guild_id, limit=5)

    # Get requesting user's rank
    user_id = interaction.user.id
    user_rank = db.get_user_balance_rank(guild_id, user_id)
    user_balance, user_currency = db.get_user_balance(user_id, guild_id)

    embed = discord.Embed(
        title="🏆 Richest Members",
        color=0xFFD700  # Gold
    )

    if not top:
        embed.description = "No balances yet — be the first to win a raffle!"
    else:
        trophy = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        lines = []
        for i, row in enumerate(top):
            name = f"<@{row['user_id']}>"
            bal = format_money(row["currency"], row["balance"])
            lines.append(f"{trophy[i]} {name}: {bal}")
        embed.add_field(name="Top 5", value="\n".join(lines), inline=False)

    # Show user's own position
    if user_rank is not None:
        embed.add_field(
            name="📍 Your Position",
            value=f"You're **#{user_rank}** with {format_money(user_currency, user_balance)}",
            inline=False,
        )
    else:
        embed.add_field(
            name="📍 Your Position",
            value=f"You have {format_money(user_currency, user_balance)} — win a raffle to climb the ranks!",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=False)


# Role-based auto-join configuration
@raffle_group.command(name="autorole", description="Configure role-based auto-join for raffles (admin only)")
@app_commands.describe(
    role="Role to automatically add to raffles (leave blank to disable)",
    enabled="Whether to enable role-based auto-join"
)
@app_commands.choices(
    enabled=[
        app_commands.Choice(name="Enable", value="true"),
        app_commands.Choice(name="Disable", value="false")
    ]
)
@app_commands.checks.has_permissions(manage_roles=True)
async def raffle_autorole(
    interaction: discord.Interaction,
    enabled: app_commands.Choice[str],
    role: discord.Role | None = None
) -> None:
    """Configure role-based auto-join"""
    guild_id = interaction.guild.id if interaction.guild else None
    if not guild_id:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    if not await ensure_owner(interaction):
        # Only owner can configure this for now - later we could allow admins with permissions
        await interaction.response.send_message("Only the bot owner can configure auto-join roles.", ephemeral=True)
        return

    role_id = role.id if role else None
    role_name = role.name if role else "None"

    # Set the guild's auto-join role and enable/disable status
    db.set_guild_auto_join_role(guild_id, role_id)
    db.set_guild_auto_join_enabled(guild_id, enabled.value == "true")

    status = "enabled" if enabled.value == "true" else "disabled"
    if role_id:
        await interaction.response.send_message(
            f"✅ Role-based auto-join {status}! "
            f"Members with the **{role_name}** role will be automatically added to new raffles.",
            ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"✅ Role-based auto-join {status}. No automatic role-based joining will occur.",
            ephemeral=True
        )


# Error handler for autorole command
@raffle_autorole.error
async def raffle_autorole_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need the 'Manage Roles' permission to use this command.", ephemeral=True)
    else:
        await interaction.response.send_message("An error occurred while processing this command.", ephemeral=True)


# Add the raffle group to the bot's command tree
# This will be done in setup_hook along with the other groups


def _mute_list_text() -> str:
    """The muted channels as readable mentions, for the reply messages."""
    muted = muted_chat_channels()
    if not muted:
        return "none"
    return ", ".join(f"<#{channel_id}>" for channel_id in sorted(muted))


@chatbot_group.command(name="show", description="Current chat reply settings")
async def chatbot_show(interaction: discord.Interaction) -> None:
    if not await ensure_owner(interaction):
        return
    cooldown = chat_cooldown_seconds()
    own_keys = ai_parser.chat_keys() if os.getenv("CHATBOT_API_KEY", "").strip() else []
    key_text = (
        f"its own ({len(own_keys)} key{'s' if len(own_keys) != 1 else ''})"
        if own_keys
        else f"shared with detection ({len(ai_parser.detection_keys())} key(s))"
    )
    backup = (
        f" (backups: {', '.join(f'`{m}`' for m in CHATBOT_FALLBACK_MODELS if m)}"
        if CHATBOT_FALLBACK_MODELS and any(CHATBOT_FALLBACK_MODELS)
        else ""
        + ")"
    )
    await interaction.response.send_message(
        "💬 **Chat replies**\n"
        f"- State: **{'on' if chatbot_is_on() else 'off'}**\n"
        f"- Model: `{CHATBOT_MODEL}`{backup}\n"
        f"- API key: {key_text}\n"
        f"- Daily cap: {CHATBOT_MAX_CALLS_PER_DAY} replies\n"
        f"- Cooldown: {f'{cooldown}s per person' if cooldown else 'off'}\n"
        f"- Muted channels: {_mute_list_text()}",
        ephemeral=True,
    )


@chatbot_group.command(name="toggle", description="Turn chat replies on or off")
@app_commands.choices(
    state=[
        app_commands.Choice(name="On - reply when mentioned (default)", value="on"),
        app_commands.Choice(name="Off - stay quiet everywhere", value="off"),
    ]
)
async def chatbot_toggle(
    interaction: discord.Interaction, state: app_commands.Choice[str]
) -> None:
    if not await ensure_owner(interaction):
        return
    db.set_setting("chatbot_enabled", state.value)
    logger.info("Chat replies switched: %s", state.value)
    await interaction.response.send_message(
        f"🔧 Chat replies are now: **{state.name}**", ephemeral=True
    )


@chatbot_group.command(
    name="cooldown", description="Seconds each person must wait between pings"
)
@app_commands.describe(seconds="0 turns the cooldown off (default)")
async def chatbot_cooldown(
    interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 3600]
) -> None:
    if not await ensure_owner(interaction):
        return
    db.set_setting("chatbot_cooldown", str(seconds))
    logger.info("Chat cooldown set to %ss", seconds)
    await interaction.response.send_message(
        f"🔧 Chat cooldown is now: **{seconds}s per person**" if seconds
        else "🔧 Chat cooldown is now **off** - anyone can ping as often as they like.",
        ephemeral=True,
    )


@chatbot_group.command(name="mute", description="Stop chat replies in a channel")
@app_commands.describe(channel="Which channel (defaults to this one)")
async def chatbot_mute(
    interaction: discord.Interaction, channel: discord.TextChannel | None = None
) -> None:
    if not await ensure_owner(interaction):
        return
    target = channel or interaction.channel
    muted = muted_chat_channels()
    if target.id in muted:
        await interaction.response.send_message(
            f"{target.mention} is already muted.", ephemeral=True
        )
        return
    muted.add(target.id)
    set_muted_chat_channels(muted)
    logger.info("Chat replies muted in channel %s", target.id)
    await interaction.response.send_message(
        f"🔇 Muted chat replies in {target.mention}. Detection and reminders "
        "there are unaffected.",
        ephemeral=True,
    )


@chatbot_group.command(name="unmute", description="Allow chat replies in a channel again")
@app_commands.describe(channel="Which channel (defaults to this one)")
async def chatbot_unmute(
    interaction: discord.Interaction, channel: discord.TextChannel | None = None
) -> None:
    if not await ensure_owner(interaction):
        return
    target = channel or interaction.channel
    muted = muted_chat_channels()
    if target.id not in muted:
        await interaction.response.send_message(
            f"{target.mention} wasn't muted.", ephemeral=True
        )
        return
    muted.discard(target.id)
    set_muted_chat_channels(muted)
    logger.info("Chat replies unmuted in channel %s", target.id)
    await interaction.response.send_message(
        f"🔊 Chat replies are back on in {target.mention}.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# /help - built from the live command tree, so it can never fall out of date
# ---------------------------------------------------------------------------

# Which command groups get which emoji in the help embed. Anything missing
# just falls back to a bullet - a new group still shows up, only unstyled.
_HELP_ICONS = {
    "debt": "💰",
    "remind": "⏰",
    "calendar": "📅",
    "settings": "🔧",
    "help": "❓",
}


def _command_lines(
    command: app_commands.Command | app_commands.Group,
    prefix: str = "",
    public_parent: bool = False,
) -> list[tuple[str, bool]]:
    """Flatten a command or group into ("/name - description", is_public) pairs.

    Descriptions come straight from the command objects, so /help always
    matches what is actually registered.
    """
    is_public = public_parent or bool(command.extras.get("public"))
    if isinstance(command, app_commands.Group):
        lines: list[tuple[str, bool]] = []
        for child in sorted(command.commands, key=lambda c: c.name):
            lines.extend(_command_lines(child, f"{prefix}{command.name} ", is_public))
        return lines
    return [(f"`/{prefix}{command.name}` - {command.description}", is_public)]


@bot.tree.command(
    name="help",
    description="Show every command and what it does",
    extras={"public": True},  # anyone can run it, so it lists itself for everyone
)
async def help_command(interaction: discord.Interaction) -> None:
    is_owner = interaction.user.id == OWNER_ID
    embed = discord.Embed(
        title="Reminder Bot - commands",
        description=(
            "Everything I can do. Most commands are owner-only; the ones you "
            "can see here are the ones you can use."
        ),
        color=discord.Color.blurple(),
    )

    for command in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        # Owner sees everything; everyone else only the commands marked public.
        lines = [text for text, public in _command_lines(command) if public or is_owner]
        if not lines:
            continue
        icon = _HELP_ICONS.get(command.name, "•")
        embed.add_field(
            name=f"{icon} {command.name.capitalize()}",
            value="\n".join(lines),
            inline=False,
        )

    embed.add_field(
        name="💬 Just talk to me",
        value=(
            "Mention me (or reply to one of my messages) and I'll answer - "
            "ask me anything. I ignore @everyone.\n"
            "Ask me for a reminder and I'll set it right there: "
            "*\"@me remind me to call mom bukas 5pm\"*."
        ),
        inline=False,
    )
    if is_owner:
        embed.set_footer(
            text="I also watch chat for debts and reminders, and DM you "
            "Confirm/Ignore before saving anything."
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _require_config()
    # Our own loggers honor LOG_LEVEL; discord.py's internals stay at INFO
    # because their DEBUG output is overwhelming and rarely useful here.
    own_level = getattr(logging, LOG_LEVEL, logging.INFO)
    for name in ("reminderbot", "reminderbot.ai", "reminderbot.calendar"):
        logging.getLogger(name).setLevel(own_level)
    bot.run(DISCORD_TOKEN, log_level=logging.INFO, root_logger=True)


if __name__ == "__main__":
    main()
