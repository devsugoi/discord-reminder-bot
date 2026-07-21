"""Optional web search support for the reminder bot's conversational replies."""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger("reminderbot.web_search")

WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "").strip().lower()
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY", "").strip()
WEB_SEARCH_ENGINE_ID = os.getenv("WEB_SEARCH_ENGINE_ID", "").strip()
WEB_SEARCH_RESULT_COUNT = int(os.getenv("WEB_SEARCH_RESULT_COUNT", "3"))

_SUPPORTED_PROVIDERS = {"serpapi", "google_cse"}


def search_enabled() -> bool:
    if WEB_SEARCH_PROVIDER not in _SUPPORTED_PROVIDERS:
        return False
    if not WEB_SEARCH_API_KEY:
        return False
    if WEB_SEARCH_PROVIDER == "google_cse" and not WEB_SEARCH_ENGINE_ID:
        return False
    return True


async def search_web(query: str) -> Tuple[Optional[str], Optional[str]]:
    if not search_enabled():
        return None, "search not enabled"
    return await asyncio.to_thread(_search_web_sync, query)


def _search_web_sync(query: str) -> Tuple[Optional[str], Optional[str]]:
    if not query.strip():
        return None, "empty query"

    if WEB_SEARCH_PROVIDER == "serpapi":
        return _search_serpapi(query)
    if WEB_SEARCH_PROVIDER == "google_cse":
        return _search_google_cse(query)

    return None, f"unsupported provider: {WEB_SEARCH_PROVIDER}"


def _search_serpapi(query: str) -> Tuple[Optional[str], Optional[str]]:
    params = {
        "engine": "google",
        "q": query,
        "api_key": WEB_SEARCH_API_KEY,
        "num": str(WEB_SEARCH_RESULT_COUNT),
        "google_domain": "google.com",
        "gl": "us",
        "hl": "en",
    }
    url = f"https://serpapi.com/search.json?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        logger.warning("SerpAPI HTTP error: %s", error)
        return None, f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        logger.warning("SerpAPI request failed: %s", error)
        return None, "request failed"

    return _summarize_serpapi_results(query, payload)


def _search_google_cse(query: str) -> Tuple[Optional[str], Optional[str]]:
    params = {
        "key": WEB_SEARCH_API_KEY,
        "cx": WEB_SEARCH_ENGINE_ID,
        "q": query,
        "num": str(WEB_SEARCH_RESULT_COUNT),
    }
    url = f"https://www.googleapis.com/customsearch/v1?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        logger.warning("Google CSE HTTP error: %s", error)
        return None, f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        logger.warning("Google CSE request failed: %s", error)
        return None, "request failed"

    return _summarize_cse_results(query, payload)


def _summarize_serpapi_results(query: str, payload: dict) -> Tuple[Optional[str], Optional[str]]:
    if not payload:
        return None, "no payload"

    lines: list[str] = [f"Search results for: {query}"]
    answer_box = payload.get("answer_box") or payload.get("knowledge_graph")
    if isinstance(answer_box, dict):
        snippet = answer_box.get("snippet") or answer_box.get("description")
        if snippet:
            lines.append(f"Quick answer: {snippet}")
    organic = payload.get("organic_results") or []
    if isinstance(organic, list):
        for index, item in enumerate(organic[:WEB_SEARCH_RESULT_COUNT], start=1):
            title = item.get("title", "(no title)").strip()
            snippet = item.get("snippet", "").strip()
            link = item.get("link") or item.get("displayed_link") or ""
            line = f"{index}. {title}"
            if snippet:
                line += f" - {snippet}"
            if link:
                line += f" ({link})"
            lines.append(line)

    if len(lines) <= 1:
        return None, "no results"
    return "\n".join(lines), None


def _summarize_cse_results(query: str, payload: dict) -> Tuple[Optional[str], Optional[str]]:
    if not payload:
        return None, "no payload"

    lines: list[str] = [f"Search results for: {query}"]
    if isinstance(payload.get("queries"), dict):
        request_info = payload["queries"].get("request")
        if isinstance(request_info, list) and request_info:
            source = request_info[0].get("searchTerms")
            if source:
                lines[0] = f"Search results for: {source}"

    items = payload.get("items") or []
    if isinstance(items, list):
        for index, item in enumerate(items[:WEB_SEARCH_RESULT_COUNT], start=1):
            title = item.get("title", "(no title)").strip()
            snippet = item.get("snippet", "").strip()
            link = item.get("link", "").strip()
            line = f"{index}. {title}"
            if snippet:
                line += f" - {snippet}"
            if link:
                line += f" ({link})"
            lines.append(line)

    if len(lines) <= 1:
        return None, "no results"
    return "\n".join(lines), None
