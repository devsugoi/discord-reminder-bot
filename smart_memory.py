"""Smart conversation memory - saves important context, not everything.

Only saves when the AI detects valuable information worth remembering.
Only loads when the current message might benefit from past context.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("reminderbot.memory")

# Patterns that suggest a message might need memory context
MEMORY_TRIGGER_PATTERNS = [
    r"\b(?:remember|recall|told you|mentioned|said before|last time)\b",
    r"\b(?:alala|sabi ko|sinabi ko|nabanggit)\b",  # Tagalog: remember, I said
    r"\b(?:my|our|their)\s+(?:name|preference|favorite|usual)\b",
    r"\b(?:as usual|like before|same as)\b",
]

# Patterns that suggest the conversation contains information worth saving
SAVE_WORTHY_PATTERNS = [
    r"\b(?:I am|I'm|ako ay|ako'y)\s+(?:a|an)?\s*\w+",  # Identity: "I am a doctor"
    r"\b(?:I work|nagtatrabaho|trabaho ko)\b",  # Work-related
    r"\b(?:I like|I love|gusto ko|favorite ko)\b",  # Preferences
    r"\b(?:I prefer|mas gusto ko|preference ko)\b",
    r"\b(?:always|never|usually|lagi|hindi ko|palagi)\b",  # Habits/patterns
    r"\b(?:call me|tawag|pangalan ko)\b",  # Name preferences
    r"\b(?:remember|tandaan|alalahanin|huwag kalimutan)\b",  # Explicit remember requests
    r"\b(?:my portfolio|portfolio ko|my (?:github|linkedin|website))\b",  # Portfolio/links
]


def should_load_memory(message_text: str) -> bool:
    """Quick check if this message might benefit from memory context.

    Uses pattern matching - no AI tokens spent here.
    Returns True if the message references past conversations or personal context.
    """
    text_lower = message_text.lower()

    # Check for memory trigger patterns
    for pattern in MEMORY_TRIGGER_PATTERNS:
        if re.search(pattern, text_lower, re.IGNORECASE):
            logger.debug("Memory trigger detected: pattern matched")
            return True

    # Questions about personal info might need context
    personal_questions = [
        "who am i", "sino ako", "what's my", "ano ang", "anong",
        "do you know", "alam mo ba", "remember me", "portfolio ko"
    ]
    if any(phrase in text_lower for phrase in personal_questions):
        logger.debug("Memory trigger detected: personal question")
        return True

    return False


async def ai_should_load_memory(message_text: str) -> bool:
    """Ask AI if this message would benefit from memory context.

    Hybrid backup: Called when patterns don't match, to catch edge cases.
    Uses ~50 tokens for a quick yes/no decision.

    Args:
        message_text: The user's message

    Returns:
        True if AI thinks memory context would help, False otherwise
    """
    import logging
    logger = logging.getLogger("reminderbot.memory")

    # Quick AI prompt for yes/no decision
    prompt = f"""Does this message ask about or reference personal information that might be stored in memory?

Message: "{message_text}"

Consider:
- Questions about their portfolio, GitHub, LinkedIn, work
- Questions about preferences or past conversations
- References to "my [thing]" or asking "what's my [thing]"
- Tagalog equivalents (anong, ano ang, portfolio ko, github ko)

Answer ONLY with JSON:
{{"needs_memory": true}}  or  {{"needs_memory": false}}

Be generous - if there's any chance memory would help, say true."""

    try:
        from google import genai
        from google.genai import types as genai_types
        import ai_parser
        import json

        keys = ai_parser.chat_keys()
        if not keys:
            return False

        client = ai_parser._client_for(keys[0])

        response = await client.aio.models.generate_content(
            model=ai_parser.CHATBOT_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=50,  # Very short response
            )
        )

        if not response or not response.text:
            return False

        text = response.text.strip()
        # Try to parse JSON
        json_match = re.search(r'\{[^}]+\}', text)
        if json_match:
            data = json.loads(json_match.group(0))
            result = data.get("needs_memory", False)
            logger.debug(f"AI memory loading decision: {result}")
            return result

        return False

    except Exception as e:
        logger.debug(f"AI memory loading check failed: {e}")
        return False


