# Datamon Bot — Design Document

**Date:** 2026-03-15
**Status:** Approved
**Scope:** Discord bot for DigiLab — role sync, slash commands, thread automation, welcome DMs
**Repo:** `datamon-bot` (standalone, separate from `digilab-app`)

---

## Context

DigiLab is a tournament tracking app for the Digimon TCG community. The DigiLab Discord server serves as the coordination hub for ~30 scene admins across 6 continents. v1.7.3 shipped all app-side prerequisites:

- **Per-action forum threads** — webhooks create a new thread per store request / data error (not one persistent thread per scene)
- **@mentions** — threads tag relevant scene admins and regional admins from `admin_user_scenes` + `admin_regions` tables
- **`discord_thread_id`** on `admin_requests` — enables bidirectional sync between app and Discord
- **`admin_regions`** table — country/state assignments for regional admins
- **`discord_resolve_thread()`** — app-side function that posts resolution message + adds Resolved tag (requires bot token)
- **`discord_send_welcome_dm()`** — app-side function that DMs credentials to new admins (requires bot token)

The bot completes the loop: Discord → App sync, role management, slash commands for scene admins, and thread lifecycle automation. It reads from the same Neon PostgreSQL database as the app.

---

## Architecture

```
┌─────────────┐    webhooks (HTTP POST)    ┌──────────────────┐
│  digilab-app │ ───────────────────────── │  Discord Server  │
│  (R Shiny)   │                            │                  │
│              │    bot token REST API      │  #scene-coord    │
│              │ ───────────────────────── │  #scene-requests │
└──────┬───────┘                            │  #bug-reports    │
       │                                    └────────┬─────────┘
       │  shared                                     │
       │  Neon DB                                    │ gateway
       │                                             │ events
       ▼                                             ▼
┌──────────────┐                           ┌─────────────────┐
│   Neon       │ ◄──────────────────────── │  datamon-bot    │
│  PostgreSQL  │     read/write            │  (discord.py)   │
└──────────────┘                           └─────────────────┘
```

- **App → Discord**: Webhooks (fire-and-forget, no bot needed) + bot token REST calls for DMs/tag updates
- **Discord → App**: Bot listens to gateway events, writes to shared DB
- **Shared state**: Both app and bot connect to the same Neon PostgreSQL database

---

## Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Runtime | Python 3.12+ | discord.py ecosystem, easy deployment |
| Discord lib | discord.py 2.x | Mature, well-documented, async-native |
| Database | asyncpg → Neon PostgreSQL | Async Postgres driver, same DB as app |
| Hosting | DigitalOcean Droplet ($6/mo) | Simple, persistent process, SSH access |
| Process mgr | systemd | Auto-restart, logging, standard Linux |
| Config | `.env` + python-dotenv | Same pattern as digilab-app |

---

## Discord Server Reference

Full server structure documented in `digilab-app/docs/plans/2026-02-26-discord-server-design.md`. Key channels the bot interacts with:

| Channel | Type | Bot Interaction |
|---------|------|----------------|
| `#scene-coordination` | Forum | React-to-resolve, auto-archive, tag management |
| `#scene-requests` | Forum | Tag updates on onboarding |
| `#bug-reports` | Forum | Tag updates on fix |
| DMs | Direct Message | Welcome DM delivery |

---

## Database Contract

The bot shares Neon PostgreSQL with digilab-app. **Read-heavy, minimal writes.** The bot never modifies core app data (players, tournaments, results, ratings).

### Tables the Bot Reads

```sql
-- Admin users: role, discord ID, active status
SELECT user_id, username, discord_user_id, role, is_active
FROM admin_users;

-- Scene assignments (direct)
SELECT user_id, scene_id, is_primary
FROM admin_user_scenes;

-- Regional assignments
SELECT user_id, country, state_region
FROM admin_regions;

-- Scenes: for /scene command, roster lookups
SELECT scene_id, slug, display_name, country, state_region, continent, is_active
FROM scenes;

-- Stores: for /scene command detail
SELECT store_id, name, scene_id, is_active
FROM stores;

-- Admin requests: for react-to-resolve lookups
SELECT id, request_type, status, discord_thread_id, resolved_at, resolved_by
FROM admin_requests;
```

### Tables the Bot Writes

```sql
-- Update request status when resolved via Discord reaction
UPDATE admin_requests
SET status = 'resolved', resolved_at = NOW(), resolved_by = $1
WHERE discord_thread_id = $2 AND status = 'pending';
```

That's it. The bot's only write is resolving requests via reaction. All other state changes flow through the app.

---

## Bot Permissions (Intents & Scopes)

