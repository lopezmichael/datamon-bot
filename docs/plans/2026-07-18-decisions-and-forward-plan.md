# Datamon Bot — Decisions & Forward Plan

**Date:** 2026-07-18
**Status:** Planned, not started (deliberately waiting on digilab-web phases)
**Prereq reading:** `2026-07-16-request-flow-redesign-impact.md` (impact assessment),
`digilab-web/docs/plans/2026-07-16-request-flow-redesign.md` (the web plan + its 2026-07-18 addendum)

## Decisions locked 2026-07-18

1. **Digest destination: the existing admin text channel.** No new channel; no forum threads.
   `DISCORD_WEBHOOK_ADMIN_DIGEST` (web-side env) points at a webhook created on the admin
   channel. Rationale: the requirement is text-channel-*shaped*, not a *new* channel — the
   latest message is the current state ("silence = inbox zero"). Digest-as-forum-thread was
   considered and rejected: an untagged thread in a forum passes the nudge gate
   (`cogs/nudge.py:85`), so the bot would nudge its way into old digest threads, and the
   archiver never cleans untagged threads.
2. **Digest cadence and ping policy: post daily when pending > 0; ping only stale items.**
   Discord mention mechanics make this clean: `<@id>` in message text *renders* as a mention,
   but only IDs listed in `allowed_mentions.users` actually notify. Fresh items name their
   responsible admins un-pinged; items pending > 72h put those admins into `allowed_mentions`.
   Re-ping daily while stale (tuning knob: back off to every 3rd day if it gets noisy).
   Pending bug/feature counts ride along un-pinged. 72h aligns with the bot's existing
   3-day first-nudge rhythm, so admins get one consistent expectation everywhere.
3. **Phase 4 posts to the existing `#feature-requests` forum, not `#bug-reports`.**
   The channel and full tag set already exist and the bot is fully wired for it
   (`config.py:75-81`; resolve = "Shipped"). Web-side: set `DISCORD_TAG_FEATURE` to the value
   of the bot's existing `DISCORD_TAG_NEW_FEATURE_REQUESTS`, create a webhook on the feature
   forum, apply the New tag at thread creation, write `discord_thread_id` back like other types.
4. **The bot stays a single-guild ops sidecar. No installable/multi-server rebuild.**
   External-server results notifications get built in digilab-web as user-profile webhook
   subscriptions (sketch in §5) — no bot install, no OAuth, no per-guild state. A public
   interactive bot is only on the table if users start asking for slash commands *in their
   own servers* (the one thing webhooks can't do), and it would be a separate,
   credential-minimal bot talking to a DigiLab API — never this process, which holds DB
   credentials with admin data.

## 1. Bot work plan (PR-by-PR)

**PR 0 — own-thread guard (anytime, independent of web phases).**
`on_thread_create` (`cogs/thread_watcher.py:21`) has no bot-author check, so the bot's own
weekly scene-health digest threads in #scene-coordination get the manual "Welcome!" message.
Confirm on the live server, then guard with `thread.owner_id == self.bot.user.id`.

**PR 1 — Phase 2 cleanup (with or after the web Phase 2 deploy; safe to lag).**
If web ships first, data_error/store_request threads simply stop being created — nothing errors.
- Delete dead templates: `messages.py:9-28` (store_request + data_error under
  scene_coordination) and `messages.py:55-64` (data_error under bug_reports).
- Fix stale comment `cogs/thread_watcher.py:64-67`.
- Keep #scene-coordination in `FORUM_CHANNELS` (manual threads, legacy pending threads,
  weekly health digest all still need it).
- Doc pass: README/DESIGN request-flow descriptions, `.env.example` tag comments.
- No env or config changes.

**PR 2 — Phase 4 support (before or with the web Phase 4 deploy).**
- Add `_APP_MESSAGES["feature_requests"]["feature_request"]` instruction template
  (mirror the bug template with Shipped semantics). Without it the generic fallback
  ("mark this as shipped") is correct but terse; react-to-resolve works day one regardless.
- Skip nudge coverage for features by default (no SLA). If wanted later: add
  `CHANNEL_FEATURE_REQUESTS` to `NUDGE_CHANNELS` with status set `{TAG_NEW_FEATURE_REQUESTS}`
  only — Planned means "accepted, waiting on dev" and shouldn't nudge.
- No new env vars.

**Server checklist (owner: Michael, at web deploy time):**
- Phase 2: create webhook on the existing admin text channel → web env
  `DISCORD_WEBHOOK_ADMIN_DIGEST`. That's the only server change.
- Phase 4: create webhook on `#feature-requests` for the web app; copy the existing
  New-tag ID into web env `DISCORD_TAG_FEATURE`. No new channels or tags.

**Expected server effect:** #scene-coordination goes quiet (manual discussion + legacy
drain + Monday health check only); #bug-reports/#scene-requests unchanged;
#feature-requests receives app threads for the first time; admin channel gets ≤1 digest/day.

## 2. Digest spec (implemented in digilab-web; recorded here for coordination)

Daily cron (`/api/internal/requests/digest`, `CRON_SECRET` auth, ~15:00 UTC). Skip entirely
when nothing is pending. Otherwise one message:

```
📋 6 pending: 3 data errors, 2 store requests · 1 bug, 0 features open
Data errors (3): Tournament #12345 (Jordan P. deck) · #12377 (record) · #12401 → admin/tournaments
Store requests (2): "Game Haven" (Dallas) · "Card Castle" (Tulsa) → admin/stores

⏰ Stale >72h — action needed:
• Data error — Tournament #12345 (4d) — @Alice
• Store request — "Game Haven" (5d) — @Bob @Carol
```