def might_contain_saveable_info(message_text: str, bot_reply: str) -> bool:
    """Quick pattern check if this exchange might contain information worth saving.

    Uses simple pattern matching - no AI tokens spent.
    Returns True if patterns suggest personal information was shared or AI should evaluate.

    This is a pre-filter to avoid calling the AI for obvious casual chat.
    """
    combined = (message_text + " " + bot_reply).lower()

    # Definitely skip obvious casual chat
    casual_only = ["haha", "lol", "ok", "thanks", "salamat", "nice", "cool", "yeah", "yup", "nope"]
    words = message_text.lower().split()
    if len(words) <= 3 and all(w in casual_only for w in words):
        return False

    # Check for save-worthy patterns
    for pattern in SAVE_WORTHY_PATTERNS:
        if re.search(pattern, combined, re.IGNORECASE):
            logger.debug("Saveable pattern detected, will ask AI to evaluate")
            return True

    # If message is substantial (10+ words), let AI evaluate
    # Could be valuable context even without keyword matches
    user_words = message_text.split()
    if len(user_words) >= 10:
        # But skip if it's mostly casual words
        casual_count = sum(1 for w in user_words[:10] if w.lower() in casual_only)
        if casual_count < 5:  # Less than half casual words
            logger.debug("Substantial message detected, will ask AI to evaluate")
            return True

    return False


