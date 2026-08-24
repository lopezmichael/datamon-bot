# Request Flow Redesign — datamon-bot Impact Assessment & Change Plan

**Date:** 2026-07-16
**Status:** Research complete. Web Phase 1 shipped 2026-07-28; both web-side verify items this
doc raised are closed (TL;DR 5 and §6). No bot implementation yet — bot PRs still pending.
**Companion doc:** `digilab-web/docs/plans/2026-07-16-request-flow-redesign.md`
**Follow-up:** decisions locked + PR-by-PR plan in `2026-07-18-decisions-and-forward-plan.md`

## TL;DR

1. **The web plan's Phase 4 channel choice is wrong about this bot.** The plan says
   feature requests will thread "to the bug-reports forum channel with a `DISCORD_TAG_FEATURE`
   tag (or a sibling channel if volume warrants)" and lists "no feature-request channel" as a
   pain point. The sibling channel **already exists and is fully wired**: `#feature-requests`
   is a registered forum in `config.py:75-81` with its own tag set (`New`/`Planned`/`Shipped`/
   `Not Planned`, `config.py:46-48`), react-to-resolve semantics (label "Shipped"), archiver
   coverage, and a manual-thread welcome message (`messages.py:128-138`). It was built
   Discord-only with no app webhook (see `docs/superpowers/specs/2026-03-16-webhook-and-bot-messaging-design.md:260`).
   The web side should post feature threads **there**, and set its `DISCORD_TAG_FEATURE` env to
   the existing `DISCORD_TAG_NEW_FEATURE_REQUESTS` tag ID rather than minting a new tag.
2. **React-to-resolve is confirmed generic over `admin_requests`** — no `request_type`
   special-casing anywhere. But resolve *semantics* (which tag is applied, which label is
   announced) are keyed per **channel**, not per type. Threading features into `#bug-reports`
   would resolve them as "**Fixed**" with the bug tag, never strip the feature tag, and
   silently exclude them from stale nudges. Using the existing feature channel avoids all three.
3. **Web Phase 2 (digest, no more data_error/store_request threads) requires zero bot changes
   to keep working.** All bot sweeps iterate Discord threads, not DB rows — thread-less
   requests are invisible to the bot by construction. The only bot work is deleting dead
   message templates.
4. **Cascade parity risk for the web digest reimplementation:** the web plan describes the
   mention set as a flat union (direct ∪ child-metro ∪ regional). The bot's actual behavior is
   a **tiered cascade with precedence** — regional admins are only pinged when tier 1 has no
   *mentionable* member. Implementing the union as written would ping regionals for covered
   scenes, which the bot deliberately never does. Full spec in §4.
