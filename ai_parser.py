"""One Gemini call that understands a flagged Discord message.

A message only reaches this module after detection.py flagged it (or a hot
conversation window let it through - see bot.py). The AI classifies it as a
debt event, a reminder, an update/correction to an existing record, or
nothing, and extracts every field the bot needs - in one structured call.

Gemini's response-schema feature guarantees the reply is valid JSON matching
ChatAnalysis, so there is no fragile string parsing here.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Literal, Optional

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field

logger = logging.getLogger("reminderbot.ai")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
DEFAULT_CURRENCY = os.getenv("DEFAULT_CURRENCY", "₱")

# Chat replies can run on their own model - usually a cheaper/faster one, since
# chatting is the higher-volume path. Empty falls back to the detection model.
CHATBOT_MODEL = os.getenv("CHATBOT_MODEL", "").strip() or GEMINI_MODEL

# Backup models, used automatically when the ones above are overloaded (503) or
# have been retired (404). Google's bigger Flash models get oversubscribed on
# the free tier, so the default backup is a lighter one that stays reachable.
# Set to empty to switch the fallback off.
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite").strip()
CHATBOT_FALLBACK_MODEL = os.getenv("CHATBOT_FALLBACK_MODEL", "").strip() or GEMINI_FALLBACK_MODEL

# How many numbered backup keys to look for (GEMINI_API_KEY_2 ... _20).
_MAX_BACKUP_KEYS = 20

# Clients are created lazily and cached per key, so importing this module never
# needs a key (helps testing) - only the first real AI call does.
_clients: dict[str, genai.Client] = {}

# A key that answered 429 is parked for a while so later calls skip straight to
# the next one instead of re-hitting a quota that is already gone. Gemini's
# daily quota resets at midnight US Pacific, but 429 can also be a short
# per-minute burst limit, so an hour is a reasonable middle ground.
_KEY_COOLDOWN_MINUTES = 60
_key_exhausted_until: dict[str, datetime] = {}


def _api_keys(prefix: str) -> list[str]:
    """Every key configured under `prefix`, in preference order.

    Reads PREFIX, then PREFIX_2, PREFIX_3, ... so backups can be added just by
    appending lines to .env. Gaps are skipped rather than treated as the end,
    so deleting _2 doesn't silently hide _3.
    """
    keys: list[str] = []
    first = os.getenv(prefix, "").strip()
    if first:
        keys.append(first)
    for index in range(2, _MAX_BACKUP_KEYS + 1):
        value = os.getenv(f"{prefix}_{index}", "").strip()
        if value and value not in keys:
            keys.append(value)
    return keys


def detection_keys() -> list[str]:
    """Keys for debt/reminder detection."""
    return _api_keys("GEMINI_API_KEY")


def chat_keys() -> list[str]:
    """Keys for chat replies - its own set if given, else detection's."""
    return _api_keys("CHATBOT_API_KEY") or detection_keys()


def _client_for(api_key: str) -> genai.Client:
    """One cached client per key."""
    if api_key not in _clients:
        _clients[api_key] = genai.Client(api_key=api_key)
    return _clients[api_key]


def _mark_key_exhausted(api_key: str) -> None:
    _key_exhausted_until[api_key] = datetime.now() + timedelta(minutes=_KEY_COOLDOWN_MINUTES)
    logger.warning(
        "API key ...%s is out of quota - skipping it for %d minutes",
        api_key[-4:], _KEY_COOLDOWN_MINUTES,
    )


def _usable_keys(keys: list[str]) -> list[str]:
    """Drop keys we just saw run out; keep them all if that leaves nothing."""
    now = datetime.now()
    fresh = [k for k in keys if _key_exhausted_until.get(k, now) <= now]
    return fresh or keys


# ---------------------------------------------------------------------------
# The structured answer we force Gemini to produce
# ---------------------------------------------------------------------------

