# Reminder Bot ⏰💰

A private assistant bot for your Discord server that:

- **Tracks debts (utang)** between you and your friends — who owes whom, how much,
  what for, and whether it's been paid — by *watching the chat* (Tagalog, English,
  or Taglish) and by manual commands.
- **Manages reminders (paalala)** — detected from chat ("paalala bukas 7pm practice",
  "wag kalimutan magbayad sa sabado"), asked for directly ("@bot remind me to call
  mom bukas 5pm"), or set manually — and delivers them on time.
- **Mirrors Discord events to Google Calendar** — scheduled events in your server
  land on your Google Calendar, and your friends can link their own with
  `/calendar link`. Edits and cancellations in Discord follow automatically.
  (This part needs no confirmations — server events are explicit, structured data.)
- **Chats back when you @mention it** — ask it anything and it answers right in the
  channel, with a bit of personality. It ignores `@everyone`, can be muted per
  channel, and can run on its own API key so chatting never eats the detection quota.
  Ask it for a reminder and it just sets one.
- **Never records a debt on its own.** Every debt it overhears is sent to you as a DM
  with **Confirm / Ignore** buttons. You are always the final judge of the ledger.

Runs 24/7 on a Raspberry Pi Zero 2 W (even underclocked to one core) at
**$0/month** using Google's Gemini free tier.

> ⚠️ **Tell your friends the bot exists.** Messages that look debt/reminder-related
> (plus a little surrounding context) are sent to Google's Gemini API, and Google's
> free tier may use that content to improve their models. The same applies to any
> message that **@mentions the bot**, since answering it means sending it (and the
> recent conversation) to Gemini too. It's casual chat, but people deserve to know.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Examples it understands](#examples-it-understands)
3. [Commands](#commands)
4. [User Memory System](#user-memory-system)
5. [Settings reference](#settings-reference)
6. [Setup guide](#setup-guide)
7. [Running on the Raspberry Pi](#running-on-the-raspberry-pi)
8. [Day-2 operations](#day-2-operations)
9. [Troubleshooting](#troubleshooting)
10. [Project structure](#project-structure)
11. [Limitations](#limitations)

---

## How it works

```
every message the bot overhears
     │
     ▼
① regex prescan (free, on the Pi, Tagalog + English keywords)
     │  no match & no hot window → ignored (99% of chat, zero cost)
     ▼
② scope gate   debt talk must involve YOU · reminders can come from anyone
     ▼
③ one Gemini call   message + last few messages as context
     │  → debt event / reminder / update / none  + all details
     ▼
④ duplicate check   same person+amount? same reminder? → warns you
     ▼
⑤ DM to you   Confirm / Ignore buttons  ← nothing is saved without this
     ▼
⑥ SQLite ledger  →  scheduled nags & reminders
```

A message that **@mentions the bot** runs the same ① and ③, but it is a request
rather than something overheard, so it is treated differently at the end:

```
@mention
     │
     ├─ nothing detected              → it just chats back
     ├─ a reminder, with a date       → saved right away, confirmed in the channel
     ├─ a reminder, no date given     → it asks you when
     └─ a debt / a correction         → Confirm / Ignore DM to you, as usual
                                        (the channel only sees ordinary banter)
```

**Hot windows:** after a detection, messages from the same people in that channel
are AI-checked for a few more minutes *even without keywords* — so corrections like
*"next month nalang pala"* or *"500 pala hindi 300"* are caught and offered to you
as **updates** to the existing record (with an audit trail of old → new values).

**Chatting:** when you **@mention** the bot (or reply to one of its messages), it first
checks whether you're actually *asking* it for something. **A reminder is set on the
spot** and confirmed in the channel — it belongs to whoever asked, exactly like
`/remind add`, which anyone can already use. No date in the request? It asks you when.
Everything else is just conversation: it answers in the channel and stops there.

A debt mentioned this way still goes to your **Confirm / Ignore** DM and is *never*
acknowledged out loud — the bot doesn't advertise that it keeps a ledger, so the
channel only ever sees an ordinary reply.

Because a mention is addressed to the bot, and the bot stands in for you, the scope
gate is skipped there: *"@bot utang ko sayo 500"* counts even though your name never
appears. `@everyone`/`@here` is still deliberately ignored, so announcements don't get
a reply. Chat can run on its own API key, its own model, and its own daily cap, so it
never competes with detection — though a mention that turns out to be a real request
spends a *detection* call, not a chat one. Replies can't ping anyone: even if someone
talks the bot into typing "@everyone", nobody gets notified.

**Cost control:** the prescan + scope gate keep AI calls to a handful per day.
A configurable daily cap (default 200, vs. Gemini's ~1,500/day free allowance)
plus quota-error detection mean you get a DM if detection ever pauses — it never
fails silently, and commands/reminders keep working without AI.

## Examples it understands

| Someone types | The bot offers |
|---|---|
| "pautang naman, 500 lang, babayaran kita sa biyernes" | New debt: they owe you ₱500, promised Friday |
| "@matthew utang mo pala ako ng 250 sa jersey" | New debt: you owe them ₱250 (jersey) |
| "binayaran na kita kanina, quits na tayo" | Payment: settles their open debt |
| "babayaran kita sa kinsenas" | Promise date → the 15th |
| "next month nalang pala" *(right after the above)* | Update: moves the pay date |
| "paalala bukas 7pm practice tayo" | Reminder tomorrow 19:00 |
| "wag kalimutan magbayad ng jersey sa sabado" | Reminder Saturday |
| "linisin natin every monday" | **Repeating** reminder, every Monday |
| "araw-araw mag-inom ng gamot 8am" | **Repeating** reminder, daily 08:00 |
| "tuwing kinsenas bayad sa kuryente" | **Repeating** reminder, monthly on the 15th |
| "@bot remind me to call mom bukas 5pm" | Reminder set **immediately**, confirmed in the channel |
| "@bot paalalahanan mo ko" *(no date)* | It asks you when — nothing is saved |
| "utang na loob, tulungan mo naman ako" | Nothing — figurative, not money |
| "bente lang utang ko sayo" | New debt: ₱20 (colloquial number understood) |

## Commands

All commands are **owner-only** and replies are **ephemeral** (only the caller
sees them) — except `/calendar` and `/help`, which any member can use.

| Command | What it does |
|---|---|
| `/help` | **Anyone:** every command and what it does. Built from the live command list, so it's never out of date — non-owners only see the commands they can actually use. |
| `/debt add person amount [description] [currency] [due]` | Record money someone owes **you**. `person` = @mention **or any typed name** — that's how you record debts made outside Discord. |
| `/debt iou person amount [description] [currency] [due]` | Record money **you** owe someone. |
| `/debt paid person [all_debts]` | Settle their most recent open debt (or all of them). |
| `/debt list [include_paid]` | The ledger, grouped, with per-currency totals. |
| `/remind add text when [repeat] [force]` | **Anyone:** set a reminder for yourself. `when` = `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` (24h) — the *first* time it fires. `repeat` = daily / weekly / monthly / yearly, or just once (default). `force: True` bypasses the duplicate warning. *(Or skip the command: **@mention the bot** and ask in plain Taglish — "@bot remind me to call mom bukas 5pm".)* |
| `/remind list` | **Anyone:** your pending reminders (🔁 marks repeating ones). The owner sees everyone's. |
| `/remind delete reminder_id` | **Anyone:** remove one of your own. The owner can remove any. |
| `/settings reminders` | Choose delivery: **DM me only** (test mode, default) or **post in the server** mentioning the debtor/requester. |
| `/settings calendar` | Pause/resume Google Calendar sync (owner-only kill switch). |
| `/settings status activity [text]` | Set the bot's presence, e.g. **Playing DOOM**, **Watching the ledger**, custom text, or clear it. Survives restarts. |
| `/settings chatbot show` | Current chat settings: on/off, model, whether it's on its own API key, daily cap, cooldown, muted channels. |
| `/settings chatbot toggle state` | Turn chat replies on or off everywhere. Survives restarts. |
| `/settings chatbot cooldown seconds` | How long each person waits between pings (0–3600). `0` = no cooldown (default). |
| `/settings chatbot mute [channel]` | Stop chat replies in a channel (defaults to the current one). Detection and reminders there are unaffected. |
| `/settings chatbot unmute [channel]` | Allow chat replies there again. |
| `/calendar link calendar_id` | **Anyone:** sync this server's events to their own Google Calendar. Share the calendar with the bot's service account first — `/calendar status` shows how. |
| `/calendar unlink` | **Anyone:** stop syncing and remove the bot-added events from their calendar. |
| `/calendar status` | **Anyone:** their link status, plus step-by-step setup help. |

---

## User Memory System

The bot remembers user preferences and personal context across conversations using a **hybrid approach** that combines pattern matching (fast, zero tokens) with AI evaluation (smart, minimal tokens).

### What It Remembers

**Nicknames (instant, zero tokens):**
- `@bot call @Doc as DOY`
- `@bot gusto ko tawag mo kay @Doc lagi ay DOY`
- `@bot forget about calling @Doc`

**Personal info (automatic, hybrid):**
- Portfolio links: `@bot tandaan mo portfolio ko https://devsugoi.github.io/`
- Work/role: Detects when you mention "I'm a software engineer" or "data scientist ako"
- Preferences: "I prefer Tagalog", "gusto ko Python"
- Any explicit "remember this" or "tandaan mo" requests

### How It Works

**Pattern matching (0 tokens):**
Most common cases are caught instantly by patterns—portfolio links with "tandaan mo", work mentions like "I'm a...", nicknames, etc.

**AI backup (~50-150 tokens):**
When patterns don't match, the AI evaluates whether the conversation contains information worth remembering or if a question needs memory context to answer properly.

### Examples

```
User: @bot tandaan mo portfolio ko https://devsugoi.github.io/
Bot: Noted!
[Saved instantly, 0 tokens]

Later:
User: @bot anong portfolio ko?
[Loads memory, 0 tokens (pattern matched)]
Bot: Your portfolio is https://devsugoi.github.io/

---

User: @bot I work as a data scientist
Bot: Nice to meet you!
[Saved automatically, 0 tokens (pattern matched)]

Later:
User: @bot what do I do for work?
[AI checks if memory needed: ~50 tokens]
Bot: You work as a data scientist!
```

### Token Usage

- **85% of conversations**: 0 tokens (casual chat + pattern matches)
- **15% of conversations**: AI backup (~50-150 tokens per evaluation)
- **Daily estimate**: ~2,750 tokens (well within Gemini free tier)

### Technical Details

- **Hybrid intelligence:** Patterns catch common cases (0 tokens), AI catches creative phrasings
- **Persistent:** Memories survive bot restarts
- **User-specific:** Each user's memories are isolated
- **Smart loading:** Only loads when the conversation needs it
- **Smart saving:** Only saves truly valuable information
- **View memories:** `python view_memory.py` (on the Pi)

Memories are stored in the `user_memory` table in SQLite. The system automatically extracts meaningful context from conversations without requiring explicit commands (though explicit "tandaan mo" or "remember" commands work too).

---

## Settings reference

All settings live in `.env` (copy `.env.example` and fill it in):

| Setting | Default | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | — | Bot token from the Discord developer portal. **Required.** |
| `OWNER_ID` | — | Your Discord user ID. **Required.** |
| `GEMINI_API_KEY` | — | From Google AI Studio (free). **Required.** |
| `GEMINI_API_KEY_2`, `_3`, … | empty | Backup keys, up to `_20`. When one runs out of quota the bot switches to the next automatically and parks the spent one for an hour. Keys from *different* Google accounts get separate quotas. |
| `GUILD_ID` | empty | Your server ID → slash commands appear instantly. Empty = global sync (up to 1 h). |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | AI model for detection. Fast, cheap, and reliably available. `gemini-3.5-flash` is stronger but frequently returns 503 (overloaded); the `gemini-2.5-*` models are retired (404). |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.1-flash-lite` | Backup model used automatically when the one above is overloaded or retired. Empty disables the fallback. |
| `CONFIDENCE_THRESHOLD` | `0.6` | Detections below this confidence are silently dropped. |
| `MAX_AI_CALLS_PER_DAY` | `200` | Internal safety cap for detection; you get a DM if it's reached. |
| `CHATBOT_ENABLED` | `true` | Reply when @mentioned. Starting value only — `/settings chatbot toggle` overrides it. |
| `CHATBOT_API_KEY` | empty | Separate Gemini key for chat, so chatting doesn't spend the detection key's quota. Empty = reuse `GEMINI_API_KEY` and its backups. |
| `CHATBOT_API_KEY_2`, `_3`, … | empty | Backup chat keys, same rule as above. |
| `CHATBOT_MODEL` | empty | Model for chat replies — a cheaper/faster one fits well here. Empty = reuse `GEMINI_MODEL`. |
| `CHATBOT_FALLBACK_MODEL` | empty | Backup model for chat replies. Empty = reuse `GEMINI_FALLBACK_MODEL`. |
| `WEB_SEARCH_PROVIDER` | empty | Optional live web search provider for conversational @mention lookup questions. Supported: `serpapi`, `google_cse`. |
| `WEB_SEARCH_API_KEY` | empty | API key for the configured web search provider. |
| `WEB_SEARCH_ENGINE_ID` | empty | Google Custom Search Engine ID (`cx`) when using `google_cse`. |
| `WEB_SEARCH_RESULT_COUNT` | `3` | How many search results to include in the prompt. Keep this small. |
| `CHATBOT_MAX_CALLS_PER_DAY` | `200` | Daily cap for chat replies, separate from `MAX_AI_CALLS_PER_DAY` so chatter can't starve detection. |
| `CHATBOT_COOLDOWN_SECONDS` | `0` | Seconds between pings per person; `0` = off. Starting value only — `/settings chatbot cooldown` overrides it. |
| `CONTEXT_MESSAGES` | `8` | Recent messages sent as context with each AI call. `0` disables. |
| `HOT_WINDOW_MINUTES` | `10` | How long keyword-less follow-ups stay AI-checked. `0` disables. |
| `MAX_FOLLOWUPS_PER_WINDOW` | `10` | Cap on keyword-less checks per window. |
| `DEFAULT_CURRENCY` | `₱` | Assumed when amounts have no symbol. |
| `DEFAULT_REMINDER_HOUR` | `9` | Delivery hour for date-only reminders. |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every detection decision. |
| `DB_PATH` | `debts.db` | Where the SQLite file lives. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `service_account.json` | Google service-account key file. The file *existing* is what switches calendar sync on. |
| `GOOGLE_CALENDAR_ID` | empty | Your own calendar's ID (optional — `/calendar link` works for you too). |
| `CALENDAR_GUILD_IDS` | empty | Comma-separated server IDs whose events sync. Empty = the `GUILD_ID` server. |
| `CALENDAR_DEFAULT_EVENT_HOURS` | `1` | Assumed length of Discord events that have no end time. |

## Setup guide

### Step 1 — Create the Discord bot and get the token

1. Go to <https://discord.com/developers/applications> → **New Application** →
   name it (e.g. "Reminder Bot") → **Create**.
2. Left sidebar → **Bot**.
3. Scroll to **Privileged Gateway Intents** → turn **ON** `MESSAGE CONTENT INTENT`
   → **Save Changes**. *(Without this the bot cannot read chat — detection won't work.)*
4. At the top of the Bot page → **Reset Token** → copy the token → this is your
   `DISCORD_TOKEN`. Treat it like a password; anyone with it controls your bot.

### Step 2 — Get your own user ID

1. Discord → **User Settings → Advanced** → enable **Developer Mode**.
2. Right-click your own name anywhere → **Copy User ID** → this is `OWNER_ID`.
3. While you're there: right-click your **server's name** → **Copy Server ID** →
   this is `GUILD_ID` (recommended, makes commands appear instantly).

### Step 3 — Invite the bot to your server

1. Developer portal → your app → **OAuth2 → URL Generator**.
2. Scopes: check `bot` **and** `applications.commands`.
3. Bot permissions: check **View Channels**, **Send Messages**, **Read Message History**.
4. Copy the generated URL, open it in your browser, pick your server, **Authorize**.

(The equivalent direct URL, with `YOUR_CLIENT_ID` from the OAuth2 page:
`https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=68608&scope=bot%20applications.commands`)

### Step 4 — Get the free Gemini API key

1. Go to <https://aistudio.google.com> and sign in with a Google account.
2. Click **Get API key** → **Create API key** → copy it → this is `GEMINI_API_KEY`.
3. No credit card needed. Free-tier limits (~1,500 requests/day) are far above
   what this bot uses.

**Optional — a second key for chatting.** The bot can answer @mentions on a
different key so casual chat never spends the quota that debt/reminder detection
depends on. Create another API key (a second key in AI Studio, or one from a
different Google account for a fully independent quota) and put it in
`CHATBOT_API_KEY`. Leave it empty and both share `GEMINI_API_KEY` — the daily
caps stay separate either way.

### Optional web search setup

The bot can use live web search for factual @mention questions like
"when is hoyofest2026?". This is optional and only works when you set up one of
these providers in `.env`:

- `WEB_SEARCH_PROVIDER=serpapi`
  - Get a key at <https://serpapi.com/>.
  - Set `WEB_SEARCH_API_KEY` to that key.
  - Leave `WEB_SEARCH_ENGINE_ID` empty.
- `WEB_SEARCH_PROVIDER=google_cse`
  - Create a Google Custom Search Engine at
    <https://cse.google.com/cse/all>.
  - Add one or more sites or choose to search the whole web.
  - Copy the `cx` value and set `WEB_SEARCH_ENGINE_ID`.
  - Set `WEB_SEARCH_API_KEY` to a Google API key with Custom Search enabled.

Keep `WEB_SEARCH_RESULT_COUNT` small (3 is a good default). If `WEB_SEARCH_PROVIDER`
is empty, the bot will still answer @mentions normally using Gemini chat.

### Step 5 — Configure and test-run on your PC first

```bash
cd discord-reminder-bot
copy .env.example .env        # (macOS/Linux: cp .env.example .env)
# edit .env - fill in DISCORD_TOKEN, OWNER_ID, GEMINI_API_KEY, GUILD_ID

python -m venv venv
venv\Scripts\activate         # (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python bot.py
```

You should see `Logged in as …` in the console. Now, in your server:

- Type `/debt` — the commands should appear.
- Run `/help` — every command, with the ones you can use.
- **@mention the bot** and ask it something ("when is Christmas?") — it should
  answer in the channel within a few seconds.
- **@mention it and ask for a reminder** ("remind me to call mom bukas 5pm") — it
  should confirm with a `⏰` line in the channel, and `/remind list` should show it.
- Have a friend (or an alt account) type *"utang ko muna 100, bayaran kita bukas"* —
  you should get a DM with Confirm/Ignore within a few seconds.
- `/remind add text:test when:2026-01-01 12:00` then `/remind list` to see it.

Testing is safe by default: reminders are in **DM-only mode** until you run
`/settings reminders` and switch to server mode.

### Step 6 — (Optional) Google Calendar sync

One free Google Cloud *service account* lets the bot write to any calendar
shared with it — yours and your friends'. No browser logins, nothing expires.

1. Go to <https://console.cloud.google.com> → create (or pick) a project →
   **APIs & Services → Library** → enable the **Google Calendar API**.
2. **IAM & Admin → Service Accounts** → **Create service account** (any name,
   no roles needed) → open it → **Keys → Add key → Create new key → JSON**.
   Save the downloaded file as `service_account.json` in this folder.
   ⚠️ Treat it like a password (it's already in `.gitignore`).
3. Restart the bot. Now anyone — you included — connects their calendar like so:
   - Google Calendar → hover the calendar → ⋮ → **Settings and sharing** →
     **Share with specific people** → add the service account's email
     (`/calendar status` in Discord shows it) with **Make changes to events**.
   - Copy the **Calendar ID** from further down that same settings page.
   - In Discord: `/calendar link calendar_id:<paste it>`.

From then on, scheduled events created in your server appear in every linked
calendar within seconds, and edits/cancellations follow automatically. A
reconciliation pass at startup and every 6 hours catches anything that
happened while the bot was offline.

## Running on the Raspberry Pi

Works on a Pi Zero 2 W, including underclocked/single-core setups — the bot is a
single process that idles at ~60–80 MB RAM.

> ⚠️ **Do not copy `venv/` to the Pi.** It contains Windows/x86 binaries that
> cannot run on the Pi's ARM CPU, and it's ~180 MB. Create a fresh one there
> (step 3). Same for `__pycache__/`. Do copy `.env`, and copy `debts.db` only if
> you want to keep your existing ledger.

```bash
# 1. Use Raspberry Pi OS Lite (no desktop). Set the correct timezone -
#    reminders use local time:
sudo raspi-config      # Localisation Options -> Timezone -> Asia/Manila

# 2. Copy this folder to the Pi (from your PC), skipping the junk:
#    rsync -av --exclude venv --exclude __pycache__ --exclude '*.pyc' \
#          discord-reminder-bot/ pi@raspberrypi.local:/home/pi/discord-reminder-bot/
#    (no rsync on Windows? see the scp commands in the README section below)

# 3. On the Pi - install dependencies (piwheels provides prebuilt ARM
#    wheels, so this is download-only, no compiling):
cd /home/pi/discord-reminder-bot
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 4. Make sure .env is filled in (copy it over or create it here).

# 5. Quick manual test, then Ctrl+C:
venv/bin/python bot.py

# 6. Install as a service (auto-start on boot, auto-restart on crash):
sudo cp reminderbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now reminderbot

# 7. Confirm it's alive:
systemctl status reminderbot
journalctl -u reminderbot -f      # live logs (Ctrl+C to stop watching)
```

If your Pi username isn't `pi` or the folder lives elsewhere, edit the `User=`,
`WorkingDirectory=`, and `ExecStart=` lines in `reminderbot.service` first.

## Day-2 operations

| Task | How |
|---|---|
| Watch what the bot is doing | `journalctl -u reminderbot -f` |
| See *why* it did/didn't react to a message | Set `LOG_LEVEL=DEBUG` in `.env`, then `sudo systemctl restart reminderbot` |
| Back up the ledger | Copy `debts.db` somewhere safe (it's a single file). `scp pi@raspberrypi.local:/home/pi/discord-reminder-bot/debts.db .` |
| Update the bot's code | Copy the new files over, then `sudo systemctl restart reminderbot` |
| Update dependencies | `venv/bin/pip install -U -r requirements.txt`, then restart |
| Change any setting | Edit `.env`, then `sudo systemctl restart reminderbot` |
| Stop the bot | `sudo systemctl stop reminderbot` (and `disable` to keep it off after reboots) |

## Troubleshooting

**Slash commands don't appear.**
Set `GUILD_ID` in `.env` and restart — global sync without it can take up to an
hour. Also confirm the invite used the `applications.commands` scope (re-invite
with the Step 3 URL if unsure).

**The bot never reacts to chat.**
1. Check `MESSAGE CONTENT INTENT` is ON in the developer portal (Step 1.3).
2. Set `LOG_LEVEL=DEBUG` and watch the logs while sending a test message like
   "utang ko sayo 100" — you'll see exactly where it stops (prescan, scope gate,
   confidence, …).
3. Debt talk between two *other* people is ignored by design — you must be involved.

**No DMs arrive.**
Your privacy settings block DMs from server members. Server Settings → Privacy →
allow direct messages, or check Settings → Privacy & Safety. The logs will show
`Cannot DM the owner`.

**The bot doesn't reply when I @mention it.**
1. Run `/settings chatbot show` — it may be switched off, or that channel muted
   (`/settings chatbot unmute`).
2. `@everyone`/`@here` is ignored **by design**, including when the bot is
   mentioned in the same message. Mention it on its own.
3. Check `MESSAGE CONTENT INTENT` is ON (Step 1.3) — without it the bot can't read
   what you asked.
4. The daily chat cap may be reached (you'd get a DM). Raise
   `CHATBOT_MAX_CALLS_PER_DAY`, or check `LOG_LEVEL=DEBUG` logs.
5. If replies come back as "my brain glitched", the chat key or model is wrong —
   check `CHATBOT_API_KEY` and `CHATBOT_MODEL` in `.env`.

**I asked the bot for a reminder and it just chatted back.**
1. It needs a day or time it can actually resolve. "bukas 5pm", "sa sabado",
   "every monday 8am" all work; "mamaya na" and "soon" don't — it will ask you when
   rather than guess. `/remind add` takes an exact time.
2. It must recognise the request as one: the free prescan looks for words like
   *remind / paalala / wag kalimutan / don't forget*. "@bot ping me later" won't trip
   it. Set `LOG_LEVEL=DEBUG` to see the decision.
3. Low confidence (jokes, vague wording) falls back to chatting. Lower
   `CONFIDENCE_THRESHOLD` if it's too shy.
4. This spends the **detection** budget, not the chat one — if `MAX_AI_CALLS_PER_DAY`
   is used up (you'd get a DM) the bot still chats, but stops setting reminders.

**"AI budget used up" / "quota exhausted" DM.**
Detection is paused until the daily reset (internal cap: midnight local; Gemini:
midnight US Pacific). Everything else keeps working. If it happens often, raise
`MAX_AI_CALLS_PER_DAY`, shrink `HOT_WINDOW_MINUTES`, or check DEBUG logs for
what's eating calls.

**Detections are wrong/silly.**
Raise `CONFIDENCE_THRESHOLD` (e.g. `0.75`). Remember you can always just press
Ignore — nothing is recorded without you.

**Gemini model errors (404 / model not found).**
Google occasionally retires models — the `gemini-2.5-*` family already returns 404.
Set `GEMINI_MODEL` in `.env` to a current one
(check <https://ai.google.dev/gemini-api/docs/models>) and restart.

**"My brain's lagging / fried" replies, or a DM saying the model keeps answering 503.**
*(In-channel wording is deliberately vague — the bot never mentions AI services,
quotas, or limits in front of your friends. The specifics only go to your DMs.)*
Google's backend for that model is overloaded — their side, not yours. The bot
handles this in three stages on its own: it retries twice with backoff, then
switches to `GEMINI_FALLBACK_MODEL` / `CHATBOT_FALLBACK_MODEL`, then remembers the
bad model for 5 minutes so later messages skip straight to the backup. You only see
an error if **both** models are down. If that happens, set `GEMINI_MODEL` and the
fallback to different models and restart — `gemini-3.5-flash` and
`gemini-flash-latest` are the usual offenders, `gemini-3.1-flash-lite` is reliable.
Note detection failures are otherwise **silent**, which is why the bot DMs you once
a day when this happens.

**`/calendar link` says it can't write to the calendar.**
The calendar isn't shared with the service account's email (shown by
`/calendar status`), or the share is view-only — it must be **Make changes to
events**. A "calendar not found" usually means a typo in the Calendar ID.

**Calendar events stopped syncing.**
Watch the logs for `Calendar:` lines. A 403 can mean the Calendar API was
disabled in the Cloud project. Transient Google errors heal on their own —
the reconciliation pass retries at startup and every 6 hours.

**The bot went offline.**
`systemctl status reminderbot` shows the last error; `journalctl -u reminderbot -n 100`
shows recent logs. The service auto-restarts on crashes; if it's flapping, the
error will be in the log (usually a bad token after a reset, or no network).

## Project structure

```
bot.py            Discord client, slash commands, confirm buttons, hot windows,
                  chatbot replies + reminders asked for by @mention, delivery
                  loop (reminders + debt nagging), calendar sync handlers +
                  reconcile loop, notifications
detection.py      Free bilingual regex prescan (Tagalog roots + English)
ai_parser.py      Two Gemini calls: a structured one for detection
                  (debt / reminder / update / none) and a free-form one for
                  chat replies, each on its own client so keys can differ
calendar_sync.py  Google Calendar client: service-account auth, event
                  translation, insert/patch/delete
db.py             SQLite: debts, reminders, settings, edit trail, calendar links
reminderbot.service   systemd unit for the Pi
.env.example      Documented template for every setting
```

## Limitations

- **Cannot see your private DMs** with other people — Discord platform boundary.
  Use `/debt add` / `/remind add` for those.
- **Pending Confirm/Ignore buttons don't survive a restart.** The ledger and
  reminders themselves are safe in SQLite; just re-add via command if one gets lost.
- **Detection is per-conversation, not psychic.** Sarcasm and vague banter are
  filtered by the confidence threshold, and you're the final filter via the buttons.
- **Chat has a short memory.** A reply only sees the last `CONTEXT_MESSAGES`
  messages in that channel — there's no long-term memory of past conversations,
  and it can't read the debt ledger or reminder list.
- **Recurring Discord events** sync as their next occurrence only — the repeat
  rule isn't copied to Google Calendar.
- **One linked calendar per Discord user** (re-linking replaces it), and everyone
  linked receives events from all synced servers.
- **Free-tier dependency.** If Google changes the free tier, detection may pause
  (you'll get a DM) — swap `GEMINI_MODEL` or the provider in `ai_parser.py`;
  the rest of the bot doesn't care.
