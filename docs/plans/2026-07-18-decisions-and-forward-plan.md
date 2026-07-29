# Datamon Bot — Decisions & Forward Plan

**Date:** 2026-07-18
**Status:** Planned, not started (deliberately waiting on digilab-web phases)
**Prereq reading:** `2026-07-16-request-flow-redesign-impact.md` (impact assessment),
`digilab-web/docs/plans/2026-07-16-request-flow-redesign.md` (the web plan + its 2026-07-18 addendum)
**Amended:** 2026-07-28 addendum below — multi-game direction, server channel plan, digest
destination changed to a dedicated `#admin-digest` channel (supersedes Decision 1's destination).

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

Web-side verify items carried over from the impact assessment — **both RESOLVED 2026-07-28**
(digilab-web `c40bd57`):
- ~~`resolveDiscordThread` must apply the channel's **resolve tag**~~ — **done, and it was
  worse than "not implemented".** The web function did have a tagging branch, but it read a
  single `DISCORD_TAG_RESOLVED` env var that **was never set in that repo**, so the branch
  returned early every time: web-side tagging had run zero times in production, and **this bot
  has been the only thing tagging threads to date.** That is why the gap was invisible —
  ✅-resolved threads archived fine, in-app-resolved threads sat open and kept getting nudged.
  `getResolutionWebhookUrl` is now `getResolutionTarget`, returning the webhook **and** the
  per-channel tag plan from a map mirroring our `FORUM_CHANNELS` (Resolved / Onboarded /
  Fixed, plus `Won't Fix` for rejections in #bug-reports). One env var could never have
  covered three channels anyway — tag IDs are per-forum. Stripping each channel's
  `initial_tags` is implemented but **conditional on four further IDs they treat as
  optional**, correctly: stripping is cosmetic, since `nudge.py` tests `tag_ids & DONE_TAGS`
  before anything else and `archiver.py` gates on the completion tag's presence. So expect
  in-app-resolved threads to keep wearing their New / Under Review tag alongside the
  completion tag unless those optional IDs get set too. **Still pending on their side: the
  three completion-tag IDs must be copied from our `.env` into theirs and into Vercel, or the
  whole path ships inert.**
- ~~Apply & Resolve must tolerate rows already resolved via reaction~~ — **done.** It no longer
  400s on a resolved row; it still performs the entity write and only no-ops the status write,
  so an admin who ✅'d in Discord and then fixed the data in the app gets the fix applied
  rather than silently dropped. The reverse (reaction on an in-app-resolved row) was already a
  safe no-op here (`cogs/reactions.py:72`) and is unchanged.

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
| ✅ Web Phase 1 shipped 2026-07-28 | Nothing bot-side. Resolve-tag behavior verified: web now applies the per-channel completion tag (`c40bd57`); see §2 |
| Web Phase 2 ships | Server: digest webhook on admin channel. Bot PR 1 (cleanup, can lag) |
| Web Phase 4 approaches | Bot PR 2 (feature template) before/with deploy; server: feature webhook + tag ID |
| Web Phases 3 & 5 | Nothing bot-side (role_sync picks up Phase 5 writes automatically) |
| User demand for external-server notifications | Webhook-subscriptions feature in digilab-web (§5) |
| User demand for external-server *commands* | Only then: separate public bot, API-backed |

---

# Addendum 2026-07-28 — Multi-game direction & server channel plan

Discussed and agreed 2026-07-28. Context that changed since 2026-07-18:

- **DigiLab is going multi-game.** Launch order: Gundam first, then One Piece / Fusion World /
  Union Arena, Naruto when it exists. The Discord server becomes **the DigiLab platform server
  for all games** — Discord stays the onboarding funnel (scene requests, admin onboarding);
  the site is where the work happens.
- **Per-game admin teams are a requirement.** Digimon-Dallas and Gundam-Dallas may be
  different people; cross-game admins will exist but are the exception.
- **§5 already shipped.** The "future sketch" webhook-subscriptions feature was built in
  digilab-web on 2026-07-23 (`user_webhooks` / `user_webhook_subscriptions` /
  `webhook_deliveries`, plan `digilab-web/docs/plans/2026-07-23-account-badges-and-discord-webhooks.md`).
  §5 is no longer pending work — remaining gap is a **per-game filter on subscriptions**
  (web-side), needed before a second game has results.

## A1. Multi-game data model facts (digilab-web, verified against schema)

- Scenes are **shared geography**: `scenes` has no game column; `scene_games (scene_id,
  game_id, is_active)` flips a scene "online" per game (schema.sql:705). "Dallas" is one row.
- `game_admin_roles (user_id, game_id, role)` is already per-game (schema.sql:438).
- **The gap:** `admin_user_scenes` (schema.sql:112) and `admin_regions` (schema.sql:50) have
  **no game column** — they can't express "admins Dallas *for Gundam*." This is the one
  schema migration per-game admins force: add `game_id`, backfill `'digimon'` (same pattern
  `admin_requests.game_id` used on 2026-07-08). Must land **before** the Phase 2 digest is
  built, so the digest cascade is game-correct from day one, and belongs with web Phase 5
  (the UI that writes these tables).

## A2. Amended decision: digest destination is a dedicated `#admin-digest` channel

Supersedes the destination clause of Decision 1 (2026-07-18: "the existing admin text
channel"). The **forum-thread rejection stands unchanged** (nudge-loop harassment, archiver
blindness, daily thread clutter — see §4).

What changed: under multi-game the digest grows (per-game sections, more items, possibly
multiple messages on busy days), and in a *chat* channel daily bot posts interleave with
human conversation — the digest gets scrolled away and the "latest message = current state /
silence = inbox zero" property erodes. The 2026-07-18 rejection of a dedicated channel was
"fine but unnecessary"; multi-game makes it necessary. New spec:

- **`#admin-digest`**: plain text channel, read-only for humans, visible to all admin roles.
  `DISCORD_WEBHOOK_ADMIN_DIGEST` points here.
- **One message per day covering all games, grouped by game** (e.g. `📋 Digimon: 3 data
  errors · Gundam: 1 store request`), while volume allows. If a message approaches Discord
  limits, split per game — harmless in a channel with no conversation to interrupt.
- Ping policy unchanged from Decision 2 (fresh items named un-pinged; >72h stale items get
  real `allowed_mentions`, per-item cascade **filtered by the request's `game_id`**).
- Bot involvement: none (channel not in `FORUM_CHANNELS`), unchanged.

## A3. Multi-game Discord decisions locked

1. **Discord admin roles stay shared across games** (@Platform/@Regional/@Scene Admin).
   The cascade pings *user IDs*, not roles — roles only gate channel visibility, and admin
   channels are shared. Per-game role sets (3 × N games) buy nothing without per-game private
   channels, which contradict the platform-server model. `role_sync` semantics become "max
   role across any game" (union over `game_admin_roles`).
2. **Forum channels stay shared; game tags on `#scene-requests` only.** It's the onboarding
   funnel — "Scene Request: Portland" is meaningless without the game. One tag per game,
   applied web-side at thread creation (same mechanism as the old continent tags).
   - `#feature-requests`: **no game tags** — requests are overwhelmingly platform-level.
   - `#bug-reports`: **no game tags** — the game-specific traffic there today (uncovered data
     errors) stops threading entirely when Phase 2 ships; what remains is platform bugs.
3. **`#scene-coordination` is scheduled for deletion** (it cannot be deleted yet): store
   requests / covered data errors still thread there until web Phase 2; legacy threads must
   drain; the weekly scene-health digest (`cogs/digest.py`) posts there; and the bot
   fail-fasts on the env var. Path: Phase 2 ships → legacy drain → Bot PR 3 (below) →
   delete channel. Continent tags die with it — game tags on #scene-requests take over.
4. **Community game roles via native Discord onboarding** ("Channels & Roles"): members
   self-select games at join → @Gundam etc. → shows game channels, makes announcement pings
   consented. Completely separate namespace from the three synced admin roles (role_sync
   only manages its three — no collision, but don't reuse names).
5. **Per-game community channels are created lazily**: one text channel per game (e.g.
   `#gundam`) when that community materializes, behind its onboarding role. Expand to a
   category only under real traffic — empty per-game categories read as a dead server.
6. **`#digilab-activity` stays one shared feed**; embeds stamp the game via the existing
   `DiscordEmbed.author` field (added for this). Split per-game only if volume forces it.
7. **Per-game results feeds in the home server dogfood user-webhooks**: create a webhook in
   the game's channel, subscribe with a game/scene filter. Zero bot code; was listed as a
   "good-fit future bot function" in §3, now a config task (pending the game filter, A0).
8. **No game tags / no changes** for #general, #welcome, #rules, #supporters, #api,
   #platform-admins, #admin-chat, #bot-log.

## A4. Bot work plan additions (extends §1)

**PR 3 — retire #scene-coordination (after Phase 2 + legacy drain).**
*Built 2026-07-29 (committed, not pushed): digest rehomed to #admin-digest via
`DISCORD_WEBHOOK_ADMIN_DIGEST` (already set in Railway + local .env), channel/tag config
stripped. Push = deploy, gated on the 10-row legacy queue reaching zero. After deploy: delete
the Discord channel.*
- Rehome the weekly scene-health digest as a plain embed in an admin channel
  (consolidating admin reporting; exact channel decided at PR time).
- Remove `CHANNEL_SCENE_COORDINATION` and its tags from `config.py` / `FORUM_CHANNELS` /
  `.env.example` (bot fail-fasts on missing env vars — deletion requires this PR first).
- Then delete the channel server-side.

**PR 4 — game-aware bot (needed by the time a second game's requests flow, not before).**

> **A1 dependency satisfied 2026-07-28** (digilab-web `cc450f7`, applied to the shared
> production DB). `admin_user_scenes` and `admin_regions` both carry
> `game_id VARCHAR(30) NOT NULL DEFAULT 'digimon' REFERENCES games(game_id)`; **every row is
> `'digimon'`**. The `admin_user_scenes` PK is now `(user_id, scene_id, game_id)` and
> `idx_admin_regions_unique` includes `game_id`. Verified post-migration that our unfiltered
> join shape still returns 186 scene rows / 8 region rows with no row duplicated under the old
> narrower keys.
>
> **Nothing reads the column yet — on the web side either.** Their writes are game-scoped
> (`api/admin/users.ts`, because an unscoped role-switch DELETE would wipe another game's
> assignments), but every read is deliberately unfiltered, for the same reason we are: a
> one-sided predicate desyncs the two cascades. So this bot is free to stay unfiltered until
> PR 4, and PR 4 lands together with their Phase 5.
- `db.py`: parameterize the five hardcoded `game_id = 'digimon'` joins (db.py:56, 113, 135,
  151, 381) by the request row's `game_id`; cascade becomes
  `get_admins_for_scene(scene_id, game_id)`; scope `admin_user_scenes` / `admin_regions`
  joins by game once those tables carry it.
- `reactions.py`: resolve-permission check scoped by the request's game.
- `role_sync`: max-role-across-games union per A3.1.
- `/admins`, `/roster`, `/scene`: optional game parameter (default: all games for the scene).
- **Decision required at PR 4 (found during the Phase 2 cascade port, 2026-07-29): is
  "platform admin" inherently global, or can it be per-game?** The tier-3 global fallback
  (`db.get_global_admin_discord_ids`) reads only `admin_users.role` and the `"user"`
  is_super/is_platform flags — a user whose platform_admin role exists *only* as a
  `game_admin_roles` row for one game is NOT in the global fallback, on either side (the web
  digest ported this faithfully). Inert while everything is digimon; at Gundam launch decide:
  either platform admins are always global (enforce via user flags, never per-game rows), or
  the fallback becomes per-game (global fallback for a Gundam request = Gundam platform
  admins + flagged users). Whichever way, bot and web must change together.

**Web-side items recorded here for coordination:**
- `admin_user_scenes` / `admin_regions` `game_id` migration (A1) — before Phase 2 digest.
- Digest game-grouping + game-scoped cascade (A2).
- Per-game subscription filter on user-webhooks — before second game's results.
- `sendWelcomeDM` (`digilab-web/src/lib/discord.ts:522`) hardcodes DigiLab/Digimon branding
  and channel names — read from `game-config` terminology before onboarding non-Digimon admins.
- Game tag env vars for #scene-requests, stamped at thread creation per the request's game.
- **Webhook identities (small, anytime):** give every webhook post a proper name + avatar
  instead of the manually-set Integrations-tab defaults. Cheapest path is per-message:
  Discord webhook payloads accept `username` and `avatar_url` — the bot's `log_to_discord`
  already sends `username="Datamon Bot"`; digilab-web's `discordSend` sends neither. Add
  optional username/avatar params there and stamp each sender (e.g. "DigiLab Requests",
  "DigiLab Digest") with a hosted icon URL. Covers all five web webhooks + future digest
  in one change; no Discord-side edits needed.

## A5. Updated sequencing (supersedes the 2026-07-18 table where they conflict)

| Order | Work |
|---|---|
| Now | Bot PR 0 (own-thread guard) |
| ✅ Done 2026-07-28 | Web Phase 1 (Apply & Resolve + resolve tags + atomic store approve), `c40bd57` — game-agnostic |
| ✅ Done 2026-07-28 | Schema migration: `game_id` on `admin_user_scenes` + `admin_regions` (digilab-web `cc450f7`, applied to the shared production DB and verified idempotent on re-run) |
| Web Phase 2 | Digest to new `#admin-digest` (A2), game-grouped; server: create channel + webhook. Bot PR 1 (cleanup, can lag) |
| Web Phase 4 | Bot PR 2 (feature template); server: feature webhook + tag ID |
| After Phase 2 + legacy drain | Bot PR 3 (retire #scene-coordination) |
| Before 2nd game's requests flow | Bot PR 4 (game-aware bot); server: game tags on #scene-requests |
| Before 2nd game's results | Web: per-game filter on user-webhook subscriptions |
| When Gundam community materializes | Server: onboarding roles + `#gundam` channel; dogfooded results webhook |

## A6. Production facts + digest bug found 2026-07-28 (expands PR 0)

**Production is Railway, not the DigitalOcean droplet the docs described.** Project
`datamon-bot` / service `datamon-bot` / env `production`, connected to
`lopezmichael/datamon-bot` — push to `main` auto-deploys. `railway` CLI is linked in this
repo; logs via `railway logs` (~1-week retention). README/DESIGN/NEXT_STEPS/CLAUDE.md were
corrected and `systemd/` deleted on 2026-07-28.

**The weekly scene-health digest has never fired — root cause found and verified:**
`db.get_dormant_scenes` (db.py:295) selects `t.date` from `tournaments`, but the column is
**`event_date`** (verified against the live DB 2026-07-28: `UndefinedColumnError`). Every
Monday 09:00 UTC the `asyncio.gather` in `cogs/digest.py:44` throws, and an uncaught
exception permanently kills a `discord.ext.tasks` loop — so the digest dies on the first
Monday after each deploy and stays dead until the next restart. Non-Monday runs return
before any logging, so the failure was invisible. Evidence: retained Railway logs
(Jul 22–28) show zero digest lines, including Monday Jul 27 09:00 with the bot online
(deploy of Jul 18 active); no digest thread has ever existed in #scene-coordination
(~13 Mondays since the cog shipped 2026-04-30). This also explains why the PR 0 own-thread
bug was never observed live — there were no own threads to welcome.

**PR 0 scope (revised):**
1. Fix `t.date` → `t.event_date` in `get_dormant_scenes`.
2. Wrap the `weekly_digest` loop body so an exception can't kill the loop (log + continue);
   audit `nudge`/`archiver`/`role_sync` loops for the same fragility while there.
3. The original own-thread guard in `on_thread_create` (`thread.owner_id == bot.user.id`) —
   needed *because* the digest will now actually post.
4. Heads-up at deploy: the first successful digest may be large (60+ days of dormant scenes
   and unassigned scenes have never been reported).

**Two more found 2026-07-28 while shipping the web side's resolve tags. Both belong in PR 0**
— they are the same failure class the rest of PR 0 already collects (silent failures inside
our own loops), and both are independent of every web phase, so nothing gates them.

5. **`_apply_resolve_tag` has no 5-tag cap, and an over-cap edit escapes the listener.**
   `cogs/reactions.py::_apply_resolve_tag` builds `new_tags` by keeping the non-stripped
   existing tags and appending the resolve tag, with no length bound, and catches only
   `discord.Forbidden`. On a thread already carrying five non-strippable tags we send six,
   Discord answers `400`, and the `discord.HTTPException` escapes all the way out of
   `on_raw_reaction_add` — **after `db.resolve_request` has already committed**
   (`cogs/reactions.py:107-117`). The row is resolved, the thread is untagged, and the
   archiver will never touch it: precisely the symptom we just spent a web-side commit fixing
   from the other direction. `cogs/thread_watcher.py:139-142` already gets both halves right
   (`if len(existing) < 5` and `except discord.HTTPException`), so the two implementations
   currently disagree about the same invariant. The web side now caps correctly too — it
   always preserves the completion tag and drops status tags first to make room
   (`mergeThreadTags` in `src/lib/discord.ts`, unit-tested), which is the semantics to copy:
   dropping a status tag is cosmetic, dropping the completion tag is the bug.
6. **Assert the configured tag IDs exist, once, at startup.** One `GET /channels/{id}` per
   channel in `FORUM_CHANNELS`, checking every configured tag ID (`resolve_tag`, `new_tag`,
   `initial_tags`) against that channel's `available_tags`. Today a wrong or deleted tag ID
   fails silently and forever on **both** sides — we swallow it, and the web app logs a
   `console.error` into Vercel and moves on — and the symptom (threads never archiving, nudges
   never stopping) is identical to the bug just fixed, so the next occurrence will cost the
   same investigation over again.

   **This repo should own it.** We hold a live gateway connection with `available_tags`
   already materialized on every `ForumChannel` object, and we fail fast on missing env vars
   at boot already, so this is a few lines in the same place rather than bespoke REST plumbing
   in an edge function that only ever sees its own three channels. It covers the web app's IDs
   transitively as well, since theirs are copied verbatim from ours — the residual risk is a
   bad transcription into their Vercel env, which is a deploy-time check, not a runtime one.

---

# Addendum 2026-07-29 — Web Phase 2 shipped (digest live, thread shutoff done)

Shipped in digilab-web on `develop` (not yet merged to `main` / deployed). This is the
trigger row in §A5's table: **Bot PR 1 (cleanup) is now unblocked**, and it can lag safely.

## What landed on their side

- **Daily digest** at `GET /api/internal/requests/digest`, Vercel cron `0 15 * * *`,
  `CRON_SECRET` auth. Posts to the dedicated `#admin-digest` text channel via
  `DISCORD_WEBHOOK_ADMIN_DIGEST`, per §A2. Skips entirely when nothing is pending.
- **Grouped by game** (`admin_requests.game_id`), per §A2. Data errors and store requests are
  itemized with admin deep links; bug and feature counts ride along un-pinged. Zero counts are
  dropped, so the §2 sample's "0 features open" never actually renders — the code is right,
  the sample was illustrative.
- **Ping policy exactly as locked in Decision 2**: fresh items name their admins via `<@id>`
  but are excluded from `allowed_mentions`; items past 72h are included, deduped across items,
  re-pinged daily. `allowed_mentions.parse` is always `[]`.
- **Thread shutoff**: `postDataError` and `postStoreRequest` are deleted, along with their
  continent-tag helpers. `#scene-requests` and `#bug-reports` thread exactly as before.

## Bot PR 1 is unblocked

Delete the dead templates — `messages.py:9-28` (store_request + data_error under
scene_coordination) and `messages.py:55-64` (data_error under bug_reports) — and fix the stale
comment at `cogs/thread_watcher.py:64-67`. Nothing errors in the meantime: those types simply
stop arriving.

**Do not delete `#scene-coordination` yet.** Verified at ship time: **8 pending rows still
carry a `discord_thread_id`**, so the legacy resolution path is live traffic, not a
hypothetical. Their `resolveDiscordThread` / `getResolutionTarget` and the
`store_request` / `data_error` branches of `resolutionChannelFor` are all retained for exactly
those rows, and their code says so in a comment so nobody "cleans it up".

**Nobody is measuring the drain, on either side.** PR 3 gates channel deletion on it and their
code gates the dead branches on it, and there is no query, script, or surface reporting the
number anywhere. `SELECT count(*) FROM admin_requests WHERE status='pending' AND
discord_thread_id IS NOT NULL` is the whole check. They have it filed as a follow-up (surface
it in the digest summary); until one side does it, treat "is the drain done?" as unanswered
rather than assumed.

## Cascade parity: verified, with one deliberate deviation

Their port is `getSceneAdminCandidates` + `selectTierAdmins` in
`src/lib/admin-digest-queries.ts` / `admin-digest.ts`, taken from `db.py:86-197` and **not**
from `sceneHasAreaAdmin` (impact §4 point 7). Before shipping they ran the batched query
side by side with our per-scene query against the shared production DB, for every pending item
and each distinct scene, exercising the direct, child-metro and global-fallthrough branches:
**zero mismatches**. All seven parity points are ratcheted in their test suite, and the
ratchets were mutation-tested (deliberate regressions introduced one at a time, each confirmed
to fail).

**The deviation, and why it does not break the §4 point 6 contract.** The digest cascade
**is** game-scoped — it filters `admin_user_scenes`, `admin_regions` and `game_admin_roles` by
the request's `game_id`. The agreed contract (both sides stay unfiltered until web Phase 5 /
our PR 4) is nonetheless intact, because it applies to `sceneHasAreaAdmin`, which is the
function with a counterpart on our side and which they left untouched. The digest cascade has
no counterpart here: every one of our consumers is thread-driven, and digest items have no
thread, so nothing we run reads that mention set. Every row in those tables is `'digimon'`
today, so it is inert either way. **Tier 3 stays unfiltered on both sides**, matching
`db.py:160-172`.

**For PR 4:** their game-scoped cascade is now the reference implementation for the same
change here, and `sceneHasAreaAdmin` still has to flip *with* us.

**One divergence to expect at game #2, currently unhandled on both sides:** their `/admins`
slash command answer and the digest's mention set will disagree for a non-Digimon scene until
PR 4 lands, since ours is still hardcoded to `game_id = 'digimon'`.

## Webhook identities (the §A4 "small, anytime" item) — done

`discordSend` now takes `username` / `avatar_url`, and every web sender is stamped:
"DigiLab Digest", "DigiLab Requests", "DigiLab" (activity), "DigiLab Admin" (resolution
embeds), all with `https://digilab.cards/icons/icon-192.png`. No Discord-side changes needed.
Our `log_to_discord` already did this; the asymmetry §A4 noted is closed.

## Still owed on their side — NOTHING (correction 2026-07-29)

The paragraph that stood here was stale: all eight tag IDs (`RESOLVED` / `ONBOARDED` /
`FIXED` / `WONT_FIX` + the four strip tags) were added to their Vercel env (Production +
Preview) and local `.env` on 2026-07-29, before this addendum was written. The resolve-tag
path is fully armed. `DISCORD_WEBHOOK_ADMIN_DIGEST` is likewise set. Remaining gates are
Michael's: a live digest test fire, then merge `develop` → `main` (which activates the
thread shutoff and the cron).
