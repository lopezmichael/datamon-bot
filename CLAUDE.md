# Datamon Bot

Discord bot for DigiLab, a multi-game Bandai TCG tournament platform (Digimon and Gundam are live; One Piece, Fusion World and Union Arena exist as catalogue-only rows). Coordinates ~200 admins across 6 continents. Handles role sync, slash commands, forum thread automation, and request resolution tracking.

**Companion repo:** `digilab-app` (the web app). This bot shares its Neon PostgreSQL database (read-only except for `admin_requests.status`).

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # Fill in all values
python bot.py
```

Requires **Python 3.13+** — `requirements.txt` pins `audioop-lts` (discord.py needs the `audioop` module 3.13 removed), whose `Requires-Python` is `>=3.13`, so `pip install` fails on 3.12. The docs said 3.12+ until 2026-08-24 and were wrong. Nothing pins the interpreter for deploy — nixpacks picks it — so CI tests 3.13 and 3.14 to bracket the range.

All env vars are required — the bot fails fast on missing values.

## Project Structure

```
.github/workflows/  # CI: compile, import-check, tests
scripts/            # check_imports.py — loads every module the way boot does
bot.py              # Entry point, DatamonBot subclass, lifecycle
config.py           # Env vars, ROLE_MAP, FORUM_CHANNELS
db.py               # asyncpg pool, all query helpers, dead-connection retry
utils.py            # Webhook logging, LoopFailureAlerter, thread tag helpers
messages.py         # Message templates for forum thread responses (game-aware)
games.py            # Shared game cache on the bot: which games are live, what to call them
cogs/
  role_sync.py      # 5-min loop: DB roles -> Discord roles
  commands.py       # /admins, /roster, /scene, /requests, /mystats, /help
  reactions.py      # React-to-resolve on forum threads
  thread_watcher.py # Posts instructions + auto-tags New on forum threads
  archiver.py       # 1-hr loop: archives stale resolved threads
  nudge.py          # 24-hr loop: nudges stale unresolved threads (3 days)
  digest.py         # Weekly scene health digest (Mondays 09:00 UTC)
```

## Key Conventions

- **Python 3.13+**, async throughout (discord.py + asyncpg)
- All config via environment variables loaded in `config.py` — never hardcode IDs or secrets
- Database queries live in `db.py` — cogs call helpers, not raw SQL
- **Never read `games.is_active` to decide behaviour.** It is a divergent second answer to
  "is this game live" and it is wrong today: Gundam is `FALSE` while carrying 16 active
  scenes, 11 admin role rows and 344 tournaments. Reading it cost the bot a digest that
  silently skipped Gundam and an `/admins game:gundam` that answered "Unknown game", and
  cost digilab-web three weeks of missing badge refreshes (see its
  `docs/references/multi-game-debt.md` and the `conventions.test.ts` ratchet). **A game is
  live here if it has active scene coverage** — `db.get_live_games`. Reading the column to
  DISPLAY a name is fine (`db.get_game_labels`); reading it to decide anything is not
- **Name no game in code.** Every game name in an outgoing message arrives as an argument
  from `games.short_name` via `bot.games` (see `games.py`). Adding game #3 must be a row in
  the database plus one optional env var, never a string edit. `tests/test_game_copy.py`
  pins that the templates contain no game name
- Anything user-facing that reports per-game data (`tournaments.game_id`, `players.game_id`,
  the `store_games` / `scene_games` junctions) must either scope to one game or say plainly
  that it is blending them. A blended total under a game-branded header is how `/scene
  austin` came to report 168 tournaments for a scene with zero Digimon activity
- **Never call `pool.fetch` / `fetchrow` / `execute` directly**, in `db.py` or anywhere else. Go through `_fetch` / `_fetchrow` / `_run`, which retry when Neon has dropped the pooled connection out from under us. Anything routed through `_run` must be safe to run more than once
- Bot is **read-only** on the database except for `UPDATE admin_requests SET status='resolved'` — enforced at the DB level since 2026-07-29: the `datamon_bot` Postgres role has SELECT everywhere and column-level UPDATE on `admin_requests(status, resolved_at, resolved_by)` only
- Periodic loops report failures via `utils.LoopFailureAlerter`, not a hand-rolled flag. Construct it with a threshold matched to the loop's cadence: >1 for the 5-minute loops so a single blip stays quiet, 1 for hourly-and-slower where the next data point is far away
- Slash commands that query the DB must `defer()` before the first query — Discord rejects a first response after 3 seconds, and a Neon cold start can exceed that. `defer()` fixes the reply's visibility for the whole interaction, so it is only straightforward where every path shares one ephemerality
- Discord rate limits: role_sync adds 1-second delays between role changes
- The three **tier** roles (Platform/Regional/Scene) are the bot's to own — it is the only
  thing that grants them, so its reverse pass may remove them. The **per-game** roles
  (`DISCORD_GAME_ROLE_<GAMEID>`, optional) are NOT: Discord onboarding grants the same roles
  to members who self-select a game, so the bot grants them additively and never removes
  one. Keep them out of `DIGILAB_ROLE_IDS`
- Tier roles stay one flat namespace across games (role_sync grants the strongest role held
  in ANY game), so treat the Discord badge as cosmetic for authorization. `game_admin_roles`
  decides who may resolve what, per game — see `db.get_admin_access_for_user`
- Logging goes to stdout via Python `logging` module; Railway captures stdout (short retention, ~1 week). Anything you'd need for a postmortem has to reach `#bot-log`, not just stdout

