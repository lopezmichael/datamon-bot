# Datamon Bot — Next Steps

## Current Status (2026-08-10)

- All bot code implemented, bug-reviewed, and compiles cleanly
- Bot connects to Discord as **Datamon#4349**, all 7 cogs load, command tree synced
- **84/86** admin `discord_user_id` values linked in DB (2 not in server: aomceodeadly, gamescornerdigimon)
- Role sync verified: all Discord roles match DB state
- DB migrations applied: `discord_thread_id` on `admin_requests`, `admin_regions` table created
- Existing requests have NULL `discord_thread_id` — react-to-resolve only works on new requests
- All **three** tracked forum channels have consistent tag behavior (resolve tags, status
  stripping, auto-archive). `#scene-coordination` was retired in PR 3, so a healthy boot logs
  `Forum config OK — 3 channels` — the earlier note expecting 4 predated that change
- `resolved_by` now stores Discord user ID (not display name) — old records unaffected

### Connection resilience + alerting pass (2026-08-10)

Deployed and confirmed booting clean (all 7 cogs, pool on first attempt, forum config OK).

- Neon was dropping idle connections; the pool served the dead connection and the 5-minute loops
  alerted repeatedly. Query helpers now retry, and `command_timeout` bounds a blackholed socket
- `/requests` and `/mystats` had been raising `UndefinedColumnError` against the live database —
  the column is `submitted_at`, not `created_at`. Both were broken for admins for some time,
  unrelated to the connection issue. Fixed and verified against production data
- Loop failure alerting moved to a shared `utils.LoopFailureAlerter` (consecutive-failure
  threshold, error text in the alert, recovery announced)
- `/requests` and `/mystats` now `defer()` before querying — **needs live confirmation, see below**

---

## Live Verification Checklist

Start the bot locally: `source .venv/bin/activate && python bot.py`

### 1. Thread Watcher + React-to-Resolve (test together)

1. Go to the DigiLab app and submit a test request (store request, scene request, or bug report)
2. Check the corresponding forum channel — the app should create a thread
3. **Thread watcher should fire:** within ~2 seconds, the bot posts:
   - Instructions message ("React ✅ on the first message to mark this as resolved")
   - A follow-up tagging relevant scene admins
4. React ✅ on the **first message** (the webhook's post, not the bot's instructions)
5. **React-to-resolve should fire:**
   - Bot posts "✅ Resolved by @you"
   - Thread gets the appropriate tag (Resolved/Onboarded/Fixed/Shipped)
   - `#bot-log` gets an entry
6. Verify in the DB: `SELECT status, resolved_by FROM admin_requests WHERE discord_thread_id = '<thread_id>'` should show `resolved`

### 2. Permission Denial

