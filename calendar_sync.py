"""Google Calendar sync for Discord scheduled events.

When someone creates/edits/cancels a scheduled event in an allowed server,
bot.py calls into this module to mirror the change onto every linked Google
Calendar. Auth is a single Google *service account*: each person shares their
calendar with the service account's email (Google Calendar -> Settings ->
"Share with specific people" -> "Make changes to events"), then registers the
calendar id with /calendar link. No OAuth browser flows and no expiring
tokens - important because the bot runs headless on a Pi.

google-api-python-client is synchronous, so every network call here is pushed
onto a worker thread with asyncio.to_thread() to keep Discord's event loop
responsive.
"""

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import discord
import google_auth_httplib2
import httplib2
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

logger = logging.getLogger("reminderbot.calendar")

# The JSON key downloaded from Google Cloud (IAM -> Service Accounts -> Keys).
# Its mere existence is what switches the whole calendar feature on.
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")

# Read/write access - we insert, patch, and delete events.
_SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Google requires an end time but Discord voice/stage events often have none;
# assume they run this long.
DEFAULT_EVENT_HOURS = int(os.getenv("CALENDAR_DEFAULT_EVENT_HOURS", "1"))

# Transient network failures are normal here: Google hangs up on keep-alive
# connections that have been idle a few minutes, and the bot can go a long
# while between events. Every execute() below passes this so googleapiclient
# retries (with backoff) instead of surfacing a one-off ECONNRESET.
API_RETRIES = int(os.getenv("CALENDAR_API_RETRIES", "3"))

# Don't let a half-open socket wedge a worker thread indefinitely.
_HTTP_TIMEOUT_SECONDS = 30

# Lazily-built singleton, mirroring ai_parser's Gemini client: importing this
# module must never require credentials (the feature is optional).
_service = None


def is_configured() -> bool:
    """True when the service-account key file exists (= feature enabled)."""
    return os.path.exists(SERVICE_ACCOUNT_FILE)


def service_email() -> str:
    """The service account's email - the address people share calendars with."""
    with open(SERVICE_ACCOUNT_FILE, encoding="utf-8") as key_file:
        return json.load(key_file).get("client_email", "(unknown)")


def _get_service():
    global _service
    if _service is None:
        credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=_SCOPES
        )

        def _build_request(_unused_http, *args, **kwargs) -> HttpRequest:
            """Give every request its own connection.

            The service object built here is a long-lived singleton shared by
            asyncio.to_thread workers, but httplib2.Http is neither thread-safe
            nor safe to leave idle - Google drops idle keep-alive sockets and
            the next reuse dies with ECONNRESET. A fresh Http per request costs
            one handshake and sidesteps both. The credentials object still
            caches its access token, so this adds no token round-trips.
            """
            authorized_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http(timeout=_HTTP_TIMEOUT_SECONDS)
            )
            return HttpRequest(authorized_http, *args, **kwargs)

        # cache_discovery=False silences a warning about a legacy cache
        # mechanism that needs a library we don't install.
        _service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
            requestBuilder=_build_request,
        )
    return _service


# ---------------------------------------------------------------------------
# Discord event -> Google Calendar event translation
# ---------------------------------------------------------------------------

def _to_gcal_body(event: discord.ScheduledEvent) -> dict:
    """Translate a Discord scheduled event into a Google Calendar event body."""
    start = event.start_time  # timezone-aware UTC, straight from Discord
    end = event.end_time or (start + timedelta(hours=DEFAULT_EVENT_HOURS))

    # Where: external events carry a free-text location; voice/stage events
    # happen in a channel, so name the channel instead.
    location = event.location or ""
    if not location and event.channel is not None:
        location = f"#{event.channel.name} (Discord)"

    # The description keeps a link back to the Discord event so anyone looking
    # at their calendar can jump straight to it.
    description_parts = [part for part in (event.description, f"Discord event: {event.url}") if part]

    return {
        "summary": event.name,
        "description": "\n\n".join(description_parts),
        "location": location,
        # Aware UTC datetimes; Google renders them in each viewer's timezone.
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        # Tags the entry as ours so reconciliation can recognize it later.
        "extendedProperties": {"private": {"discord_event_id": str(event.id)}},
    }


def content_hash(event: discord.ScheduledEvent) -> str:
    """Fingerprint of everything we sync.

    Stored beside each mapping so the reconciliation loop (and duplicate
    gateway events) can skip API calls when nothing actually changed.
    """
    body_json = json.dumps(_to_gcal_body(event), sort_keys=True)
    return hashlib.sha256(body_json.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Calendar API operations (each wrapped in asyncio.to_thread)
# ---------------------------------------------------------------------------

async def upsert_event(
    event: discord.ScheduledEvent, calendar_id: str, gcal_event_id: str | None
) -> str:
    """Create or update one calendar entry; returns the Google event id.

    With a known id we patch; without one we insert. A patch that 404s
    (someone hand-deleted the entry) falls back to a fresh insert so the
    calendar heals instead of erroring forever.
    """
    body = _to_gcal_body(event)
    events_api = _get_service().events()

    def _call() -> dict:
        if gcal_event_id:
            try:
                return events_api.patch(
                    calendarId=calendar_id, eventId=gcal_event_id, body=body
                ).execute(num_retries=API_RETRIES)
            except HttpError as error:
                if error.resp.status not in (404, 410):
                    raise
                # Entry is gone - fall through and recreate it.
        return events_api.insert(calendarId=calendar_id, body=body).execute(
            num_retries=API_RETRIES
        )

    result = await asyncio.to_thread(_call)
    return result["id"]


async def delete_event(calendar_id: str, gcal_event_id: str) -> None:
    """Remove one calendar entry; 404/410 means it's already gone - fine."""
    events_api = _get_service().events()

    def _call() -> None:
        try:
            events_api.delete(calendarId=calendar_id, eventId=gcal_event_id).execute(
                num_retries=API_RETRIES
            )
        except HttpError as error:
            if error.resp.status not in (404, 410):
                raise

    await asyncio.to_thread(_call)


async def verify_write_access(calendar_id: str) -> str | None:
    """Probe whether we can write to a calendar. None = OK, else a reason.

    Inserting (and immediately deleting) a tiny test event is the only
    reliable probe: a calendar shared read-only would pass a metadata read
    but fail exactly when the first real event needs writing.
    """
    events_api = _get_service().events()

    def _call() -> None:
        now = datetime.now(timezone.utc)
        probe = {
            "summary": "Reminder Bot link test (auto-deleted)",
            "start": {"dateTime": now.isoformat()},
            "end": {"dateTime": (now + timedelta(minutes=1)).isoformat()},
        }
        created = events_api.insert(calendarId=calendar_id, body=probe).execute(
            num_retries=API_RETRIES
        )
        events_api.delete(calendarId=calendar_id, eventId=created["id"]).execute(
            num_retries=API_RETRIES
        )

    try:
        await asyncio.to_thread(_call)
        return None
    except HttpError as error:
        if error.resp.status == 404:
            return "calendar not found - double-check the Calendar ID"
        if error.resp.status == 403:
            return "no write access - the share must be 'Make changes to events'"
        logger.exception("Calendar: unexpected error verifying %s", calendar_id)
        return f"Google Calendar error {error.resp.status}"
    except OSError:
        # Network trouble that outlived the retries - this runs inside a slash
        # command, so return a reason instead of blowing up the interaction.
        logger.exception("Calendar: network error verifying %s", calendar_id)
        return "couldn't reach Google Calendar - try again in a moment"
