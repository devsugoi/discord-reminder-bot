"""Daily Philippines news digest: RSS fetch, optional AI shorten, Discord formatting."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

logger = logging.getLogger("reminderbot.news")

_DEFAULT_PH_FEEDS = (
    "https://newsinfo.inquirer.net/feed",
    "https://www.rappler.com/feed/",
    "https://www.philstar.com/rss/headlines",
)
_DEFAULT_WORLD_FEEDS = (
    "http://feeds.bbci.co.uk/news/world/rss.xml",
)

NEWS_PH_FEEDS = [
    part.strip()
    for part in os.getenv("NEWS_PH_FEEDS", ",".join(_DEFAULT_PH_FEEDS)).split(",")
    if part.strip()
]
NEWS_WORLD_FEEDS = [
    part.strip()
    for part in os.getenv("NEWS_WORLD_FEEDS", ",".join(_DEFAULT_WORLD_FEEDS)).split(",")
    if part.strip()
]
NEWS_PH_COUNT = int(os.getenv("NEWS_PH_COUNT", "5"))
NEWS_WORLD_COUNT = int(os.getenv("NEWS_WORLD_COUNT", "3"))
NEWS_USE_AI = os.getenv("NEWS_USE_AI", "true").strip().lower() in ("1", "true", "yes", "on")
NEWS_MAX_CHARS = int(os.getenv("NEWS_MAX_CHARS", "1800"))
NEWS_INCLUDE_LINKS_DEFAULT = os.getenv("NEWS_INCLUDE_LINKS", "true").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
NEWS_TAGLISH_DEFAULT = os.getenv("NEWS_TAGLISH_DEFAULT", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
NEWS_MAX_CALLS_PER_DAY = int(os.getenv("NEWS_MAX_CALLS_PER_DAY", "2"))

_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass(frozen=True)
class HeadlineItem:
    title: str
    link: str
    source_name: str | None = None


def parse_news_time(value: str) -> tuple[int, int] | None:
    """Parse HH:MM into (hour, minute), or None if invalid."""
    match = _TIME_PATTERN.match(value.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def validate_news_time(value: str) -> bool:
    return parse_news_time(value) is not None


def should_deliver_now(now: datetime, configured_time: str) -> bool:
    """True once local time has reached the configured delivery time."""
    parsed = parse_news_time(configured_time)
    if parsed is None:
        return False
    hour, minute = parsed
    now_minutes = now.hour * 60 + now.minute
    target_minutes = hour * 60 + minute
    return now_minutes >= target_minutes


def include_links_enabled(stored_value: str) -> bool:
    """Resolve per-guild stored value, falling back to env default."""
    if not stored_value.strip():
        return NEWS_INCLUDE_LINKS_DEFAULT
    return stored_value.strip().lower() in ("1", "true", "yes", "on")


def taglish_enabled(stored_value: str) -> bool:
    """Resolve per-guild language setting, falling back to env default."""
    if not stored_value.strip():
        return NEWS_TAGLISH_DEFAULT
    return stored_value.strip().lower() == "taglish"


async def fetch_headlines(feed_urls: Iterable[str], limit: int) -> list[HeadlineItem]:
    """Fetch up to `limit` headlines across the given feed URLs."""
    if limit <= 0:
        return []
    items: list[HeadlineItem] = []
    seen_titles: set[str] = set()
    for feed_url in feed_urls:
        if len(items) >= limit:
            break
        try:
            batch = await asyncio.to_thread(_fetch_feed_sync, feed_url, limit - len(items))
        except Exception:
            logger.exception("Failed to fetch news feed: %s", feed_url)
            continue
        for item in batch:
            key = item.title.casefold()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            items.append(item)
            if len(items) >= limit:
                break
    return items


async def fetch_all_headlines() -> tuple[list[HeadlineItem], list[HeadlineItem]]:
    """Fetch Philippines and world headline lists."""
    ph_items, world_items = await asyncio.gather(
        fetch_headlines(NEWS_PH_FEEDS, NEWS_PH_COUNT),
        fetch_headlines(NEWS_WORLD_FEEDS, NEWS_WORLD_COUNT),
    )
    return ph_items, world_items


async def summarize_digest(
    ph_items: list[HeadlineItem],
    world_items: list[HeadlineItem],
    *,
    use_ai: bool = True,
    taglish: bool = False,
) -> tuple[list[HeadlineItem], list[HeadlineItem]]:
    """Optionally shorten or rewrite titles with Gemini; links stay attached by index."""
    if not ph_items and not world_items:
        return ph_items, world_items
    if not use_ai:
        return ph_items, world_items
    if not taglish and not NEWS_USE_AI:
        return ph_items, world_items

    from ai_parser import shorten_news_headlines

    combined = ph_items + world_items
    shortened = await shorten_news_headlines(
        [item.title for item in combined],
        taglish=taglish,
    )
    if shortened is None or len(shortened) != len(combined):
        return ph_items, world_items

    updated = [
        HeadlineItem(
            title=_clean_title(new_title) or item.title,
            link=item.link,
            source_name=item.source_name,
        )
        for item, new_title in zip(combined, shortened)
    ]
    split_at = len(ph_items)
    return updated[:split_at], updated[split_at:]


def format_digest(
    ph_items: list[HeadlineItem],
    world_items: list[HeadlineItem],
    *,
    include_links: bool,
    when: datetime | None = None,
) -> str:
    """Render the daily digest as Discord markdown."""
    when = when or datetime.now()
    header = f"📰 Daily News — {when.strftime('%b %d, %Y')}\n\n"
    message = header + _digest_body(ph_items, world_items, include_links=include_links)
    if len(message) <= NEWS_MAX_CHARS:
        return message

    ph = list(ph_items)
    world = list(world_items)
    while ph or world:
        body = _digest_body(ph, world, include_links=include_links)
        candidate = header + body
        if len(candidate) <= NEWS_MAX_CHARS:
            return candidate
        if world:
            world.pop()
        elif ph:
            ph.pop()
        else:
            break
    return header + "Headlines are too long to post right now."


def _digest_body(
    ph_items: list[HeadlineItem],
    world_items: list[HeadlineItem],
    *,
    include_links: bool,
) -> str:
    sections: list[str] = []
    if ph_items:
        lines = [_format_bullet(item, include_links) for item in ph_items]
        sections.append("**Philippines**\n" + "\n".join(lines))
    if world_items:
        lines = [_format_bullet(item, include_links) for item in world_items]
        sections.append("**World**\n" + "\n".join(lines))
    if not sections:
        return "No headlines available right now."
    return "\n\n".join(sections)


def _format_bullet(item: HeadlineItem, include_links: bool) -> str:
    title = _clean_title(item.title)
    if include_links and item.link:
        safe_title = title.replace("]", "\\]")
        return f"• [{safe_title}]({item.link})"
    return f"• {title}"


def _clean_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _fetch_feed_sync(feed_url: str, limit: int) -> list[HeadlineItem]:
    request = urllib.request.Request(
        feed_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; ReminderBot/1.0)"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read()
    return _parse_feed_xml(payload, feed_url, limit)


def _parse_feed_xml(payload: bytes, feed_url: str, limit: int) -> list[HeadlineItem]:
    root = ET.fromstring(payload)
    tag = _local_name(root.tag).lower()
    if tag == "rss":
        channel = root.find("channel")
        source_name = _element_text(channel.find("title")) if channel is not None else None
        item_nodes = channel.findall("item") if channel is not None else []
        return _items_from_rss_nodes(item_nodes, source_name, limit)
    if tag == "feed":
        source_name = _element_text(root.find("atom:title", _ATOM_NS)) or _element_text(
            root.find("title")
        )
        entries = root.findall("atom:entry", _ATOM_NS) or root.findall("entry")
        return _items_from_atom_entries(entries, source_name, limit)
    logger.warning("Unsupported feed format at %s (root=%s)", feed_url, tag)
    return []


def _items_from_rss_nodes(
    nodes: list[ET.Element],
    source_name: str | None,
    limit: int,
) -> list[HeadlineItem]:
    items: list[HeadlineItem] = []
    for node in nodes:
        if len(items) >= limit:
            break
        title = _element_text(node.find("title"))
        link = _element_text(node.find("link"))
        if not title:
            continue
        items.append(HeadlineItem(title=title, link=link, source_name=source_name))
    return items


def _items_from_atom_entries(
    entries: list[ET.Element],
    source_name: str | None,
    limit: int,
) -> list[HeadlineItem]:
    items: list[HeadlineItem] = []
    for entry in entries:
        if len(items) >= limit:
            break
        title = _element_text(entry.find("atom:title", _ATOM_NS)) or _element_text(
            entry.find("title")
        )
        link = _atom_link(entry)
        if not title:
            continue
        items.append(HeadlineItem(title=title, link=link, source_name=source_name))
    return items


def _atom_link(entry: ET.Element) -> str:
    for link_node in entry.findall("atom:link", _ATOM_NS) + entry.findall("link"):
        href = link_node.attrib.get("href", "").strip()
        rel = link_node.attrib.get("rel", "alternate").strip() or "alternate"
        if href and rel == "alternate":
            return href
    for link_node in entry.findall("atom:link", _ATOM_NS) + entry.findall("link"):
        href = link_node.attrib.get("href", "").strip()
        if href:
            return href
    return ""


def _element_text(node: ET.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return _clean_title(node.text)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag
