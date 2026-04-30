# Datamon Bot — Next Steps

## Current Status (2026-04-30)

- All bot code implemented, bug-reviewed, and compiles cleanly
- Bot connects to Discord as **Datamon#4349**, all 7 cogs load, command tree synced
- **84/86** admin `discord_user_id` values linked in DB (2 not in server: aomceodeadly, gamescornerdigimon)
- Role sync verified: all Discord roles match DB state
- Slash commands (`/admins`, `/roster`, `/requests`, `/mystats`, `/scene`, `/help`) confirmed working
- DB migrations applied: `discord_thread_id` on `admin_requests`, `admin_regions` table created
- Existing requests have NULL `discord_thread_id` — react-to-resolve only works on new requests
- All four forum channels have consistent tag behavior (resolve tags, status stripping, auto-archive)
- `resolved_by` now stores Discord user ID (not display name) — old records unaffected

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

### 5. Slash Commands (already confirmed working, but double-check)

- `/admins dfw` — should show admins with role emojis and mentions
- `/roster dfw` — admin-only, shows stores + tournament counts
- `/requests` — admin-only, shows open request counts per type with oldest age and avg resolution time
- `/mystats` — admin-only, shows your resolved count, avg resolution time, scenes managed
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
2. **Expected:** Bot creates a forum thread in `#scene-coordination` with sections for dormant scenes, unassigned scenes, and deactivated stores
3. Follow-up message mentions relevant admins for each flagged scene
4. If all scenes are healthy, no thread is created
5. To test: temporarily change the weekday check in `cogs/digest.py`

---

## Deployment

The bot currently runs on your laptop. To run it persistently:

### Option A: VPS (recommended)

1. Provision a small Linux VPS (Ubuntu 22.04+, 1 vCPU, 512MB RAM is plenty)
2. Install Python 3.12+
3. Copy the project:
   ```bash
   scp -r /Users/michaellopez/repos/datamon-bot user@server:/opt/datamon-bot/
   ```
4. On the server:
   ```bash
   cd /opt/datamon-bot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
5. Copy your `.env` to the server (securely — don't commit it)
6. Install the systemd service:
   ```bash
   sudo cp systemd/datamon.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now datamon
   ```
7. Monitor: `sudo journalctl -u datamon -f`

### Option B: Same server as digilab-app

If digilab-app already runs on a server, deploy alongside it. Same steps as above.

---

## Remaining Items

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