## Commands

| Command | Access | Description |
|---------|--------|-------------|
| `/admins [scene] [game]` | Public | View admins for a scene (default: grouped by game) |
| `/roster [scene] [game]` | Admin-only | Stores & tournament counts (unscoped blends games, and says so) |
| `/requests` | Admin-only | Open request summary, grouped by game (ephemeral) |
| `/mystats` | Admin-only | Personal resolution stats (ephemeral) |
| `/scene [scene] [game]` | Public | Scene info card (default: per-game breakdown) |
| `/help` | Public | Bot features (ephemeral) |

## Testing

No pytest, no CI. Two kinds of standalone test file, both stdlib only:

- **Pure-function tests** over the pieces whose output nobody sees for a week — the digest's
  rendering, embed fitting, admin-access levels, the game-aware copy layer.
- **`tests/test_conventions.py`** — mechanical guards for the rules above whose violation is
  *silent*: reading `games.is_active` for behaviour, a game name hardcoded in an emitted
  string, a direct `pool.fetch` bypassing the retry wrappers. It scans string literals via
  the AST rather than grepping source, so the comments that argue these rules don't trip it.
  **Every check is mutation-verified.** If you change one, break the thing it guards and
  confirm it fails before trusting it — a ratchet that can't fail reads as coverage.

Run them with **`tests/run.py`**, never a shell loop:

```bash
.venv/bin/python tests/run.py          # every test file, truthful exit code
.venv/bin/python scripts/check_imports.py   # every module imports (needs a full .env)
```

The loop this replaced — `for t in tests/*.py; do python "$t"; done` — **could not
fail**: a `for` loop exits with the status of its LAST command, so a failure in any
earlier file returned 0. `tests/run.py` aggregates, and also fails a file that exits
clean having asserted nothing.

`scripts/check_imports.py` is separate because five of the seven cogs are imported by
no test at all, so a module-level error in them reaches production as a Railway crash
loop. It needs real env vars (it loads the modules the way boot does), which is exactly
why it is not in `tests/`.

## CI

`.github/workflows/ci.yml` runs compile → imports → tests on PRs into `main` and
`develop`, and on pushes to `develop`. **Railway auto-deploys `main`, so the
`develop → main` PR is the only gate before production** — it needs branch protection
requiring the `test` check, or CI is advisory.

Everything else is verified manually against a live Discord server — see `NEXT_STEPS.md`.

## Deployment

Production runs on **Railway** (project `datamon-bot`, connected to the GitHub repo) — pushing to `main` auto-deploys. Logs via `railway logs` (CLI is linked in this repo). See `README.md` for details.