### Gateway Intents
- `GUILDS` — role/channel metadata
- `GUILD_MEMBERS` — role sync (privileged intent, must be enabled in Developer Portal)
- `GUILD_MESSAGE_REACTIONS` — react-to-resolve
- `MESSAGE_CONTENT` — not needed (slash commands only, no message parsing)

### OAuth2 Scopes
- `bot` — standard bot presence
- `applications.commands` — slash commands

### Bot Permissions (integer)
- Manage Roles — role sync
- Send Messages — thread resolution messages, welcome DMs
- Manage Threads — archive/unarchive, tag updates
- Read Message History — reaction context
- Use Slash Commands — implicit with `applications.commands` scope

---

## Features

### 1. Role Sync

**Purpose:** Keep Discord roles in sync with app-level admin roles.

**Roles to sync:**
| App Role | Discord Role | Color |
|----------|-------------|-------|
| `super_admin` | `Platform Admin` | Red |
| `regional_admin` | `Regional Admin` | Gold |
| `scene_admin` | `Scene Admin` | Green |

**Sync trigger:** Periodic poll every 5 minutes (cron-style with `discord.ext.tasks`).

**Logic:**
```
For each admin_user where is_active = TRUE and discord_user_id IS NOT NULL:
  1. Fetch Discord member by user ID
  2. Determine expected role from admin_users.role
  3. If member doesn't have expected role → add it
  4. If member has a DigiLab role they shouldn't → remove it

For each Discord member with a DigiLab role:
  1. Look up in admin_users by discord_user_id
  2. If not found or is_active = FALSE → remove all DigiLab roles
```

**Edge cases:**
- Bot's role must be ABOVE managed roles in Discord hierarchy
- If member left the server, skip silently
- Rate limit: Discord allows 10 role changes/10s per guild — batch with 1s delays

### 2. Slash Commands

All commands are guild-only (no DM usage).

#### `/admins [scene]`
**Who can use:** Anyone
**Description:** Show admins for a scene.

```
/admins dallas-fort-worth

📋 Admins for Dallas-Fort Worth
  🟢 @mike — Scene Admin (primary)
  🟡 @sarah — Regional Admin (Texas)
```

**Implementation:**
- Query `admin_user_scenes` + `admin_regions` joined to `admin_users` and `scenes`
- Autocomplete on scene slug/display_name
- Show direct assignments + inherited regional coverage

#### `/roster [scene]`
**Who can use:** Scene Admin+
**Description:** Show stores and tournament summary for a scene.

```
/roster dallas-fort-worth

🏪 Dallas-Fort Worth — 4 stores, 23 tournaments
  Common Ground Games — 12 events
  Madness Games & Comics — 8 events
  More Fun Game Center — 2 events
  Collected Lubbock — 1 event
```

**Implementation:**
- Query `stores` + `tournaments` joined by scene
- Permission check: caller must be scene_admin for that scene, regional_admin covering it, or super_admin
- Autocomplete on scene slug

#### `/scene [scene]`
**Who can use:** Anyone
**Description:** Scene info card with link to app.

```
/scene dallas-fort-worth

🌎 Dallas-Fort Worth
  📍 Texas, United States
  🏪 4 active stores
  🏆 23 tournaments tracked
  👥 45 players
  🔗 https://app.digilab.cards/?scene=dallas-fort-worth
```

**Implementation:**
- Query `scenes`, `stores`, `tournaments`, `players` (via materialized views if available)
- Embed with scene color/branding
- Deep link to app with `?scene=` parameter

#### `/help`
**Who can use:** Anyone
**Description:** Bot info and command list.

### 3. React-to-Resolve

**Purpose:** Scene admins resolve requests directly from Discord by reacting to the thread's first message.

**Trigger:** ✅ reaction on the first message of a `#scene-coordination` forum thread.

**Flow:**
```
1. User reacts ✅ on first message in a thread
2. Bot checks: is this a #scene-coordination thread?
3. Bot checks: does this thread have a matching admin_requests.discord_thread_id?
4. Bot checks: is the reactor a Scene Admin+ for the relevant scene?
5. If all pass:
   a. UPDATE admin_requests SET status = 'resolved', resolved_by = reactor_username, resolved_at = NOW()
   b. Add "Resolved" tag to the thread
   c. Post confirmation: "✅ Resolved by @user"
6. If permission check fails: DM the user explaining they need scene admin access
```