5. **Legacy threads keep working unchanged.** One gap to verify web-side: threads for rows
   resolved in-app only auto-archive if a completion tag gets applied — the bot's archiver is
   tag-driven, and the bot itself only tags on reaction. `resolveDiscordThread` should apply
   the channel's resolve tag, not just post a message.
   **CLOSED 2026-07-28** (digilab-web `c40bd57`): `getResolutionTarget` now returns the
   channel's completion-tag plan alongside the webhook, and `resolveDiscordThread` applies it
   (Resolved / Onboarded / Fixed, `Won't Fix` for rejections in #bug-reports). Status-tag
   stripping is implemented but **conditional**: it only happens for tags whose IDs are set
   web-side, and those four vars are documented as optional there because stripping is purely
   cosmetic — `nudge.py` tests `tag_ids & DONE_TAGS` first and `archiver.py` gates on the
   completion tag's presence, so the completion tag alone is what actually retires a thread.
   Expect resolved threads to keep showing their New / Under Review tag unless those optional
   IDs get set. Investigation found web-side tagging had never run **at all** — the old code
   read one `DISCORD_TAG_RESOLVED` that was never set in that repo — so this bot had been the
   only thing tagging threads. Detail in §6.
6. **Digest channel: keep the bot read-only.** Concur with the default; reasoning in §5.

---

## 1. Bot behavior map per request type

React-to-resolve, archiving, nudging, and manual-thread welcomes are all keyed off the
**parent forum channel** (`config.FORUM_CHANNELS`, `config.py:55-82`). Only the instruction
template posted on app-created threads is keyed off `request_type`
(`thread_watcher.py:47-48` → `messages.app_thread_message`, `messages.py:68-76`).
`grep` confirms `messages.py` is the *only* module with request-type-specific logic.

| Request type | Channel today | Instructions template | Mention on create | React-to-resolve | Nudge | Archive |
|---|---|---|---|---|---|---|
| `store_request` | #scene-coordination | `messages.py:9-17` | cascade (`thread_watcher.py:68-77`) | yes, "Resolved" | **no** (channel not in `NUDGE_CHANNELS`) | via `TAG_RESOLVED` |
| `data_error` (scene resolved) | #scene-coordination | `messages.py:19-28` | cascade | yes, "Resolved" | **no** | via `TAG_RESOLVED` |
| `data_error` (no scene) | #bug-reports | `messages.py:55-64` | global admins (`thread_watcher.py:77`) | yes, "Fixed" | yes | via `TAG_FIXED` |
| `scene_request` | #scene-requests | `messages.py:31-42` | global admins (rows have no scene) | yes, "Onboarded" | yes | via `TAG_ONBOARDED` |
| `bug_report` | #bug-reports | `messages.py:45-53` | global admins | yes, "Fixed" | yes | via `TAG_FIXED` |
| `feature_request` (new) | — (manual threads only today) | **none** → generic fallback (`thread_watcher.py:50-57`) | would follow same scene/global logic | works day one (see §2) | **not covered** (`nudge.py:24-35`) | via `TAG_SHIPPED` |

Channel-level behaviors that continue regardless of request types:
- Manual (non-app) threads: welcome message + auto "New" tag + global-admin ping for
  scene-requests/bug-reports (`thread_watcher.py:115-159`), tag-only react-to-resolve
  (`reactions.py:132-176`).
- Stale nudge/auto-archive loop covers only `#bug-reports` and `#scene-requests`
  (`nudge.py:24-35`); resolved-thread archiver covers all four forums (`archiver.py:16-24, 46`).
- Weekly scene-health digest posts threads into #scene-coordination (`cogs/digest.py`) —
  unrelated to the new daily request digest despite the name.

### Dead code once Phase 2 ships

- `messages.py:9-28` — `store_request` + `data_error` templates for scene_coordination.
- `messages.py:55-64` — `data_error` fallback template for bug_reports.
- Stale comment `thread_watcher.py:64-67` ("data errors with no resolved scene").

That's it. Everything else stays live: the cascade is used by `/admins`, the nudge loop, and
the weekly scene-health digest; #scene-coordination stays in `FORUM_CHANNELS` for manual
threads, legacy threads, and the health digest.

## 2. React-to-resolve: fully generic, channel-gated

Flow (`cogs/reactions.py:20-61`): ✅ emoji only (`:21`) → must be a thread (`:36-43`) → parent
must be in `FORUM_CHANNELS` (`:46`) → reaction must be on the starter message, i.e.
`message_id == thread.id` (`:49-51`) → row lookup by `discord_thread_id = str(thread.id)`
(`db.py:229-237`). Row found → app path: skip if already resolved (`:72`), permission check by
**scene** (platform role, or scene access via `get_admin_scenes_for_user`; scene-less requests
resolvable by any active admin — `:80-106`), then `UPDATE admin_requests SET
status='resolved', resolved_at, resolved_by` (`db.py:240-251`), apply the channel's resolve
tag + strip the channel's initial tags (`:187-210`), confirm in-thread, log.

> **Superseded 2026-08-13 by PR 4** (see the forward-plan doc). The permission check is now
> `db.get_admin_access_for_user(pool, discord_user_id, request.game_id)` and branches on an
> explicit access level, per game. Two changes to the description above: the Discord Platform
> Admin role is no longer a bypass (it is one flat badge across games, so the DB decides), and
> a scene-less request is resolvable by an active admin **of that request's game**, not by any
> active admin.

No `request_type` reference anywhere in the path. **`feature_request` works with zero bot
changes** the moment the web app writes `discord_thread_id` back, *provided* the thread lands
in a registered forum channel. What channel choice affects:

- **Existing `#feature-requests` (recommended):** resolve label "Shipped", `TAG_SHIPPED`
  applied, `New`/`Planned` stripped (`config.py:75-81`). Only gap: no app-thread instruction
  template, so the generic fallback posts "React ✅ … to mark this as shipped" — fine on day
  one, proper template is a 10-line add.
- **`#bug-reports` (web plan as written):** resolves as "Fixed" with `TAG_FIXED`; a
  `DISCORD_TAG_FEATURE` tag would never be stripped (not in the channel's `initial_tags`,
  `config.py:71`); nudge loop skips any thread whose tags don't intersect the bug status tags
  (`nudge.py:85`), so feature threads would never be nudged. Avoid.

## 3. Thread↔row matching and thread-less requests

- Matching is **thread-ID equality only**: `admin_requests.discord_thread_id =
  str(thread.id)` (`db.py:234-236`), with the starter-message guard making the thread ID also
  the message ID. No channel→type assumptions in the lookup; the channel whitelist is just a
  gate. The nudge loop does the same per-thread lookup for mention targeting (`nudge.py:174`).
- **Nothing iterates `admin_requests` expecting threads to exist.** Archiver (`archiver.py:46-51`)
  and nudge (`nudge.py:69-74`) iterate live Discord forum threads and work backwards; the
  weekly health digest queries scenes/stores, not requests. Thread-less digest-handled
  requests are simply never seen by the bot — no crashes, no dead pings.
- `/requests` and `/mystats` query `admin_requests` directly and are type-agnostic
  (`db.py:274-287`, `341-353`; `commands.py:193` title-cases whatever types exist). Post-redesign
  they'll correctly include thread-less data errors/store requests and `feature_request` rows
  with no changes — `/requests` actually becomes the only *Discord* surface showing digest-only
  backlog between digests, which is a nice property.

## 4. The mention cascade, precisely (source of truth for the web reimplementation)

Two functions: `db.get_admins_for_scene` (`db.py:86-177`, one UNION query) and
`db.select_tier_admins` (`db.py:180-197`, precedence). Scene-less requests bypass the cascade
entirely → `db.get_global_admin_discord_ids` (`db.py:254-271`).

Tables: `admin_user_scenes`, `admin_users`, `"user"` (joined via `legacy_admin_id`),
`game_admin_roles` (**hardcoded `game_id = 'digimon'`**), `scenes`, `admin_regions`.

- **Tier 1 — scene:** direct assignees (`admin_user_scenes.scene_id = $1`, active only)
  **∪** child-metro admins when the scene is a `state`-type rollup (metros matched on
  `country` + `state_region`). **Country rollups deliberately do NOT fan out** to child metros
  (mass-ping prevention, `db.py:92-97`).
- **Tier 2 — regional:** `admin_regions.country = scene.country AND (ar.state_region IS NULL
  OR ar.state_region = scene.state_region)`.
- **Tier 3 — global:** super + platform admins via `admin_users.role IN
  ('super_admin','platform_admin')` **OR** `"user".is_super_admin` / `is_platform_admin`.
- **Precedence (`select_tier_admins`):** return the lowest tier having ≥1 member with a
  **non-null `discord_user_id`**. So: covered metro → regionals/globals never pinged; but a
  scene whose only admin has no linked Discord falls through to tier 2. "Uncovered items fall
  to Michael" in the web doc is really "fall to *all* super+platform admins."
- **Role resolution** everywhere is `COALESCE(is_super_admin → 'super_admin',
  is_platform_admin → 'platform_admin', game_admin_roles.role, admin_users.role)`; only
  `is_active = TRUE` rows count.
- **Formatting:** raw `<@{discord_user_id}>`, deduped by ID and against users already
  mentioned in the webhook starter message (`thread_watcher.py:81-95`); the reporter is
  additionally pinged if found in the guild by username match (`thread_watcher.py:98-107`).

**Parity checklist for the web digest implementation** (each is a real divergence risk):
1. Tiered precedence, not a flat union — the plan's wording (`§Phase 2`, "direct ∪
   child-metro ∪ regional") reads as a union.
2. "Covered" means *has a mentionable Discord ID*, not *has an admin row*.
3. State rollups fan out to child metros; country rollups don't.
4. Regional match is country + nullable-state, per above.
5. Global fallback = all super+platform admins, from either legacy role or user flags.
6. The bot is `game_id='digimon'`-hardcoded; the web side is multi-game (`gameId`-first
   convention) — decide explicitly whether digest cascades are per-game.
   **Schema landed 2026-07-28, decision still deliberately deferred.** `admin_user_scenes` and
   `admin_regions` now carry `game_id` (digilab-web `cc450f7`, every row `'digimon'`), so the
   cascade *can* be scoped — but neither side does it yet, on purpose. `sceneHasAreaAdmin`
   (web) and `get_admins_for_scene` (bot) both stay unfiltered so they cannot desync: a
   predicate added on one side only would change who gets pinged on one side only, which is
   the exact failure this checklist exists to prevent. They get filtered **together**, at web
   Phase 5 and bot PR 4. Until then, treat "unfiltered" as the agreed contract, not an
   oversight.
7. Also note the plan anchors the cascade to `sceneHasAreaAdmin` (`discord.ts:104`) — that's
   a boolean coverage check; this bot's pair of functions is the behavioral source of truth.

## 5. Should the bot touch the new digest channel? No.

Concur with read-only, for concrete mechanical reasons, not just surface-area taste:

- React-to-resolve's anchor is *thread starter message ID == thread ID ==
  `discord_thread_id`*. Digest items are lines inside one webhook message in a plain text
  channel — there is no per-item message, no thread, and no DB column to match on. Supporting
  per-item reactions would require the web app to persist a message↔request mapping and the
  bot to grow a second resolution path against it, duplicating the admin UI's one-click
  Apply & Resolve (which is the whole point of Phase 1/2).
- The bot already ignores the digest channel for free: every listener gates on the
  `FORUM_CHANNELS` whitelist. Zero config needed.
- If stale items need more pull than a mention, put admin deep links in the digest
  (web-side, already planned) rather than making Discord a write surface again.

## 6. Other impacts, config, and open items

- **New env vars: none for the bot.** `DISCORD_WEBHOOK_ADMIN_DIGEST` is web-only. If Phase 4
  targets the existing feature forum, `DISCORD_TAG_FEATURE` (web) should be set to the value
  of the bot's existing `DISCORD_TAG_NEW_FEATURE_REQUESTS`; the bot needs no new IDs. Only the
  rejected bug-channel variant would force new bot config.
- **Legacy-thread archiving gap — CLOSED 2026-07-28** (digilab-web `c40bd57`; was: "verify
  web-side"). The archiver only archives threads carrying a completion tag
  (`archiver.py:56-59`), and the bot only applies those tags on reaction. The web app now
  applies the channel's completion tag on every in-app resolve, via `getResolutionTarget`
  (a channel map mirroring our `FORUM_CHANNELS`), and its apply action tolerates rows we
  already resolved by ✅ — it still performs the entity write and only no-ops the status
  write, so the data fix cannot evaporate. Two findings worth keeping: web-side tagging had
  **never run once** (the old code read a single `DISCORD_TAG_RESOLVED` that was unset in that
  repo, so every in-app resolve left its thread untagged), and their old merge was
  `[...current, tag].slice(0, 5)`, which dropped the tag it was adding on any thread already
  at five. In-app-first resolution was otherwise clean all along: a later ✅ reaction is a
  no-op (`reactions.py:72`). **No further web-side verification owed here** — only the deploy
  step of copying `DISCORD_TAG_RESOLVED` / `_ONBOARDED` / `_FIXED` from our env into theirs.
- **Who applies the New tag to app-created feature threads?** The bot only auto-tags *manual*
  threads (`thread_watcher.py:122-138`); for app threads today the webhook presumably sets
  tags. Web Phase 4 should apply `New` (existing tag) at thread creation so resolve-time tag
  stripping and any future nudge coverage behave like bugs/scenes.
- **Nudge coverage decision for features:** `#feature-requests` isn't in `NUDGE_CHANNELS`.
  If features should get the 3d/21d/30d stale lifecycle, add the channel with status set
  `{TAG_NEW_FEATURE_REQUESTS}` (a `Planned` tag means "accepted, waiting on dev" and shouldn't
  nudge; `Shipped`/`Not Planned`/`On Hold` are already in `DONE_TAGS`, `nudge.py:38-46`).
  Default suggestion: leave un-nudged initially; feature requests have no SLA.
- **Docs to update at ship time:** `README.md`/`DESIGN.md` request-flow descriptions,
  `CLAUDE.md` cog descriptions if templates are removed, `.env.example` comments for the
  scene-coordination tags ("added when request resolved" stays true only for manual/legacy).
- **Pre-existing quirk noticed (unrelated, verify):** `on_thread_create` has no
  bot-author check, so the bot's own weekly health-digest threads in #scene-coordination
  should be receiving the manual "Welcome to Scene Coordination!" message
  (`thread_watcher.py:21-38` → `:115`). Worth confirming on the live server and adding an
  `thread.owner_id == bot.user.id` guard sometime.

## 7. Phased change plan (bot-side), aligned to web phases

**With web Phase 1 (Apply & Resolve):** no bot changes. Watch item: resolve-tag application on
legacy threads (above).

**With web Phase 2 (digest + thread shutoff) — Bot PR #1, cleanup only, can lag the web
deploy safely** (if web ships first, threads simply stop being created and the watcher stops
firing; nothing errors):
- Delete `messages.py:9-28` and `:55-64`; fix the `thread_watcher.py:64-67` comment.
- Keep #scene-coordination in `FORUM_CHANNELS` (manual threads, legacy pending threads, weekly
  health digest all still need it).
- Doc updates.

**With web Phase 4 (feature requests) — Bot PR #2, ship before or with the web change:**
- Add `_APP_MESSAGES["feature_requests"]["feature_request"]` instruction template
  (mirrors the bug template with Shipped semantics). Without it the generic fallback is
  correct but terse — react-to-resolve itself works day one with no bot deploy.
- Coordinate web-side: post to the existing `#feature-requests` forum; reuse
  `TAG_NEW_FEATURE_REQUESTS`; write back `discord_thread_id` exactly like bugs.
- Optional: nudge coverage per §6 (default: skip).

**Web Phases 3 & 5:** no bot impact. Better scene resolution on store requests improves
`scene_id` fill rates (helps web digest mentions, no bot effect since those types stop
threading). Phase 5's `game_admin_roles`/`admin_user_scenes` writes flow to Discord
automatically via the role-sync loop within 5 minutes — no changes needed.
