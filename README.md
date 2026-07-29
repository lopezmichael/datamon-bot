# Datamon Bot

Discord bot for [DigiLab](https://app.digilab.cards) — Digimon TCG Tournament Tracker.

Handles role sync, slash commands, react-to-resolve, thread lifecycle automation, and new-thread instructions for the DigiLab Discord server.

## Prerequisites

- Python 3.12+
- Discord bot token with **Server Members** privileged intent enabled
- Access to the shared Neon PostgreSQL database

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # Fill in all values
python bot.py
```

## Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/admins [scene]` | Anyone | View admins for a scene |
| `/roster [scene]` | Scene Admin+ | View stores & tournaments |
| `/requests` | Scene Admin+ | Open request summary |
| `/mystats` | Scene Admin+ | Personal resolution stats |
| `/scene [scene]` | Anyone | View scene info and stats |
| `/help` | Anyone | Show bot commands and info |

## Features

- **Role Sync** (5 min loop) — Syncs Discord roles with DB admin roles. Adds/removes Platform Admin, Regional Admin, and Scene Admin roles. Logs changes to `#bot-log`.
- **React-to-Resolve** — React ✅ on a forum thread's first message to resolve the request. Enforces scene-level permissions. Adds the appropriate tag (Resolved/Onboarded/Fixed/Shipped) and strips status tags (New, Under Review, etc.).
- **Thread Watcher** — Posts channel-specific instructions, tags relevant admins, and auto-applies "New" tag when threads are created in tracked forum channels.
- **Auto-Archive** (1 hr loop) — Archives resolved threads after 48 hours of inactivity. Archives "Won't Fix", "Not Planned", and "On Hold" threads after 1 week.
- **Stale Nudges** (daily) — Reminds admins about unresolved threads in `#bug-reports` and `#scene-requests` with no activity for 3+ days.
- **Scene Health Digest** (weekly, Mondays 09:00 UTC) — Posts to `#admin-digest` (via webhook) summarizing dormant scenes (no tournaments in 60+ days), scenes with no assigned admin, and recently deactivated stores. Mentions relevant admins per scene. Skips the post if everything is healthy.
- **Startup Config Check** — On boot, verifies every forum channel ID resolves to a real forum and every configured tag ID exists in that forum. Mismatches are logged and posted to `#bot-log`; the bot keeps running. Without it a wrong tag ID fails silently forever (threads never archive, nudges never stop).

## Request Flow

Where each request type goes, as of the web app's Phase 2 deploy (2026-07-29):

| Request type | Where it lands | Bot's role |
|--------------|---------------|-----------|
| Data error | Web admin UI + daily `#admin-digest` post | None — that channel isn't a tracked forum |
| Store request | Web admin UI + daily `#admin-digest` post | None |
| Scene request | `#scene-requests` thread | Instructions, admin mentions, nudges, resolve, archive |
| Bug report | `#bug-reports` thread | Instructions, admin mentions, nudges, resolve, archive |
| Feature request | `#feature-requests` (manual posts for now) | Resolve, archive |

`#scene-coordination` was retired after its legacy threads drained — the bot no longer watches
it and the channel has been deleted from the server.

## Architecture

```
bot.py              Entry point, Bot subclass, lifecycle
config.py           Env var loading, constants, ROLE_MAP
db.py               asyncpg pool + query helpers
messages.py         Message templates for forum threads
cogs/
  role_sync.py      Periodic role sync task
  commands.py       Slash commands (/admins, /roster, /requests, /mystats, /scene, /help)
  reactions.py      React-to-resolve handler
  thread_watcher.py Post instructions + auto-tag New on forum threads
  archiver.py       Auto-archive stale threads
  nudge.py          Stale thread nudges (3-day threshold)
  digest.py         Weekly scene health digest
```

## Deployment

Production runs on [Railway](https://railway.app) (project `datamon-bot`, service `datamon-bot`,
environment `production`), connected to the `lopezmichael/datamon-bot` GitHub repo — **pushing to
`main` auto-deploys**. There is no server to SSH into.

```bash
# One-time setup on a new machine
brew install railway
railway login
railway link          # pick the datamon-bot project/service

# Day to day
railway status        # deploy state
railway logs          # stdout logs (retention is short — about a week)
railway logs --lines 5000 | grep -i <term>
```

Environment variables are managed in the Railway dashboard (Variables tab), mirroring
`.env.example`.

## Forum Channel Mapping

| Channel | ✅ Resolve Tag | Status Tags Stripped | Archive-after-1-week Tag |
|---------|---------------|---------------------|------------------------|
| `#scene-requests` | Onboarded | New, Needs More Info, Needs Admin | On Hold |
| `#bug-reports` | Fixed | New, Under Review, Confirmed | Won't Fix |
| `#feature-requests` | Shipped | New, Planned | Not Planned |
