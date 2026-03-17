# Datamon Bot

Discord bot for DigiLab (Digimon TCG tournament platform). Coordinates ~30 scene admins across 6 continents. Handles role sync, slash commands, forum thread automation, and request resolution tracking.

**Companion repo:** `digilab-app` (the web app). This bot shares its Neon PostgreSQL database (read-only except for `admin_requests.status`).

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # Fill in all values
python bot.py
```

Requires Python 3.12+. All env vars are required — the bot fails fast on missing values.

## Project Structure

```
bot.py              # Entry point, DatamonBot subclass, lifecycle
config.py           # Env vars, ROLE_MAP, FORUM_CHANNELS
db.py               # asyncpg pool, all query helpers
utils.py            # Shared utilities (webhook logging)
cogs/
  role_sync.py      # 5-min loop: DB roles -> Discord roles
  commands.py       # /admins, /roster, /scene, /help
  reactions.py      # React-to-resolve on forum threads
  thread_watcher.py # Posts instructions on new forum threads
  archiver.py       # 1-hr loop: archives stale resolved threads
systemd/
  datamon.service   # Production systemd unit file
```

## Key Conventions

- **Python 3.12+**, async throughout (discord.py + asyncpg)
- All config via environment variables loaded in `config.py` — never hardcode IDs or secrets
- Database queries live in `db.py` — cogs call helpers, not raw SQL
- Bot is **read-only** on the database except for `UPDATE admin_requests SET status='resolved'`
- Discord rate limits: role_sync adds 1-second delays between role changes
- Logging goes to stdout via Python `logging` module; production uses journald

## Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/admins [scene]` | Public | View admins for a scene |
| `/roster [scene]` | Admin-only | Stores & tournament counts |
| `/scene [scene]` | Public | Scene info card |
| `/help` | Public | Bot features (ephemeral) |

## Testing

No automated test suite. Verification is manual against a live Discord server — see `NEXT_STEPS.md` for the checklist.

## Deployment

Production target: DigitalOcean droplet (Ubuntu, $6/mo). Managed via systemd (`systemd/datamon.service`). See `README.md` for full deployment steps.