1. Have a non-admin Discord user (or someone who isn't admin for that scene) react ✅ on a new request thread
2. **Expected:** reaction is removed, user gets a DM saying they need admin access
3. If the user has DMs disabled, the reaction is still removed (DM failure is handled gracefully)

### 3. Role Sync

1. In the DB, change an admin's role (e.g., update a scene_admin to regional_admin)
2. Wait up to 5 minutes
3. **Expected:** Discord role updates automatically, change logged to `#bot-log`
4. To test the reverse: manually add a DigiLab role to a non-admin in Discord
5. **Expected:** bot removes it on next sync cycle, logs to `#bot-log`

### 4. Auto-Archive

1. Find or create a thread with a resolve tag (Resolved/Onboarded/Fixed/Shipped) that has been inactive for 48+ hours
2. Wait for the hourly archive loop (or restart the bot to trigger it sooner)
3. **Expected:** thread gets archived, logged to `#bot-log`
4. Test "Won't Fix" / "Not Planned" / "On Hold" tags — these should auto-archive after 1 week
5. To test quickly: temporarily lower thresholds in `cogs/archiver.py`

### 5. Slash Commands

**`/requests` and `/mystats` are the priority here (2026-08-10).** They were failing outright
against the live database until today, *and* they are the two commands whose response mechanism
changed (they now `defer()` and reply via `followup`). Nothing about that path can be tested
without a live Discord server, so it is unverified until someone runs it.

- `/requests` — admin-only. Should render, not error. Expected shape as of 2026-08-10:
  34 store / 25 scene / 5 data-error / 3 bug-report open. Confirm the reply is **ephemeral**
  and that there is no lingering "thinking…" state
- `/mystats` — admin-only, shows your resolved count, avg resolution time, scenes managed.
  Same ephemeral / no-stuck-thinking check
- `/admins dfw` — should show admins with role emojis and mentions
- `/roster dfw` — admin-only, shows stores + tournament counts
- `/scene dfw` — shows stats + link to app
- `/help` — shows command list (ephemeral) — should list all 6 commands
- Test autocomplete by typing partial scene names

### 6. Auto-Tag "New" on Manual Threads

1. Create a new thread manually in `#bug-reports`, `#feature-requests`, or `#scene-requests`
2. **Expected:** Bot auto-applies the "New" tag and posts a welcome message
3. Verify the tag respects the 5-tag limit (if thread already has 5 tags, "New" is skipped)

### 7. Stale Thread Nudges

1. Find or create an unresolved thread in `#bug-reports` or `#scene-requests` with no activity for 3+ days
2. Wait for the daily nudge loop (runs every 24 hours)
3. **Expected:** Bot posts a reminder and re-pings relevant admins
4. Verify resolved threads (with Fixed/Won't Fix/etc. tags) are NOT nudged
5. Feature request threads should never be nudged

### 8. Weekly Scene Health Digest

1. Runs automatically on Mondays at 09:00 UTC
2. **Expected:** Bot posts a webhook message to `#admin-digest` with sections for dormant scenes, unassigned scenes, and deactivated stores
3. Follow-up message mentions relevant admins for each flagged scene
4. If all scenes are healthy, no thread is created
5. To test: temporarily change the weekday check in `cogs/digest.py`

### 9. Loop Failure Alerting (new 2026-08-10)

Hard to trigger deliberately; mostly confirmed by absence. What to look for in `#bot-log`:

1. A failing loop should produce **one** message naming the exception
   (e.g. `⚠️ **Role sync loop** failed 2 consecutive runs — ConnectionDoesNotExistError: …`),
   not a bare "check `railway logs`"
2. When it heals, a single `✅ … recovered after N failed runs`
3. A loop that fails once and succeeds on the next tick should produce **nothing** — that
   suppression is the whole point, and it is also the trade-off: a slow flap on a 5-minute loop
   is now invisible in Discord. `railway logs` still has every occurrence

---

## Deployment

**Done (2026-07-28 status):** production runs on Railway, auto-deploying from pushes to `main`
on `lopezmichael/datamon-bot`. Monitor with `railway logs` (CLI linked in this repo). The old
VPS/systemd instructions are obsolete and the systemd unit has been removed.

---

## Remaining Items

### Decide: `defer()` on the three public slash commands (2026-08-10)

`/requests` and `/mystats` defer before querying. `/admins`, `/roster` and `/scene` do not,
because they are the awkward case: their **success** reply is a public embed but their **error**
replies ("Scene `x` not found", "You need admin access for this scene") are ephemeral, and
`defer()` fixes visibility for the whole interaction. Covering them means picking one:

- **Defer public** — errors become visible in-channel
- **Defer ephemeral** — the scene/roster/admin embeds stop being publicly visible

Until one is chosen, those three can still hit Discord's 3-second deadline on a slow query and
show "the application did not respond".

### Confirm Neon's autosuspend / idle setting (2026-08-10)

The retry handles idle-connection drops regardless of cause, but the underlying cutoff was never
established. Neon's compute autosuspend defaults to 5 minutes, which would sit exactly on the
5-minute loop interval and would explain both the intermittency and the flapping. Visible in the
Neon console (Branch → Compute → scale-to-zero / `suspend_timeout_seconds`). Worth knowing before
changing any loop interval. Measured separately: connections to the `-pooler` endpoint survived
420s idle, so PgBouncer is absorbing most of it.

### Dead env var: `DISCORD_CHANNEL_SCENE_COORDINATION`

`config.py` stopped requiring it when PR 3 retired the channel, and `.env.example` is already
clean, but the variable is still set in local `.env` and probably in Railway's Variables tab.
Harmless — nothing reads it — but it should be deleted from Railway so the next person doesn't
think the channel is still live.

### Not yet populated: `admin_regions`

The `admin_regions` table exists but has no rows. Until regional admins are assigned, the bot treats all admins as either scene-level (via `admin_user_scenes`) or global (super_admins). To assign regional admins:

```sql
-- Example: make user_id 5 a regional admin for all of Germany
INSERT INTO admin_regions (user_id, country, assigned_by)
VALUES (5, 'Germany', 'michael');

-- Example: make user_id 12 a regional admin for Texas only
INSERT INTO admin_regions (user_id, country, state_region, assigned_by)
VALUES (12, 'USA', 'Texas', 'michael');
```

Also update their role in `admin_users` to `regional_admin` so role sync assigns the correct Discord role.

### Backfill `discord_thread_id` (optional)

Existing requests created before the migration have NULL `discord_thread_id`. React-to-resolve won't work on those threads. Options:
- **Do nothing** — only new requests get the feature (recommended, simplest)
- **Manual backfill** — match existing `admin_requests` to Discord threads by title/content and UPDATE the column

### 2 unlinked admins

aomceodeadly and gamescornerdigimon aren't in the Discord server. If they join later, run the matching script again or manually:
```sql
UPDATE admin_users SET discord_user_id = '<their_discord_id>' WHERE username = '<username>';
```