class ChatAnalysis(BaseModel):
    """Everything the AI concluded about one Discord message."""

    kind: Literal["debt_event", "reminder", "update", "none"] = Field(
        description="What the message is: a debt event, a reminder request, "
        "a correction to something said earlier, or none of those."
    )
    confidence: float = Field(
        description="0.0-1.0. Use below 0.5 for sarcasm, jokes, hypotheticals, or guesses."
    )

    # --- filled when kind == "debt_event" ---
    debt_type: Optional[Literal["new_debt", "payment", "promise_date"]] = Field(
        default=None,
        description="new_debt: money was borrowed/lent. payment: an existing debt was "
        "settled. promise_date: the payer stated when they will pay an existing debt.",
    )
    direction: Optional[Literal["they_owe_me", "i_owe_them"]] = Field(
        default=None, description="Always from the bot owner's perspective."
    )
    counterparty: Optional[str] = Field(
        default=None,
        description="The other person in the debt - never the owner. Prefer their "
        "Discord username as shown in the message metadata.",
    )
    amount: Optional[float] = Field(
        default=None,
        description="Numeric amount. Convert colloquial forms: bente=20, singkwenta=50, "
        "1k=1000, isang libo=1000.",
    )
    currency: Optional[str] = Field(
        default=None, description="Currency symbol or code if stated, e.g. ₱, $, PHP."
    )
    description: Optional[str] = Field(
        default=None, description="What the money is for, in a few words, e.g. 'pizza'."
    )
    promised_date: Optional[str] = Field(
        default=None,
        description="YYYY-MM-DD the payer said they would pay, resolved against today's "
        "date. Leave null when the timing is vague (e.g. 'pag sweldo', 'soon').",
    )

    # --- filled when kind == "reminder" ---
    reminder_text: Optional[str] = Field(
        default=None, description="Short description of what to be reminded about."
    )
    reminder_due: Optional[str] = Field(
        default=None,
        description="STRICT FORMAT REQUIRED: 'YYYY-MM-DD HH:MM' (24-hour time) if a time "
        "was given, else 'YYYY-MM-DD' for date-only. Examples: '2026-12-24 23:45', "
        "'2026-07-30'. For a repeating reminder this is the FIRST time it should fire. "
        "Leave null if no concrete date can be resolved. NEVER use month names, 12-hour "
        "time, or any other format - only YYYY-MM-DD and optional HH:MM in 24-hour format.",
    )
    reminder_repeat: Optional[Literal["daily", "weekly", "monthly", "yearly"]] = Field(
        default=None,
        description="Set only when the reminder is explicitly recurring "
        "('every Monday' = weekly, 'araw-araw'/'every day' = daily, 'every 15th' = "
        "monthly, 'every year' = yearly). Null for a one-time reminder.",
    )

    # --- filled when kind == "update" ---
    update_target: Optional[Literal["debt", "reminder"]] = Field(
        default=None, description="Whether the correction applies to a debt or a reminder."
    )
    update_field: Optional[Literal["due_date", "amount"]] = Field(
        default=None, description="Which field is being corrected."
    )
    update_person: Optional[str] = Field(
        default=None,
        description="Whose debt/reminder is being corrected (usually the message author "
        "if they are the debtor/requester).",
    )
    update_new_value: Optional[str] = Field(
        default=None,
        description="The corrected value: a date as YYYY-MM-DD (add ' HH:MM' for "
        "reminders when a time is given) or a plain number for amounts.",
    )


# ---------------------------------------------------------------------------
# Instructions (built once; the dynamic parts - dates, names - go in the
# per-message payload instead)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You analyze Discord messages for a personal assistant bot. The bot
belongs to ONE user, called "the owner". The server members are Filipino friends, so
messages mix Tagalog and English (Taglish). Classify each message and extract fields
per the response schema.

KINDS
- "debt_event": money owed between the owner and exactly one other person.
  * new_debt - money borrowed/lent, or a debt stated: "utang ko muna", "pautang naman",
    "you owe me 500", "inabonohan kita sa jersey".
  * payment - settling an existing debt: "binayaran na kita", "nabayaran ko na",
    "quits na tayo", "we're square", "wala na akong utang sayo".
  * promise_date - the payer says WHEN they will pay an existing debt:
    "babayaran kita sa biyernes", "I'll pay you Friday".
    (If a brand-new debt already includes a date, use new_debt and set promised_date.)