async def analyze_for_memory(
    user_message: str,
    bot_reply: str,
    author_name: str,
) -> Optional[tuple[str, str]]:
    """Analyze conversation to extract memorable information using hybrid approach.

    Returns (memory_key, memory_value) if something should be saved, None otherwise.

    **Hybrid Approach:**
    1. Try pattern-based extraction first (zero tokens, fast)
    2. If patterns don't find anything, ask AI (smart backup)

    Args:
        user_message: What the user said
        bot_reply: What the bot replied
        author_name: User's display name

    Returns:
        Tuple of (memory_key, memory_value) or None
    """
    import logging
    logger = logging.getLogger("reminderbot.memory")

    combined = user_message.lower()

    # ============================================================
    # STEP 1: Try pattern-based extraction first (zero tokens)
    # ============================================================

    # Portfolio/Links (explicit remember requests)
    if "tandaan" in combined or "remember" in combined or "alalahanin" in combined:
        # Extract URLs from the message
        url_pattern = r'https?://[^\s]+'
        urls = re.findall(url_pattern, user_message)
        if urls:
            url = urls[0]  # Take the first URL
            # Check what the user called it first, then check the URL
            if "portfolio" in combined or "website" in combined:
                logger.info("Pattern matched: portfolio link")
                return ("portfolio_link", url)
            elif "github" in combined or "github" in url:
                logger.info("Pattern matched: GitHub link")
                return ("github_link", url)
            elif "linkedin" in combined or "linkedin" in url:
                logger.info("Pattern matched: LinkedIn link")
                return ("linkedin_link", url)
            else:
                logger.info("Pattern matched: generic link")
                return ("work_link", url)

    # Work/role information
    work_patterns = [
        r"(?:I am|I'm|ako ay|ako'y)\s+(?:a|an)\s+(\w+(?:\s+\w+){0,2})",
        r"(?:I work as|trabaho ko ay|I'm working as)\s+(?:a|an)?\s*(\w+(?:\s+\w+){0,2})",
    ]
    for pattern in work_patterns:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            role = match.group(1).strip()
            if len(role) > 2 and role not in ["a", "an", "the"]:
                logger.info("Pattern matched: work/role information")
                return ("work_role", role)

    # Preferences
    if "prefer" in combined or "gusto ko" in combined or "mas gusto" in combined:
        pref_match = re.search(r"(?:prefer|gusto ko|mas gusto)\s+(.{10,50})", combined, re.IGNORECASE)
        if pref_match:
            preference = pref_match.group(1).strip()
            logger.info("Pattern matched: preference")
            return ("user_preference", preference)

    # ============================================================
    # STEP 2: Patterns didn't find anything - ask AI as backup
    # ============================================================

    logger.debug("Patterns didn't match, asking AI to evaluate...")

    # Import here to avoid circular dependency
    from google import genai
    from google.genai import types as genai_types
    import ai_parser

    # Build prompt for AI to analyze
    prompt = f"""Analyze this Discord conversation and decide if it contains information worth remembering about the user.

User: {author_name}
Their message: "{user_message}"
Bot's reply: "{bot_reply}"

Should we remember anything from this exchange? Consider:
- Personal information (job, role, background)
- Preferences (language, style, favorites)
- Important links (portfolio, GitHub, LinkedIn, website)
- Context that would help future conversations
- Explicit "remember this" or "tandaan mo" requests

Do NOT remember:
- Casual chat ("haha", "ok thanks", "nice")
- Temporary information
- Questions without answers
- Generic responses

If there's something worth remembering, respond with JSON:
{{
  "should_save": true,
  "memory_type": "custom_note",
  "memory_text": "Brief description of what to remember",
  "reason": "Why this is worth remembering"
}}

If nothing worth remembering, respond with:
{{
  "should_save": false,
  "reason": "Why not"
}}

Be conservative - only save truly useful information."""

    try:
        # Use chat keys for this since it's part of the chat flow
        keys = ai_parser.chat_keys()
        if not keys:
            logger.warning("No API keys available for memory analysis")
            return None

        client = ai_parser._client_for(keys[0])

        response = await client.aio.models.generate_content(
            model=ai_parser.CHATBOT_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.1,  # Low temperature for consistent decisions
                max_output_tokens=200,
            )
        )

        if not response or not response.text:
            return None

        # Parse JSON response
        import json

        # Extract JSON from response (might have markdown code blocks)
        text = response.text.strip()
        json_match = re.search(r'\{[^}]+\}', text, re.DOTALL)
        if not json_match:
            return None

        data = json.loads(json_match.group(0))

        if data.get("should_save"):
            memory_text = data.get("memory_text", "").strip()
            if memory_text:
                logger.info(
                    "AI decided to save memory: %s (reason: %s)",
                    memory_text[:50],
                    data.get("reason", "")
                )
                # Use a more specific memory type to avoid overwriting
                memory_type = data.get("memory_type", "custom_note")
                if memory_type == "custom_note":
                    # Make it more specific based on content
                    if "portfolio" in memory_text.lower() or "github.io" in memory_text.lower():
                        memory_type = "portfolio_link"
                    elif "github.com" in memory_text.lower():
                        memory_type = "github_link"
                    elif "linkedin" in memory_text.lower():
                        memory_type = "linkedin_link"
                    elif "work" in memory_text.lower() or "job" in memory_text.lower():
                        memory_type = "work_role"
                    elif "prefer" in memory_text.lower():
                        memory_type = "user_preference"

                return (memory_type, memory_text)

        logger.debug("AI decided not to save: %s", data.get("reason", ""))
        return None

    except Exception as e:
        logger.warning("Memory analysis failed (non-critical): %s", str(e))
        return None


async def save_conversation_memory(
    user_id: int,
    user_message: str,
    bot_reply: str,
    author_name: str,
) -> bool:
    """Save important information from a conversation if it contains anything valuable.

    Returns True if something was saved, False otherwise.
    This is the main entry point called after each chat reply.
    """
    # Quick pattern check first (no tokens)
    if not might_contain_saveable_info(user_message, bot_reply):
        return False

    # Try to extract memorable info
    result = await analyze_for_memory(user_message, bot_reply, author_name)

    if result is None:
        return False

    memory_key, memory_value = result

    # Save to database
    import db
    db.save_user_memory(
        user_id=user_id,
        memory_key=memory_key,
        memory_value=memory_value,
        context=""
    )

    logger.info(
        "Saved conversation memory for user %s: %s",
        user_id,
        memory_value[:50]
    )
    return True
