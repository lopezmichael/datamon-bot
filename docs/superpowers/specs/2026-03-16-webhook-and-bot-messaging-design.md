# Webhook & Bot Messaging — Design Spec

**Date:** 2026-03-16
**Status:** Draft
**Scope:** digilab-app webhook improvements + datamon-bot thread watcher overhaul
**Repos:** `digilab-app` (webhook changes), `datamon-bot` (bot response changes)

---

## Problem

The current system has gaps:

1. **Bot messages are generic** — the same "React ✅ to mark as resolved" instruction is posted in every channel regardless of context
2. **Manual Discord posts get wrong instructions** — if someone creates a thread directly in a forum channel (not via the app), the bot still says "New request received!" even though there's no DB request to resolve
3. **Webhook messages lack richness** — plain text, no embeds, no request IDs, inconsistent @mentions across request types
4. **Scene requests and bug reports don't mention anyone** — only store requests and data errors tag admins
5. **Scene update announcements are broken** — `discord_post_scene_update()` was disconnected during a recent cleanup and is dead code
6. **Feature requests channel has no bot guidance** — `#feature-requests` is Discord-only with no bot support for manual posts

---

## Design Decisions

- **Embeds for all webhook messages** — color-coded by type, structured fields, request ID in footer
- **Channel-specific bot responses** — each channel gets tailored instructions for both app-created and manual threads
- **App-created vs manual detection** — bot checks `db.get_request_by_thread()` after the 2-second wait; presence of a record means app-created
- **Feature requests stays Discord-only** — no in-app form, bot provides guidance on manual posts only
- **No deep links for now** — admins are scene-scoped so generic app links aren't useful; revisit if requested
- **Messages module** — new `messages.py` in datamon-bot with builder functions, keeping cog logic clean

---

## Part 1: digilab-app Webhook Improvements

All changes are in `R/discord_webhook.R` and its call sites. The webhook URL call sites in server files don't change — only the JSON payload structure changes inside each `discord_post_*` function.

### 1.1 Embed Format (All Webhooks)

Switch from plain `content` text to Discord embeds. Every webhook message becomes:

```json
{
  "embeds": [{
    "title": "...",
    "description": "...",
    "color": <int>,
    "fields": [...],
    "footer": { "text": "Request #42 • Submitted via DigiLab" },
    "timestamp": "2026-03-16T14:30:00Z"
  }],
  "content": "<@mention1> <@mention2>"
}
```