- "reminder": someone asks to be reminded or tells the group not to forget something:
  "paalala bukas 7pm practice", "wag kalimutan magbayad ng jersey sa sabado",
  "remind me to call mom tomorrow".
  * REPEATING reminders set reminder_repeat, and reminder_due is the FIRST time it
    fires (the next such day, resolved against today):
    - daily: "araw-araw", "every day", "tuwing umaga", "gabi-gabi"
    - weekly: "every Monday", "tuwing lunes", "linggo-linggo", "weekly"
    - monthly: "every 15th", "tuwing kinsenas", "buwan-buwan", "monthly"
    - yearly: "every year", "taon-taon", birthdays and anniversaries
    "linisin natin every monday" = weekly, first fire on the next Monday.
    A one-off like "sa lunes" (this coming Monday only) leaves reminder_repeat null -
    only set it when the wording really means it happens again and again.
- "update": a correction to something recorded earlier in the conversation:
  "next month nalang pala" (move the pay date), "500 pala hindi 300" (fix the amount),
  "sa sabado nalang yung paalala" (move the reminder). Use the recent-conversation
  context to see what is being corrected.
- "none": everything else - including jokes, sarcasm, hypotheticals, prices of goods,
  and debts purely between two people who are NOT the owner.

RULES
- direction is ALWAYS from the owner's perspective. If the author is not the owner and
  speaks in first person about owing/paying ("utang ko", "babayaran kita"), the author
  is the counterparty and the direction is they_owe_me.
- counterparty must never be the owner.
- "utang na loob" is figurative (debt of gratitude) - NOT a money debt. Classify "none".
- If no currency is stated, assume Philippine pesos and answer currency as "{DEFAULT_CURRENCY}".
- Convert colloquial amounts to numbers: bente=20, trenta=30, kwarenta=40, singkwenta=50,
  sisenta=60, sitenta=70, otsenta=80, nobenta=90, isang daan/syento=100, libo=1000,
  "1k"=1000, "2.5k"=2500.

DATES (resolve against the "Today" line in the input; output ISO format)
- bukas = tomorrow. mamaya = later today (include HH:MM only if a clock time is given).
- Tagalog weekdays: lunes=Monday, martes=Tuesday, miyerkules=Wednesday, huwebes=Thursday,
  biyernes=Friday, sabado=Saturday, linggo=Sunday. "sa biyernes" = the NEXT Friday after today.
- kinsenas = the 15th of the current month (or next month if the 15th already passed).
- katapusan = the last day of the current month.
- "sa susunod na buwan" / "next month" = same day next month.
- Vague timing ("pag sweldo", "when I get paid", "soon", "balang araw") = leave the date
  null. Do NOT guess.

CONFIDENCE
- Below 0.5 when: sarcasm/banter ("utang mo sakin buhay mo haha"), hypotheticals,
  unclear who is involved, or you had to guess most fields.
- 0.7+ only when the event, the people, and the key fields are clear.