- Fresh section: admins named via `<@id>` but **excluded** from `allowed_mentions` (no ping).
- Stale section: those admins **included** in `allowed_mentions` (real notification),
  deduped across items; uncovered items fall to global admins.
- Mention resolution must match the bot's cascade semantics exactly — the 7-point parity
  checklist is in `2026-07-16-request-flow-redesign-impact.md` §4 (tiered precedence, not a
  union; "covered" = non-null `discord_user_id`; state rollups fan out, country rollups
  don't; game-scoping decision needed since the bot hardcodes `game_id='digimon'`).
- Bot involvement: none. The channel isn't in `FORUM_CHANNELS`, so every listener ignores it.

Web-side verify items carried over from the impact assessment:
- `resolveDiscordThread` must apply the channel's **resolve tag** on legacy threads, not just
  post a message — the bot's archiver is tag-driven and only the reaction path tags today.
- The Apply & Resolve action must tolerate rows already resolved via reaction (and vice
  versa: reaction on an in-app-resolved row is already a safe no-op, `cogs/reactions.py:72`).

## 3. What the bot is (identity statement)

A **single-guild ops sidecar**: a mostly-read-only mirror of the shared DigiLab database into
the admin team's Discord, with exactly one write (resolve). Current functions:

| Cog | Function |
|---|---|
| `role_sync` | DB admin roles → Discord roles, 5-min loop |
| `thread_watcher` | Instructions + admin mentions + New tag on forum threads |
| `reactions` | React ✅ to resolve (`admin_requests.status`) |
| `nudge` | Stale thread lifecycle: nudge 3d/21d, close 30d |
| `archiver` | Archive resolved/closed threads (48h / 1wk by tag) |
| `digest` | Weekly scene-health digest (Mondays, #scene-coordination) |
| `commands` | `/admins` `/roster` `/scene` `/requests` `/mystats` `/help` |

The redesign intentionally *shrinks* its workflow surface — Discord stops being the inbox.

**Good-fit future functions** (same identity: read-only, home guild, low trust surface):
- More lookups: `/player`, `/store`, `/results <scene>`, archetype/meta stats.
- Community-facing announcements in the home server: new results feed, weekly meta snapshot.
- Admin accountability: resolution leaderboards, digest enrichment.

**Poor fits (don't build into this bot):** anything multi-tenant/per-guild-state, anything
that widens DB write access, anything installable in untrusted guilds.

## 4. Explicitly rejected alternatives (so future sessions don't relitigate)

- **New dedicated digest text channel** — fine but unnecessary; existing admin channel wins.
- **Digest as #bug-reports forum thread** — nudge loop harassment + archiver blindness +
  daily thread clutter + wrong channel semantics.
- **Ping-on-every-fresh-item** — re-creates the noise Phase 2 deletes; trains channel muting.
- **Making datamon-bot installable/multi-server** — architecture is single-guild to the bone
  (every ID is an env var), Discord verification + privileged-intent review at scale, and
  trust separation: this process holds admin DB credentials.
- **Bot interacting with the digest channel (react-to-resolve on line items)** — no per-item
  anchor exists (resolve keys on thread ID = starter message ID = `discord_thread_id`);
  would need a web-written mapping table and a duplicate resolution path.

## 5. Future feature sketch: Discord webhook subscriptions (digilab-web, not this repo)

The answer to "users want DigiLab notifications in their own server" — without a bot.

- **Settings UI** (account settings → "Discord notifications"): user creates an incoming
  webhook in their own channel (2 clicks in Discord), pastes the URL, picks scope — likely
  reusing the follows/favorites model (my scenes, my stores; archetype filters later).
  Requires an account. Natural Pro-perk candidate.
- **Storage:** one table — `user_id`, `webhook_url`, filter config (JSON), `is_active`,
  `last_posted_at`, `failure_count`.
- **Validation:** URL must match `discord.com/api/webhooks/…`; "Send test message" button on
  save.
- **Sender:** fires on result ingest (or piggybacks a cron); posts formatted result embeds
  per subscription's filters. Rate-limit friendly: batch per webhook per run.
- **Failure handling:** 401/404 = webhook deleted on their end → set `is_active = false`,
  surface a "reconnect" notice in settings. Transient 5xx → retry next run,
  deactivate after N consecutive failures.
- **Non-goals:** no bot installation, no OAuth flow, no slash commands in external guilds,
  no per-guild configuration. If command demand materializes, that's a separate public-bot
  project against a DigiLab API.

## Sequencing summary

| Trigger | Work |
|---|---|
| Anytime | Bot PR 0 (own-thread guard) |
| Web Phase 1 ships | Nothing bot-side; verify resolve-tag behavior on legacy threads |
| Web Phase 2 ships | Server: digest webhook on admin channel. Bot PR 1 (cleanup, can lag) |
| Web Phase 4 approaches | Bot PR 2 (feature template) before/with deploy; server: feature webhook + tag ID |
| Web Phases 3 & 5 | Nothing bot-side (role_sync picks up Phase 5 writes automatically) |
| User demand for external-server notifications | Webhook-subscriptions feature in digilab-web (§5) |
| User demand for external-server *commands* | Only then: separate public bot, API-backed |
