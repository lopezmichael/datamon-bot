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
| `/scene [scene]` | Anyone | View scene info and stats |
| `/help` | Anyone | Show bot commands and info |

## Features

- **Role Sync** (5 min loop) — Syncs Discord roles with DB admin roles. Adds/removes Platform Admin, Regional Admin, and Scene Admin roles. Logs changes to `#bot-log`.
- **React-to-Resolve** — React ✅ on a forum thread's first message to resolve the request. Enforces scene-level permissions. Adds the appropriate tag (Resolved/Onboarded/Fixed/Shipped).
- **Thread Watcher** — Posts resolve instructions and tags relevant admins when new threads are created in tracked forum channels.
- **Auto-Archive** (1 hr loop) — Archives resolved threads that have been inactive for 48+ hours.

## Architecture

```
bot.py              Entry point, Bot subclass, lifecycle
config.py           Env var loading, constants, ROLE_MAP
db.py               asyncpg pool + query helpers
cogs/
  role_sync.py      Periodic role sync task
  commands.py       Slash commands (/admins, /roster, /scene, /help)
  reactions.py      React-to-resolve handler
  thread_watcher.py Post instructions on new forum threads
  archiver.py       Auto-archive stale threads
```

## Deployment

```bash
# Copy files to server
scp -r . datamon@server:/opt/datamon-bot/

# Install and enable service
sudo cp systemd/datamon.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now datamon
sudo journalctl -u datamon -f
```

## Forum Channel Mapping

| Channel | ✅ Action | Tag Added |
|---------|-----------|-----------|
| `#scene-coordination` | Resolve request | Resolved |
| `#scene-requests` | Mark onboarded | Onboarded |
| `#bug-reports` | Mark fixed | Fixed |
| `#feature-requests` | Mark shipped | Shipped |