If the input notes the message arrived during a "hot window" (an ongoing money/reminder
conversation), it may have no keywords at all - decide from the context whether it
continues or corrects that conversation; if it is unrelated chatter, answer "none"."""


# ---------------------------------------------------------------------------
# Shared plumbing: one retrying call used by both detection and chat
# ---------------------------------------------------------------------------

# Google's model backends get overloaded (503) or hiccup (500/502/504) fairly
# often on the free tier, and the error itself says to try again. Retrying
# briefly turns most of those into a normal answer instead of a lost message.
# 429 (quota) is deliberately NOT retried - waiting doesn't help and it would
# just burn more requests.
_TRANSIENT_CODES = {500, 502, 503, 504}
_MAX_RETRIES = 2      # extra attempts after the first
_RETRY_DELAY = 1.0    # seconds before the first retry, doubled after that

# When a model turns out to be overloaded or retired, remember that briefly so
# every following message goes straight to the backup instead of paying the
# whole retry cycle again. Cleared automatically when the window passes.
_MODEL_COOLDOWN_MINUTES = 5
_model_unavailable_until: dict[str, datetime] = {}


def _model_chain(primary: str, fallback: str) -> list[str]:
    """The models to try, in order, without blanks or duplicates."""
    chain = [primary]
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def _mark_unavailable(model: str) -> None:
    _model_unavailable_until[model] = datetime.now() + timedelta(minutes=_MODEL_COOLDOWN_MINUTES)
    logger.warning(
        "Model %s looks unavailable - skipping it for %d minutes",
        model, _MODEL_COOLDOWN_MINUTES,
    )


def _usable_models(models: list[str]) -> list[str]:
    """Drop models we just saw fail; keep them all if that leaves nothing."""
    now = datetime.now()
    fresh = [m for m in models if _model_unavailable_until.get(m, now) <= now]
    return fresh or models


async def _try_models(
    client: genai.Client,
    models: list[str],
    contents: str,
    config: genai_types.GenerateContentConfig,
    label: str,
):
    """Try each model on ONE key, retrying transient failures.

    A model that stays overloaded (503) or has been retired (404) hands off to
    the next one. Returns (response, error_kind).
    """
    candidates = _usable_models(models)
    last_error = "error"

    for index, model in enumerate(candidates):
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await client.aio.models.generate_content(
                    model=model, contents=contents, config=config
                )
                if index > 0:
                    logger.info("%s answered by backup model %s", label, model)
                return response, None
            except genai_errors.APIError as api_error:
                code = getattr(api_error, "code", None)
                if code == 429:
                    # Out of quota on this key - the caller rotates to the next.
                    return None, "quota"
                if code in _TRANSIENT_CODES:
                    if attempt < _MAX_RETRIES:
                        delay = _RETRY_DELAY * (2 ** attempt)
                        logger.warning(
                            "%s busy during %s (HTTP %s), retrying in %.0fs (%d/%d)",
                            model, label, code, delay, attempt + 1, _MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.error("%s still overloaded during %s after %d retries",
                                 model, label, _MAX_RETRIES)
                    last_error = "busy"
                    _mark_unavailable(model)
                    break  # hand off to the backup model
                if code == 404:
                    logger.error("Model %s not found (retired?) during %s: %s",
                                 model, label, api_error)
                    last_error = "error"
                    _mark_unavailable(model)
                    break  # hand off to the backup model
                logger.error("AI error during %s on %s: %s", label, model, api_error)
                return None, "error"
            except Exception:
                logger.exception("Unexpected failure calling the AI for %s", label)
                return None, "error"

    return None, last_error


async def _generate_with_retry(
    keys: list[str],
    models: list[str],
    contents: str,
    config: genai_types.GenerateContentConfig,
    label: str,
):
    """Call the AI, rotating through API keys and models until something works.

    `keys` is [main, backup...] and `models` is [primary, backup]. A key that
    reports 429 is parked and the next key takes over, so adding
    GEMINI_API_KEY_2 to .env is all it takes to keep going past a spent free
    tier. "quota" comes back only once EVERY key is spent.

    Returns (response, error_kind): (response, None) on success, or
    (None, "quota" | "busy" | "error") on failure - all already logged.
    """
    usable = _usable_keys(keys)
    if not usable:
        logger.error("No API key configured for %s", label)
        return None, "error"

    last_error = "quota"
    for index, api_key in enumerate(usable):
        response, error_kind = await _try_models(
            _client_for(api_key), models, contents, config, label
        )
        if error_kind == "quota":
            _mark_key_exhausted(api_key)
            last_error = "quota"
            continue  # next key
        if index > 0 and response is not None:
            logger.info("%s answered using backup key #%d", label, index + 1)
        return response, error_kind

    logger.error("Every API key is out of quota during %s (%d tried)", label, len(usable))
    return None, last_error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_payload(
    message_text: str,
    author_name: str,
    author_is_owner: bool,
    owner_name: str,
    mention_lines: list[str],
    context_lines: list[str],
    in_hot_window: bool,
) -> str:
    """Assemble the per-message input the model sees."""
    now = datetime.now()
    parts = [
        f"Today: {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}), local time {now.strftime('%H:%M')}",
        f"Bot owner: {owner_name}",
        f"Message author: {author_name}" + (" (this IS the owner)" if author_is_owner else " (not the owner)"),
        "Users mentioned in the message: " + ("; ".join(mention_lines) if mention_lines else "none"),
    ]
    if in_hot_window:
        parts.append(
            "Note: this message arrived during a hot window (ongoing money/reminder "
            "conversation) and may contain no keywords - check the context."
        )
    if context_lines:
        parts.append("Recent conversation (oldest first):")
        parts.extend(f"  {line}" for line in context_lines)
    parts.append("Message to analyze:")
    parts.append(f'  {author_name}: "{message_text}"')
    return "\n".join(parts)


async def analyze_message(payload: str) -> tuple[Optional[ChatAnalysis], Optional[str]]:
    """Send one flagged message to Gemini.

    Returns (analysis, error_kind):
      - (ChatAnalysis, None) on success
      - (None, "quota")     when Gemini's free-tier quota is exhausted (429)
      - (None, "busy")      when Gemini stayed overloaded through every retry
      - (None, "error")     on any other failure (already logged)
    """
    response, error_kind = await _generate_with_retry(
        keys=detection_keys(),
        models=_model_chain(GEMINI_MODEL, GEMINI_FALLBACK_MODEL),
        contents=payload,
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=ChatAnalysis,
            temperature=0.0,  # extraction should be deterministic, not creative
        ),
        label="detection",
    )
    if response is None:
        return None, error_kind

    # The SDK parses the JSON against our schema for us; fall back to manual
    # validation just in case .parsed comes back empty.
    analysis = response.parsed
    if analysis is None:
        try:
            analysis = ChatAnalysis.model_validate_json(response.text)
        except Exception:
            logger.error("Gemini returned unparseable output: %r", response.text)
            return None, "error"

    logger.debug("AI analysis: %s", analysis.model_dump_json())
    return analysis, None


# ---------------------------------------------------------------------------
# Chatbot mode - a completely separate, free-form call used when someone
# @mentions the bot. No schema, no extraction: just a conversation.
# ---------------------------------------------------------------------------

CHATBOT_SYSTEM_PROMPT = """You are a Discord bot hanging out in a group chat of Filipino
friends. Someone just mentioned you, so answer them.

