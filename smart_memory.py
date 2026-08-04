"""Server-wide context memory — one compact document per guild.

After each chat reply, a gated LLM step may rewrite the guild's standing
context (rules, nicknames, durable facts) while keeping it under a hard
character limit.
"""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger("reminderbot.memory")

# Patterns suggesting a message may establish or change standing server context
DURABLE_INSTRUCTION_PATTERNS = [
    r"\b(?:remember|tandaan|alalahanin|huwag kalimutan)\b",
    r"\b(?:from now on|starting now|always|never|usually|lagi|palagi)\b",
    r"\b(?:everyone|this server|the server|whole server|server-wide|for this server)\b",
    r"\b(?:speak|reply|respond|use|talk)\s+(?:in|using)\s+\w+",
    r"\b(?:call|tawag)\s+(?:me|mo|kay|sa)\b",
    r"\b(?:call|tawag)\s+<@!?(\d+)>",
    r"\b(?:keep (?:it )?(?:short|brief|under \d+))\b",
    r"\b(?:my portfolio|portfolio ko|my (?:github|linkedin|website))\b",
    r"\b(?:I am|I'm|ako ay|ako'y)\s+(?:a|an)?\s*\w+",
    r"\b(?:I prefer|gusto ko|mas gusto)\b",
    r"\b(?:forget|clear|reset)\s+(?:the\s+)?(?:server|guild)\s+(?:memory|context|rules)\b",
]

CASUAL_ONLY = frozenset(
    ["haha", "lol", "ok", "thanks", "salamat", "nice", "cool", "yeah", "yup", "nope"]
)


def might_contain_durable_instruction(user_message: str, bot_reply: str) -> bool:
    """Quick pattern check — skip obvious casual chat, flag likely standing rules."""
    combined = (user_message + " " + bot_reply).lower()
    words = user_message.lower().split()
    if len(words) <= 3 and all(w in CASUAL_ONLY for w in words):
        return False

    for pattern in DURABLE_INSTRUCTION_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            logger.debug("Durable instruction pattern matched")
            return True

    if len(words) >= 8:
        casual_count = sum(1 for w in words[:10] if w in CASUAL_ONLY)
        if casual_count < 4:
            logger.debug("Substantial message — will evaluate for context update")
            return True

    return False


async def maybe_update_server_context(
    guild_id: int,
    user_message: str,
    bot_reply: str,
    author_name: str,
) -> bool:
    """Evaluate whether to rewrite the guild's compact server context.

    Returns True if context was updated.
    """
    if guild_id is None:
        return False

    if not might_contain_durable_instruction(user_message, bot_reply):
        return False

    import db

    current = db.get_server_context(guild_id)
    rewritten = await _rewrite_server_context(
        current_context=current,
        user_message=user_message,
        bot_reply=bot_reply,
        author_name=author_name,
    )
    if rewritten is None:
        return False

    normalized_current = current.strip()
    normalized_new = rewritten.strip()
    if normalized_new == normalized_current:
        return False

    db.set_server_context(guild_id, normalized_new)
    logger.info(
        "Updated server context for guild %s (%d chars)",
        guild_id,
        len(normalized_new),
    )
    return True


async def _rewrite_server_context(
    current_context: str,
    user_message: str,
    bot_reply: str,
    author_name: str,
) -> Optional[str]:
    """Ask the LLM to merge new info into a compact bullet-list context."""
    import ai_parser
    import db

    keys = ai_parser.chat_keys()
    if not keys:
        logger.warning("No API keys available for server context rewrite")
        return None

    max_chars = db.SERVER_CONTEXT_MAX_CHARS

    prompt = f"""You maintain a compact standing-context document for a Discord server bot.

Current server context (may be empty):
\"\"\"
{current_context or "(empty)"}
\"\"\"

Latest exchange:
{author_name}: "{user_message}"
Bot: "{bot_reply}"

Decide if this exchange adds, changes, or removes a DURABLE standing rule or fact
that should apply across the whole server (language, tone, nicknames, user facts
worth remembering for everyone, explicit "remember for this server" requests).

Do NOT save:
- Casual chat, jokes, one-off questions
- Temporary or ephemeral information
- Things already fully captured in current context

If nothing durable changed, respond with JSON only:
{{"update": false}}

If context should change, rewrite the ENTIRE context as a compact bullet list
(max {max_chars} characters total). Merge new info, drop duplicates, resolve
conflicts (newer wins), keep Discord mention format like <@USER_ID> when referring
to users. Respond with JSON only:
{{"update": true, "context": "- bullet one\\n- bullet two"}}

Be conservative — only update for genuinely useful standing instructions."""

    try:
        from google.genai import types as genai_types

        client = ai_parser._client_for(keys[0])
        response = await client.aio.models.generate_content(
            model=ai_parser.CHATBOT_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=600,
            ),
        )

        if not response or not response.text:
            return None

        text = response.text.strip()
        json_match = re.search(r"\{[\s\S]*\}", text)
        if not json_match:
            return None

        data = json.loads(json_match.group(0))
        if not data.get("update"):
            logger.debug("LLM decided no server context update needed")
            return None

        new_context = (data.get("context") or "").strip()
        if not new_context:
            return None

        return db.trim_context_to_limit(new_context)

    except Exception as e:
        logger.warning("Server context rewrite failed (non-critical): %s", e)
        return None
