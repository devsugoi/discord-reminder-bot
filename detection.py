"""Free bilingual (Tagalog + English) regex prescan.

This runs on every single message, entirely on the Pi, at zero cost.
Its only job is deciding whether a message *might* be about a debt or a
reminder, so it is worth spending one (free-tier) AI call on. The AI does
the real understanding afterwards.

Design notes:
- Tagalog verbs conjugate with prefixes and infixes (utang -> umutang,
  nangutang, pautang), so we match on word ROOTS with \\w* around them
  instead of exact words. A few infixed forms (humiram, hiniram,
  binayaran) break the root apart, so those are listed explicitly.
- False positives are cheap here (one small AI call that answers "none").
  False negatives mean a missed detection. So the patterns lean permissive.
"""

import re

# ---------------------------------------------------------------------------
# Money amounts
# Matches: ₱500 | $20.50 | 500 pesos | 20 bucks | 1k | 2.5k | bare "500"
# and colloquial Filipino/Spanish number words: bente, singkwenta, libo ...
#
# Bare 2+ digit numbers count too, because Filipinos usually type amounts with
# no symbol at all ("sent you 500 via gcash"). This only matters when a weak
# debt word is ALSO present, so ordinary numbers in chat stay ignored.
# ---------------------------------------------------------------------------
MONEY_PATTERN = re.compile(
    r"(?:[$€£₱]\s?\d[\d,]*(?:\.\d{1,2})?"                                     # ₱500, $20.50
    r"|\b\d[\d,]*(?:\.\d{1,2})?\s?"
    r"(?:dollars?|bucks?|usd|php|pesos?|piso|euros?|eur|pounds?|quid)\b"      # 500 pesos, 20 bucks
    r"|\b\d+(?:\.\d+)?k\b"                                                    # 1k, 2.5k
    r"|\b\d{2,}[\d,]*(?:\.\d{1,2})?\b"                                        # bare 500, 1,500.50
    r"|\b(?:bente|trenta|kwarenta|singkwenta|sisenta|sitenta"
    r"|otsenta|nobenta|siyento|libo|sanlibo)\b)",                             # bente = 20, libo = 1000
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# DEBT — strong signals: enough on their own to flag the message.
# ---------------------------------------------------------------------------
_DEBT_STRONG_PATTERNS = [
    # -- Tagalog roots --
    r"\w*utang\w*",          # utang, umutang, nangutang, inutang, pautang, may utang...
    r"\w*hiram\w*",          # hiram, pahiram, hiramin, nanghiram...
    r"\bh(?:um|in)iram\b",   # humiram / hiniram (infix breaks the 'hiram' root)
    r"\w*bayad\w*",          # bayad, magbayad, magbabayad, nagbayad...
    r"\w*bayaran\b",         # bayaran, babayaran...
    r"\bbinayaran\b",        # 'paid (it)' (infixed form)
    r"\bnabayaran\b",        # 'was paid'
    r"\w*abono\w*",          # abono, inabonohan (covered/advanced money for someone)
    r"\w*singil\w*",         # singil, singilin, sisingilin (collecting payment)
    r"\bnaniningil\b",       # 'is collecting payment' (infixed form)
    r"\bquits\b",            # Taglish: "quits na tayo" = we're even
    # -- English --
    r"\bowes?\b|\bowed\b|\bowing\b",
    r"\biou\b",
    r"\bpay\s+(?:you|me|him|her|them)\s+back\b",
    r"\bpaid\s+(?:you|me|him|her|them)?\s*back\b",
    r"\bpay\s+(?:you|me)\s+(?:on|by|next|this|tomorrow|when|after)\b",
    r"\bwe(?:'re| are|re)\s+(?:even|square)\b",
    r"\bsettled?\s+up\b",
    r"\blent\b|\blend\s+(?:me|you)\b|\bborrow(?:ed|ing)?\b",
    r"\bspot\s+me\b",
    r"\bcover(?:ed)?\s+(?:for\s+)?(?:me|you)\b",
]

# ---------------------------------------------------------------------------
# DEBT — weak signals: only count when a money amount is also present.
# ("sagot" alone just means "answer"; "pera" alone is any money talk.)
# ---------------------------------------------------------------------------
_DEBT_WEAK_PATTERNS = [
    r"\bpera\b",                                   # money
    r"\bsweldo\b",                                 # salary / payday
    r"\bsagot\s+(?:ko|mo|niya)\b",                 # "sagot ko" = I'll cover it
    r"\w*padala\w*",                               # send money (magpadala, pinadala)
    r"\bgcash\w*\b|\bmaya\b|\bvenmo\w*\b|\bpaypal\b|\bzelle\w*\b|\bcash\s?app\b",
    r"\bpay\b|\bpaid\b|\bpaying\b|\bpayment\b",
    r"\bsen[dt]\b|\bsending\b",
    r"\bgood\s+for\s+it\b",
]

# ---------------------------------------------------------------------------
# REMINDER — strong signals.
# ---------------------------------------------------------------------------
_REMINDER_STRONG_PATTERNS = [
    # -- Tagalog --
    r"\w*paalala\w*",        # paalala, ipaalala, paalalahanan, magpaalala...
    r"\bkalimutan\b",        # (wag/huwag) kalimutan = don't forget
    r"\bmakalimutan\b",      # baka makalimutan = might forget
    r"\balalahanin\b",       # remember it / keep it in mind
    r"\btandaan\b",          # "tandaan mo" = remember this
    # -- English --
    r"\bremind(?:er|ers)?\b",
    r"\bdon'?t\s+forget\b",
    r"\bremember\s+to\b",
    r"\bnote\s+to\s+self\b",
]

_debt_strong_regex = re.compile("|".join(_DEBT_STRONG_PATTERNS), re.IGNORECASE)
_debt_weak_regex = re.compile("|".join(_DEBT_WEAK_PATTERNS), re.IGNORECASE)
_reminder_strong_regex = re.compile("|".join(_REMINDER_STRONG_PATTERNS), re.IGNORECASE)


def prescan(message_text: str) -> set[str]:
    """Classify a message into zero or more rough categories.

    Returns a set that may contain "debt" and/or "reminder".
    An empty set means: not interesting, don't spend an AI call
    (unless a hot conversation window says otherwise - see bot.py).
    """
    categories: set[str] = set()
    if not message_text:
        return categories

    if _debt_strong_regex.search(message_text):
        categories.add("debt")
    elif _debt_weak_regex.search(message_text) and MONEY_PATTERN.search(message_text):
        # Weak words like "pay"/"pera" only count next to an actual amount.
        categories.add("debt")

    if _reminder_strong_regex.search(message_text):
        categories.add("reminder")

    return categories