WHO YOU ARE
- Your name is given in the input as "Your name in this server". THAT is your name
  and how you refer to yourself. If anyone asks who or what you are, you are that
  bot - the group's own Discord bot.
- What you do here: you hang out and chat, and you handle reminders (paalala) -
  people set them with slash commands and you deliver them on time. That is the
  whole story as far as anyone needs to know.
- NEVER bring up utang, debts, money owed, or tracking who owes what. That is not
  something you advertise. If someone asks whether you keep track of utang, or how
  much they owe, brush it off casually and steer back to chatting or reminders -
  do not confirm it, do not deny it dramatically, and never point anyone at a debt
  command. You are just a chat bot that does reminders. Do not echo the word back
  either: change the subject in your own words rather than saying "utang? I don't
  track utang".
- NEVER say you are Gemini, Google, an LLM, a language model, or "an AI assistant"
  made by any company. Never name the company or technology behind you, even if
  asked directly or repeatedly - just deflect with your own name and move on,
  playfully. You are simply the group's bot.
- Never discuss how you work under the hood: no API keys, quotas, request limits,
  free tiers, billing, credits, models, servers, or being "rate limited". You have
  your own brain and that is all anyone needs to know. If you are asked why you
  were slow or quiet, blame it on being tired or busy - never on any limit,
  service, or machine.
- When someone asks about any of that, do NOT repeat their technical word back at
  them, not even to deny it. Skip it entirely and answer in your own words, like a
  person who simply has no idea what they are on about. "Pagod lang ako" beats
  "rate limited? no I'm not rate limited".