**Edge cases:**
- Ignore reactions on non-first messages
- Ignore reactions in channels other than #scene-coordination
- Ignore if request already resolved (idempotent)
- Remove reaction if unauthorized (feedback that it didn't work)

### 4. Auto-Archive Stale Threads

**Purpose:** Keep forum channels clean by archiving resolved threads.

**Logic:**
- Every hour, scan `#scene-coordination` threads
- If thread has "Resolved" tag AND last message is >48 hours old → archive
- Don't archive threads without "Resolved" tag (they may be waiting on action)

**Implementation:** `discord.ext.tasks` loop, PATCH channel with `archived: true`.

### 5. Welcome DM Delivery

**Purpose:** Deliver admin credentials to new scene admins via DM instead of manual copy-paste.

**This is already implemented app-side** in `discord_send_welcome_dm()` which uses the bot token. The bot just needs to be running with a valid token for this to work — no bot-side code needed beyond being online.

The app calls the Discord REST API directly with the bot token. The bot process doesn't intercept or handle these DMs.

**Fallback:** If DM delivery fails (user has DMs disabled), the app shows a copy-paste panel with the credentials.

---

## Project Structure

```
datamon-bot/
├── bot.py                  # Entry point — bot startup, extension loading
├── cogs/
│   ├── role_sync.py        # Periodic role sync task
│   ├── commands.py         # Slash commands (/admins, /roster, /scene, /help)
│   ├── reactions.py        # React-to-resolve handler
│   └── archiver.py         # Auto-archive stale threads
├── db.py                   # asyncpg connection pool, query helpers
├── config.py               # Environment variable loading, constants
├── requirements.txt        # discord.py, asyncpg, python-dotenv
├── .env.example            # Template for required env vars
├── systemd/
│   └── datamon.service     # systemd unit file for deployment
└── README.md               # Setup, deployment, commands reference
```

---

## Environment Variables

```bash
# Discord
DISCORD_BOT_TOKEN=           # Bot token from Developer Portal
DISCORD_GUILD_ID=            # Server ID (for guild-specific commands)

# Discord Role IDs (right-click role → Copy ID)
DISCORD_ROLE_PLATFORM_ADMIN=
DISCORD_ROLE_REGIONAL_ADMIN=
DISCORD_ROLE_SCENE_ADMIN=

# Discord Channel IDs
DISCORD_CHANNEL_SCENE_COORDINATION=   # Forum channel ID for react-to-resolve

# Discord Forum Tag IDs
DISCORD_TAG_RESOLVED=                 # Same value as in digilab-app .env

# Database (same Neon instance as digilab-app)
NEON_HOST=
NEON_DATABASE=
NEON_USER=
NEON_PASSWORD=
```

---

## Deployment

### DigitalOcean Droplet

```bash
# Ubuntu 24.04, $6/mo (1 vCPU, 1GB RAM)
# More than enough — bot uses <50MB RAM

# Setup
sudo apt update && sudo apt install python3.12 python3.12-venv
git clone <repo>
cd datamon-bot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in values

# Test
python bot.py

# Deploy as systemd service
sudo cp systemd/datamon.service /etc/systemd/system/
sudo systemctl enable datamon
sudo systemctl start datamon

# Logs
journalctl -u datamon -f
```

### systemd Unit File

```ini
[Unit]
Description=Datamon Discord Bot
After=network.target

[Service]
Type=simple
User=datamon
WorkingDirectory=/home/datamon/datamon-bot
ExecStart=/home/datamon/datamon-bot/.venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=/home/datamon/datamon-bot/.env

[Install]
WantedBy=multi-user.target
```

---

## Implementation Order

| Phase | Feature | Estimated Effort |
|-------|---------|-----------------|
| 1 | Scaffolding: bot.py, config, db pool, cog loading | Small |
| 2 | Slash commands: /admins, /roster, /scene, /help | Medium |
| 3 | Role sync (periodic task) | Medium |
| 4 | React-to-resolve | Medium |
| 5 | Auto-archive stale threads | Small |
| 6 | Deploy to DigitalOcean + systemd | Small |

Phase 1–2 first for immediate utility. Phase 3–4 for automation. Phase 5–6 for production.

---

## What the Bot Does NOT Do

- **No message parsing** — all interaction via slash commands and reactions
- **No tournament data modification** — read-only for players/tournaments/results
- **No rating calculations** — that's app-side only
- **No webhook sending** — the app handles all webhooks; the bot handles gateway events
- **No web dashboard** — it's a pure Discord bot, no HTTP server
- **No Carl-bot replacement** — Carl-bot continues to handle welcome messages and auto-moderation; the bot handles DigiLab-specific features only

---

## Monitoring

- **Systemd**: `systemctl status datamon`, auto-restart on crash
- **Logging**: Python `logging` module → stdout → journald
- **Health check**: Bot sets a custom status ("Watching N scenes") updated every 5 minutes alongside role sync
- **Error tracking**: Log errors to a `#bot-log` channel in the OPS category (simple webhook, not Sentry)
- **Uptime**: DigitalOcean monitoring alerts if droplet goes down
