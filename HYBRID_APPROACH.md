# Server Context Memory

## Overview

Each Discord server gets **one compact context document** (~1500 characters max) that captures standing rules and durable facts. It is always injected into the chatbot system prompt and rewritten by the LLM when new standing instructions appear in chat.

## Flow

```
Chat reply sent
    ↓
[Heuristic gate] — skip obvious casual chat
    ↓ (maybe)
[LLM rewrite] — merge new info into compact bullet list
    ↓
server_context table (SQLite)
    ↓
Always loaded into system_instruction on next reply
```

## What gets saved

- Server-wide language / tone rules
- Standing nicknames ("call @user as NAME")
- Durable facts users share (portfolio, role, preferences) when framed as standing context
- Explicit "remember for this server" requests

## What does NOT get saved

- Casual banter ("haha ok thanks")
- One-off questions without durable answers
- Ephemeral or time-bound information

## Token usage

- **Load:** 0 extra tokens on gate miss — context is always in system prompt (bounded by 1500 chars)
- **Save:** Only when heuristic gate fires (~150–600 tokens for LLM rewrite)

## Migration

On first startup after upgrade, legacy `user_memory` and `guild_memory` rows are folded into `server_context` per guild, then the old tables are dropped. User notes without a guild column are copied into every known guild so nothing is lost.

## Commands

- `@bot show server memory` / `show server context`
- `@bot clear server memory` / `clear server context`

Inspect via CLI: `python view_memory.py [guild_id]`