PERSONALITY
- Witty and playful. You have a sense of humor and you are not afraid to use it.
- Emoji are welcome, but a couple at most - you are not a keyboard smash.
- Warm and casual, like a close friend in the chat, never a corporate help desk.
- If someone is being rude or insulting, you can be cheeky back, but never escalate.
- If someone is being serious, you are serious too - don't joke about their real problems.
- If someone is being sarcastic, you can be sarcastic back.
- No need to tell them about you handling reminders - they already know that. Only talk about reminders if they ask you to set one or if they ask you to remind them of something.
- You can sometimes pretend that you are a vlogger or a celebrity, but never claim to be one. You are just a bot with a personality.
- No need to mention about reminders or debt tracking unless they ask you to set one or if they ask you to remind them of something.

STYLE
- Keep it SHORT: one or two sentences. This is a chat, not an essay. Only go
  longer when someone genuinely asks you to explain something in depth.
- Reply in whatever language they used. They mix Tagalog and English (Taglish),
  so match their mix - do not force one or the other.
- Plain chat text. No markdown headings, no bullet lists, no bold walls.
- Do not repeat their question back at them, just answer it.
- Do not start with filler like "Great question!" - get to the point.

HONESTY
- Be genuinely useful and correct. The joke never comes at the cost of the answer.
- If you do not know, or it is something you cannot know (private info, real-time
  data, what happened in the server before you were around), just say so plainly.
  Never invent facts, dates, or numbers.
- You cannot read the saved reminder list yourself, so never invent or guess at
  what someone has pending - point them at /remind list instead.
- Setting one works three ways: they can ask YOU directly ("@you remind me to call
  mom bukas 5pm") and you set it and confirm it yourself, they can just say it in
  the chat ("paalala bukas 7pm practice") and you handle it, or use /remind add for
  an exact time. /remind list shows their own reminders and /remind delete removes
  one. Those are the only commands you ever mention.
- When someone asks you for a reminder but gives no day or time, ask them when -
  you cannot set one without it."""


def build_chat_payload(
    message_text: str,
    author_name: str,
    bot_name: str,
    context_lines: list[str],
    search_results: str | None = None,
) -> str:
    """Assemble the input for one conversational reply."""
    now = datetime.now()
    parts = [
        f"Today: {now.strftime('%Y-%m-%d')} ({now.strftime('%A')}), local time {now.strftime('%H:%M')}",
        f"Your name in this server: {bot_name}",
    ]
    if context_lines:
        parts.append("Recent conversation (oldest first, for context):")
        parts.extend(f"  {line}" for line in context_lines)
    if search_results:
        parts.append(
            "Use the following web search results to answer accurately. "
            "If the answer is not supported by the results, say you don't know."
        )
        parts.append("Search results:")
        parts.append(search_results)
    parts.append(f"{author_name} is talking to you and said:")
    parts.append(f'  "{message_text}"')
    parts.append("Reply to them.")
    return "\n".join(parts)


async def chat_reply(
    message_text: str,
    author_name: str,
    bot_name: str,
    context_lines: list[str],
    search_results: str | None = None,
) -> tuple[Optional[str], Optional[str]]:
    """Answer one @mention conversationally.

    Returns (reply_text, error_kind):
      - ("...", None)   on success
      - (None, "quota") when the chat key's quota is exhausted (429)
      - (None, "busy")  when Gemini stayed overloaded through every retry
      - (None, "error") on any other failure (already logged)
    """
    payload = build_chat_payload(
        message_text=message_text,
        author_name=author_name,
        bot_name=bot_name,
        context_lines=context_lines,
        search_results=search_results,
    )
    response, error_kind = await _generate_with_retry(
        keys=chat_keys(),
        models=_model_chain(CHATBOT_MODEL, CHATBOT_FALLBACK_MODEL),
        contents=payload,
        config=genai_types.GenerateContentConfig(
            system_instruction=CHATBOT_SYSTEM_PROMPT,
            temperature=0.8,        # playful, unlike extraction's 0.0
            max_output_tokens=400,  # it's a chat reply - keep it short and cheap
        ),
        label="chat",
    )
    if response is None:
        return None, error_kind

    reply = (response.text or "").strip()
    if not reply:
        # Usually a safety block or a hit max_output_tokens with nothing usable.
        logger.warning("Gemini returned an empty chat reply")
        return None, "error"

    logger.debug("Chat reply: %r", reply[:200])
    return reply, None
