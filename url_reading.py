"""Optional link reading for the chatbot's @mention replies via Gemini URL Context."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

import video_understanding

logger = logging.getLogger("reminderbot.url_reading")

# --- Configuration -----------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


CHATBOT_URL_READING_ENABLED = _env_bool("CHATBOT_URL_READING_ENABLED", False)
CHATBOT_URL_READING_MAX_CALLS_PER_DAY = int(
    os.getenv("CHATBOT_URL_READING_MAX_CALLS_PER_DAY", "15")
)
CHATBOT_URL_READING_MAX_URLS_PER_MESSAGE = int(
    os.getenv("CHATBOT_URL_READING_MAX_URLS_PER_MESSAGE", "3")
)
CHATBOT_URL_READING_BLOCK_PRIVATE = _env_bool("CHATBOT_URL_READING_BLOCK_PRIVATE", True)

_URL_PATTERN = re.compile(
    r"https?://[^\s<>\"')\]]+",
    re.IGNORECASE,
)

_TRAILING_PUNCT = re.compile(r"[.,;:!?)>\]]+$")

_YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"})


@dataclass
class UrlInput:
    """Prepared URLs for one chat reply."""

    urls: list[str] = field(default_factory=list)
    uses_url_budget: bool = False
    rejected: list[str] = field(default_factory=list)


def chatbot_url_reading_is_on() -> bool:
    """Whether link reading is on (env default + live /settings override)."""
    import db

    saved = db.get_setting("chatbot_url_reading_enabled", "")
    return saved == "on" if saved else CHATBOT_URL_READING_ENABLED


def url_reading_enabled() -> bool:
    return chatbot_url_reading_is_on()


def _normalize_url(raw: str) -> str:
    return _TRAILING_PUNCT.sub("", raw.strip())


def extract_urls(text: str) -> list[str]:
    """Return unique http(s) URLs from message text, in order."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_PATTERN.finditer(text):
        url = _normalize_url(match.group(0))
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _host_is_private(hostname: str) -> bool:
    lowered = hostname.lower().rstrip(".")
    if lowered in {"localhost", "0.0.0.0"} or lowered.endswith(".localhost"):
        return True
    if lowered == "::1" or lowered.startswith("fe80:") or lowered.startswith("fc") or lowered.startswith("fd"):
        return True

    # Bracketed IPv6, e.g. [::1]
    if lowered.startswith("[") and lowered.endswith("]"):
        lowered = lowered[1:-1]

    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        return False

    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def is_safe_url(url: str) -> bool:
    if not CHATBOT_URL_READING_BLOCK_PRIVATE:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc:
        return False

    host = parsed.hostname or ""
    if not host:
        return False
    if _host_is_private(host):
        return False
    return True


def _is_youtube_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in _YOUTUBE_HOSTS


def exclude_youtube(urls: list[str]) -> list[str]:
    """Drop YouTube URLs when the video feature handles them."""
    if not (
        video_understanding.video_enabled() and video_understanding.youtube_enabled()
    ):
        return urls
    return [url for url in urls if not _is_youtube_url(url)]


def filter_urls(urls: list[str]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    rejected: list[str] = []
    for url in urls:
        if is_safe_url(url):
            allowed.append(url)
        else:
            rejected.append(url)
    return allowed, rejected


def prepare_urls_for_gemini(text: str) -> UrlInput:
    if not url_reading_enabled():
        return UrlInput()

    raw_urls = extract_urls(text)
    if not raw_urls:
        return UrlInput()

    raw_urls = exclude_youtube(raw_urls)
    allowed, rejected = filter_urls(raw_urls)
    if rejected:
        logger.debug("Rejected unsafe URLs: %s", rejected)

    capped = allowed[:CHATBOT_URL_READING_MAX_URLS_PER_MESSAGE]
    if not capped:
        return UrlInput(rejected=rejected)

    return UrlInput(
        urls=capped,
        uses_url_budget=True,
        rejected=rejected,
    )