**Key points:**
- `content` is used only for @mentions (mentions don't trigger notifications inside embeds)
- `footer` includes the `admin_requests.id` for cross-referencing
- `timestamp` uses ISO 8601 — Discord renders it in the viewer's local timezone
- `color` is an integer, not hex string

### 1.2 Color Coding

| Request Type | Color | Hex | Int |
|---|---|---|---|
| Store Request | Blue | `#5865F2` | `5793266` |
| Scene Request | Purple | `#9B59B6` | `10181046` |
| Data Error | Orange | `#E67E22` | `15105570` |
| Bug Report | Red | `#E74C3C` | `15158332` |
| Scene Update | Green | `#57F287` | `5763719` |
| Resolution | Green | `#2ECC71` | `3066993` |
| Rejection | Dark Grey | `#95A5A6` | `9807270` |

### 1.3 Store Request Embed

**Channel:** `#scene-coordination`
**Trigger:** User submits store request via public stores page

```
Embed:
  title: "New Store Request"
  color: 5793266 (blue)
  fields:
    - name: "Store"      value: "{store_name}"         inline: true
    - name: "Location"   value: "{city_state}"          inline: true
    - name: "Scene"      value: "{scene_display_name}"  inline: true
    - name: "Discord"    value: "{discord_username}"     inline: true  (if provided)
  footer: "Request #{id} • Submitted via DigiLab"
  timestamp: ISO 8601

content: "<@admin1> <@admin2>"  (scene admins — already implemented)
```

**Changes from current:**
- Plain text → embed
- Add `discord_username` field (currently saved to DB but not included in message)
- Add request ID in footer
- Add ISO timestamp (replaces formatted string in body)

### 1.4 Scene Request Embed

**Channel:** `#scene-requests`
**Trigger:** User submits new scene/community request

```
Embed:
  title: "New Scene Request"
  color: 10181046 (purple)
  fields:
    - name: "Store/Community"  value: "{store_name}"         inline: true
    - name: "Location"         value: "{location}"            inline: true
    - name: "Discord"          value: "{discord_username}"    inline: true  (if provided)
  footer: "Request #{id} • Submitted via DigiLab"
  timestamp: ISO 8601

content: "<@super_admin1> <@super_admin2>"  (NEW — mention all super_admins)
```

**Changes from current:**
- Plain text → embed
- **Add super_admin mentions** — scene requests are global (no scene to route to), so ping super_admins who triage them
- Add request ID in footer

**New helper needed:** `get_super_admin_mentions(db_pool)` — queries `admin_users WHERE role = 'super_admin' AND is_active = TRUE AND discord_user_id IS NOT NULL`

### 1.5 Data Error Embed

**Channel:** `#scene-coordination` (falls back to `#bug-reports` if no scene)
**Trigger:** User reports data error via in-app modal

```
Embed:
  title: "Data Error Report"
  color: 15105570 (orange)
  fields:
    - name: "Type"         value: "{item_type}"            inline: true
    - name: "Item"         value: "{item_name}"             inline: true
    - name: "Scene"        value: "{scene_display_name}"    inline: true
    - name: "Description"  value: "{user_description}"      inline: false
    - name: "Discord"      value: "{discord_username}"      inline: true  (if provided)
  footer: "Request #{id} • Submitted via DigiLab"
  timestamp: ISO 8601

content: "<@admin1> <@admin2>"  (scene admins — already implemented)
```

**Changes from current:**
- Plain text → embed
- Add request ID in footer
- Mentions already work for scene-routed errors

**Fallback (no scene):** Same embed but without Scene field, posted to `#bug-reports`, and **add super_admin mentions** (same new helper as scene requests).

### 1.6 Bug Report Embed

**Channel:** `#bug-reports`
**Trigger:** User submits bug report via in-app modal

```
Embed:
  title: "Bug: {title}"
  color: 15158332 (red)
  fields:
    - name: "Description"  value: "{description}"          inline: false
    - name: "Context"      value: "{context}"               inline: true
    - name: "Discord"      value: "{discord_username}"      inline: true  (if provided)
  footer: "Request #{id} • Submitted via DigiLab"
  timestamp: ISO 8601

content: "<@super_admin1> <@super_admin2>"  (NEW — mention super_admins)
```

**Changes from current:**
- Plain text → embed
- **Add super_admin mentions** — bugs are global, no scene admin to route to
- Add request ID in footer

### 1.7 Resolution/Rejection Embed

**Channel:** Existing thread (any forum channel)
**Trigger:** Admin resolves/rejects via admin notifications panel

```
Embed:
  title: "Resolved" or "Rejected"
  color: 3066993 (green) or 9807270 (grey)
  description: "by {admin_username} via DigiLab"
  timestamp: ISO 8601
```

**Changes from current:**
- Plain text → embed
- Add timestamp

### 1.8 Scene Update Announcement — Re-wire

**Channel:** `#scene-updates`
**Trigger:** When a new scene is created (currently dead code — was removed in commit `862b708`)

**Re-wire approach:** Call `discord_post_scene_update()` automatically when a scene record is inserted/activated, rather than relying on a manual button. The trigger point is wherever scenes get created in the admin flow.

```
Embed:
  title: "New Scene: {scene_name}"
  color: 5763719 (green)
  description: "{random_celebratory_template}"
  fields:
    - name: "Location"   value: "{country}, {state_region}"   inline: true
    - name: "Continent"  value: "{continent}"                  inline: true
  footer: "DigiLab"
  timestamp: ISO 8601
```

**Changes from current:**
- Re-wire the trigger (find correct insertion point in admin scene creation flow)
- Plain text → embed
- Add location/continent fields
- Keep the random template selection for variety in the description

### 1.9 New Helper: `get_super_admin_mentions(db_pool)`

```r
get_super_admin_mentions <- function(db_pool) {
  # Query admin_users WHERE role = 'super_admin' AND is_active = TRUE AND discord_user_id IS NOT NULL
  # Return "<@id1> <@id2> ..." or ""
}
```

Used by: scene requests, bug reports, data error fallback (no scene).

---

## Part 2: datamon-bot Thread Watcher Overhaul

### 2.1 New File: `messages.py`

A module containing message builder functions. Each function returns a string (or list of strings for multi-message responses). This keeps message content easy to find and edit without digging through cog logic.

```python
# messages.py — Bot response templates for forum threads

def app_thread_instructions(channel_type: str, request: dict) -> str:
    """Instructions for app-created threads (have a DB request record)."""
    ...

def manual_thread_instructions(channel_type: str) -> str:
    """Instructions for manually created threads (no DB request)."""
    ...
```

### 2.2 Channel Types

Define a `channel_type` string for each tracked forum channel to drive message selection:

| Channel | `channel_type` | Purpose |
|---|---|---|
| `#scene-coordination` | `"scene_coordination"` | Store requests, data errors, general admin questions |
| `#scene-requests` | `"scene_requests"` | New scene/community requests |
| `#bug-reports` | `"bug_reports"` | Bug reports, data errors (fallback) |
| `#feature-requests` | `"feature_requests"` | Feature ideas (Discord-only, no app webhook) |

Add `channel_type` to the `FORUM_CHANNELS` config dict so the thread watcher can pass it to message builders.

### 2.3 App-Created Thread Messages

These fire when `db.get_request_by_thread()` returns a record. The webhook embed already has the context, so the bot focuses on **actionable next steps**.

#### `#scene-coordination` — Store Request

```
📋 **Store Request — Action Needed**

A new store has been requested for this scene. Tagged admins, please:
1. Verify the store exists and is running Digimon TCG events
2. Check if this store is already listed under a different name
3. React ✅ on the first message when this has been handled

If you need more info from the requester, reply in this thread.
```

#### `#scene-coordination` — Data Error

```
🔍 **Data Error — Review Needed**

A data error has been reported for this scene. Tagged admins, please:
1. Review the error details above
2. Make the correction in the admin panel if confirmed
3. React ✅ on the first message when this has been fixed

If you can't reproduce the issue, ask the reporter for more details in this thread.
```

#### `#scene-requests` — Scene Request

```
🌍 **New Scene Request — Triage Needed**

Someone wants to bring DigiLab to a new area! Platform admins, please:
1. Check if this area overlaps with an existing scene
2. Determine if there's enough local activity to warrant a new scene
3. If approved, create the scene and assign an admin
4. React ✅ on the first message when this has been handled

If you need more info, reach out to the requester in this thread — their Discord is listed above if provided.
```

#### `#bug-reports` — Bug Report

```
🐛 **Bug Report — Triage Needed**

A bug has been reported. Platform admins, please:
1. Try to reproduce using the context above
2. Prioritize and track in our issue tracker if confirmed
3. React ✅ on the first message when this has been addressed

If you need more details, ask the reporter in this thread.
```

#### `#bug-reports` — Data Error (fallback, no scene)

```
🔍 **Data Error — Review Needed**

A data error was reported but couldn't be routed to a specific scene. Platform admins, please:
1. Identify which scene this belongs to
2. Review and correct the error if confirmed
3. React ✅ on the first message when this has been fixed
```

**Distinguishing store requests from data errors:** The bot can check `request["request_type"]` from the DB record to determine which message to show within `#scene-coordination`.

### 2.4 Manual Thread Messages

These fire when `db.get_request_by_thread()` returns `None` — someone created a thread directly in Discord.

#### `#scene-coordination`

```
👋 **Welcome to Scene Coordination!**

This channel is for scene admins to discuss anything related to managing their scenes — data corrections, reorganizing scenes, general questions, or anything else.

**Tips:**
• Tag the relevant scene admins if you need their attention — check `/admins <scene>` to find them
• For data errors, the fastest route is the **Report Error** button in the app — it creates a tracked request and notifies the right admins automatically
• When your question or issue is resolved, react ✅ on the first message to mark it done
```

#### `#scene-requests`

```
👋 **Welcome to Scene Requests!**

This channel is for requesting new scenes or communities on DigiLab.

**To help us process your request, please include:**
• The city or region you'd like to add
• Any stores or communities running Digimon TCG events there
• Your Discord handle so we can follow up

**Tip:** You can also submit scene requests directly through the app at https://app.digilab.cards — it automatically notifies the right people and tracks the request.

A platform admin will review your request and follow up here.
```

#### `#bug-reports`

```
👋 **Thanks for reporting a bug!**

To help us track this down, please make sure you've included:
• What you were doing when it happened
• What you expected vs what actually happened
• The page/tab you were on and which scene (if applicable)

**Tip:** The **Report a Bug** button in the app auto-fills context and creates a tracked request — it's the fastest way to get a fix.

A platform admin will triage this and follow up here.
```

#### `#feature-requests`

```
👋 **Thanks for the feature idea!**

To help us evaluate your suggestion, consider including:
• What problem this would solve for you or your community
• How you'd expect it to work
• How important this is relative to other things you'd like to see

Platform admins review feature requests regularly. Community discussion and upvotes (reactions) help us prioritize!
```

### 2.5 Thread Watcher Logic Changes

Updated flow in `cogs/thread_watcher.py`:

```
on_thread_create(thread):
  1. Check if thread.parent_id is in FORUM_CHANNELS → if not, return
  2. Wait 2 seconds (for webhook's first message)
  3. Look up request: db.get_request_by_thread(thread.id)
  4. Determine channel_type from FORUM_CHANNELS config

  IF request exists (app-created):
    a. Determine request_type from record (store_request, data_error, scene_request, bug_report)
    b. Post channel+type-specific instructions from messages.py
    c. Post admin mentions (existing logic, unchanged)

  IF request is None (manual thread):
    a. Post channel-specific welcome/guidance from messages.py
    b. Do NOT post admin mentions (user can tag who they need)
```

### 2.6 Config Changes

Extend `FORUM_CHANNELS` in `config.py` to include `channel_type`:

```python
FORUM_CHANNELS: dict[int, dict] = {
    CHANNEL_SCENE_COORDINATION: {
        "resolve_tag": TAG_RESOLVED,
        "label": "Resolved",
        "channel_type": "scene_coordination",
    },
    CHANNEL_SCENE_REQUESTS: {
        "resolve_tag": TAG_ONBOARDED,
        "label": "Onboarded",
        "channel_type": "scene_requests",
    },
    CHANNEL_BUG_REPORTS: {
        "resolve_tag": TAG_FIXED,
        "label": "Fixed",
        "channel_type": "bug_reports",
    },
    CHANNEL_FEATURE_REQUESTS: {
        "resolve_tag": TAG_SHIPPED,
        "label": "Shipped",
        "channel_type": "feature_requests",
    },
}
```

---

## Part 3: Edge Cases & Community Workflows

### 3.1 React-to-Resolve on Manual Threads

**Problem:** If someone creates a manual thread and another user reacts ✅, the bot currently tries `db.get_request_by_thread()` and returns `None`, so it does nothing. This is correct behavior — no DB request means nothing to resolve.

**But:** Users might expect the ✅ reaction to work on manual threads too, at least for tagging purposes.

**Decision:** Keep current behavior. Manual threads don't have a DB record, so ✅ does nothing. The bot's manual-thread instructions tell users to react ✅ "to mark it done" — the resolve tag won't be applied automatically, but this is acceptable. If we want to support this later, we can add a lightweight "tag-only resolve" path that just applies the completion tag without a DB write.

### 3.2 Duplicate Threads

**Problem:** A user might submit a request via the app AND create a manual Discord thread about the same issue.

**Decision:** No automated deduplication. This is a human triage problem. The app-created thread will have the tracked request; the manual thread can be closed by an admin with a note pointing to the tracked thread.

### 3.3 Data Errors in `#bug-reports` (Fallback)

**Problem:** When a data error has no scene, it falls back to `#bug-reports`. The bot needs to distinguish this from actual bug reports.

**Solution:** Check `request["request_type"]` — if it's `"data_error"`, show the data error message; if `"bug_report"`, show the bug message. Both are in `#bug-reports` but get different instructions.

### 3.4 Thread Watcher Timing

**Problem:** The bot waits 2 seconds for the webhook's first message. If Discord is slow, the webhook message might not be there yet.

**Decision:** Keep the 2-second wait. It's worked so far. If we see issues in production, we can increase it or add a retry.

### 3.5 Admins Without `discord_user_id`

**Problem:** 2 of 86 admins don't have Discord IDs linked (aomceodeadly, gamescornerdigimon — not in the server).

**Impact:** These admins won't be mentioned in webhook messages or bot responses. This is expected and handled — the mention query filters for `discord_user_id IS NOT NULL`.

### 3.6 `admin_regions` Unpopulated

**Problem:** Regional admin table exists but has no rows. Regional mentions will return empty results.

**Impact:** Until regional admins are assigned, only direct scene admins and super_admins get mentioned. No code changes needed — the queries already handle this gracefully.

### 3.7 Feature Requests — No Resolve Flow

**Problem:** `#feature-requests` has a `TAG_SHIPPED` resolve tag and the bot watches for ✅ reactions, but there's no `admin_requests` record for manual feature request threads.

**Decision:** Same as 3.1 — ✅ does nothing on manual threads. Feature request lifecycle is managed manually by platform admins (tag as Planned, In Progress, Shipped, etc.). The bot's welcome message tells users that "platform admins review feature requests regularly."

### 3.8 Cross-Scene Discussions in `#scene-coordination`

**Problem:** Some threads in `#scene-coordination` involve multiple scenes (e.g., Spain admins discussing scene reorganization). The bot's admin mentions are tied to a single scene.

**Decision:** For app-created threads, mentions are scoped to the request's scene — this is correct. For manual threads, the bot's guidance tells users to use `/admins <scene>` to find who to tag. No automated cross-scene mention logic needed.

### 3.9 Bot Edits Own Messages

**Consideration:** Should the bot edit its initial instructions after a thread is resolved (e.g., strike through the checklist)?

**Decision:** No. The ✅ confirmation message and tag are sufficient signal. Editing old messages adds complexity for minimal value.

### 3.10 Welcome DM — Not in Scope

The welcome DM (`discord_send_welcome_dm`) uses the bot token REST API from digilab-app directly. It's not part of the thread watcher flow and doesn't need changes in this effort. Potential improvements (server invite link, scene deep link) are noted but deferred.

---

## Summary of Changes by Repo

### digilab-app (`R/discord_webhook.R` + call sites)

| Change | Scope |
|---|---|
| Convert all `discord_post_*` functions to use embeds | 5 functions |
| Add `get_super_admin_mentions()` helper | New function |
| Add super_admin mentions to scene requests | `discord_post_scene_request()` |
| Add super_admin mentions to bug reports | `discord_post_bug_report()` |
| Add super_admin mentions to data error fallback | `discord_post_data_error()` |
| Add `discord_username` to store request message | `discord_post_to_scene()` |
| Add request ID to all webhook footers | All `discord_post_*` functions — pass `request_id` parameter from call sites |
| Convert resolution message to embed | `discord_resolve_thread()` |
| Re-wire scene update announcement | Find scene creation trigger + call `discord_post_scene_update()` |
| Convert scene update to embed | `discord_post_scene_update()` |

### datamon-bot

| Change | Scope |
|---|---|
| New `messages.py` module | ~10 message builder functions |
| Add `channel_type` to `FORUM_CHANNELS` config | `config.py` |
| Overhaul thread watcher to branch on app vs manual | `cogs/thread_watcher.py` |
| Thread watcher uses `request_type` for sub-routing | `cogs/thread_watcher.py` |

---

## Implementation Order

1. **digilab-app: Embed conversion** — change all webhook functions to use embeds (no new features, just format)
2. **digilab-app: Add mentions** — `get_super_admin_mentions()` + wire into scene requests, bug reports, data error fallback
3. **digilab-app: Request IDs in footers** — pass `request_id` from call sites into webhook functions
4. **digilab-app: Re-wire scene updates** — find trigger, call `discord_post_scene_update()`, convert to embed
5. **datamon-bot: `messages.py`** — write all message templates
6. **datamon-bot: Config update** — add `channel_type` to `FORUM_CHANNELS`
7. **datamon-bot: Thread watcher overhaul** — app vs manual branching, request_type sub-routing
8. **Integration testing** — test all webhook types + bot responses end-to-end
