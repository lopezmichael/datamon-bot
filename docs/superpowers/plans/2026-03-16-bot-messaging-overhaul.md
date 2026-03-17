# Bot Messaging Overhaul — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Overhaul the datamon-bot thread watcher to post channel-specific, context-aware messages for both app-created and manually created forum threads.

**Architecture:** New `messages.py` module holds all message templates. `config.py` gets a `channel_type` key in `FORUM_CHANNELS`. Thread watcher branches on DB lookup result (app vs manual) and `request_type` to select the right message. Admin mention logic is preserved but only fires for app-created threads.

**Tech Stack:** Python 3.12+, discord.py 2.4.x, asyncpg 0.30.x

**Design spec:** `docs/superpowers/specs/2026-03-16-webhook-and-bot-messaging-design.md` (sections 2.1–2.6)

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `messages.py` | Create | All bot message templates — app-created and manual thread instructions per channel |
| `config.py` | Modify (line 44-49) | Add `channel_type` key to each `FORUM_CHANNELS` entry |
| `cogs/thread_watcher.py` | Modify (lines 20-82) | Branch on app vs manual, use `messages.py` builders, conditional admin mentions |

---

## Task 1: Add `channel_type` to `FORUM_CHANNELS` config

**Files:**
- Modify: `config.py:44-49`

- [ ] **Step 1: Update `FORUM_CHANNELS` dict**

Replace lines 44-49 in `config.py`:

```python
# Forum channel → resolve config mapping
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

- [ ] **Step 2: Verify bot still starts**

Run: `source .venv/bin/activate && python bot.py`
Expected: Bot connects, all 5 cogs load, no errors. Ctrl+C to stop.

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add channel_type to FORUM_CHANNELS config"
```

---

## Task 2: Create `messages.py` — App-Created Thread Messages

**Files:**
- Create: `messages.py`

These are the messages posted when a thread has a matching `admin_requests` record (created via the DigiLab app). The webhook embed already provides context, so the bot focuses on actionable next steps.

- [ ] **Step 1: Create `messages.py` with app-created message builders**

Create `messages.py` in the project root:

```python
"""Datamon Bot — Message templates for forum thread responses."""

# ---------------------------------------------------------------------------
# App-created thread messages (thread has a matching admin_requests record)
# ---------------------------------------------------------------------------

_APP_MESSAGES: dict[str, dict[str, str]] = {
    "scene_coordination": {
        "store_request": (
            "\U0001f4cb **Store Request — Action Needed**\n"
            "\n"
            "A new store has been requested for this scene. Tagged admins, please:\n"
            "1. Verify the store exists and is running Digimon TCG events\n"
            "2. Check if this store is already listed under a different name\n"
            "3. React \u2705 on the first message when this has been handled\n"
            "\n"
            "If you need more info from the requester, reply in this thread."
        ),
        "data_error": (
            "\U0001f50d **Data Error — Review Needed**\n"
            "\n"
            "A data error has been reported for this scene. Tagged admins, please:\n"
            "1. Review the error details above\n"
            "2. Make the correction in the admin panel if confirmed\n"
            "3. React \u2705 on the first message when this has been fixed\n"
            "\n"
            "If you can't reproduce the issue, ask the reporter for more details in this thread."
        ),
    },
    "scene_requests": {
        "scene_request": (
            "\U0001f30d **New Scene Request — Triage Needed**\n"
            "\n"
            "Someone wants to bring DigiLab to a new area! Platform admins, please:\n"
            "1. Check if this area overlaps with an existing scene\n"
            "2. Determine if there's enough local activity to warrant a new scene\n"
            "3. If approved, create the scene and assign an admin\n"
            "4. React \u2705 on the first message when this has been handled\n"
            "\n"
            "If you need more info, reach out to the requester in this thread "
            "\u2014 their Discord is listed above if provided."
        ),
    },
    "bug_reports": {
        "bug_report": (
            "\U0001f41b **Bug Report — Triage Needed**\n"
            "\n"
            "A bug has been reported. Platform admins, please:\n"
            "1. Try to reproduce using the context above\n"
            "2. Prioritize and track in our issue tracker if confirmed\n"
            "3. React \u2705 on the first message when this has been addressed\n"
            "\n"
            "If you need more details, ask the reporter in this thread."
        ),
        "data_error": (
            "\U0001f50d **Data Error — Review Needed**\n"
            "\n"
            "A data error was reported but couldn't be routed to a specific scene. "
            "Platform admins, please:\n"
            "1. Identify which scene this belongs to\n"
            "2. Review and correct the error if confirmed\n"
            "3. React \u2705 on the first message when this has been fixed"
        ),
    },
}


def app_thread_message(channel_type: str, request_type: str) -> str | None:
    """Get the bot's instructions for an app-created thread.

    Returns None if no message is defined for this channel_type + request_type combo.
    """
    channel_messages = _APP_MESSAGES.get(channel_type)
    if not channel_messages:
        return None
    return channel_messages.get(request_type)
```

- [ ] **Step 2: Verify the module loads**

Run: `python -c "import messages; print(messages.app_thread_message('scene_coordination', 'store_request')[:40])"`
Expected: Prints `📋 **Store Request — Action Needed**`

- [ ] **Step 3: Commit**

```bash
git add messages.py
git commit -m "feat: add messages.py with app-created thread templates"
```

---

## Task 3: Add Manual Thread Messages to `messages.py`

**Files:**
- Modify: `messages.py`

These fire when someone creates a thread directly in Discord (no DB record). They provide channel-specific welcome/guidance.

- [ ] **Step 1: Add manual thread messages and the public function**

Append to `messages.py`, after the `app_thread_message` function:

```python
# ---------------------------------------------------------------------------
# Manual thread messages (no admin_requests record — user posted directly)
# ---------------------------------------------------------------------------

_MANUAL_MESSAGES: dict[str, str] = {
    "scene_coordination": (
        "\U0001f44b **Welcome to Scene Coordination!**\n"
        "\n"
        "This channel is for scene admins to discuss anything related to managing "
        "their scenes \u2014 data corrections, reorganizing scenes, general questions, "
        "or anything else.\n"
        "\n"
        "**Tips:**\n"
        "\u2022 Tag the relevant scene admins if you need their attention \u2014 "
        "check `/admins <scene>` to find them\n"
        "\u2022 For data errors, the fastest route is the **Report Error** button "
        "in the app \u2014 it creates a tracked request and notifies the right admins "
        "automatically\n"
        "\u2022 When your question or issue is resolved, react \u2705 on the first "
        "message to mark it done"
    ),
    "scene_requests": (
        "\U0001f44b **Welcome to Scene Requests!**\n"
        "\n"
        "This channel is for requesting new scenes or communities on DigiLab.\n"
        "\n"
        "**To help us process your request, please include:**\n"
        "\u2022 The city or region you'd like to add\n"
        "\u2022 Any stores or communities running Digimon TCG events there\n"
        "\u2022 Your Discord handle so we can follow up\n"
        "\n"
        "**Tip:** You can also submit scene requests directly through the app at "
        "https://app.digilab.cards \u2014 it automatically notifies the right people "
        "and tracks the request.\n"
        "\n"
        "A platform admin will review your request and follow up here."
    ),
    "bug_reports": (
        "\U0001f44b **Thanks for reporting a bug!**\n"
        "\n"
        "To help us track this down, please make sure you've included:\n"
        "\u2022 What you were doing when it happened\n"
        "\u2022 What you expected vs what actually happened\n"
        "\u2022 The page/tab you were on and which scene (if applicable)\n"
        "\n"
        "**Tip:** The **Report a Bug** button in the app auto-fills context and "
        "creates a tracked request \u2014 it's the fastest way to get a fix.\n"
        "\n"
        "A platform admin will triage this and follow up here."
    ),
    "feature_requests": (
        "\U0001f44b **Thanks for the feature idea!**\n"
        "\n"
        "To help us evaluate your suggestion, consider including:\n"
        "\u2022 What problem this would solve for you or your community\n"
        "\u2022 How you'd expect it to work\n"
        "\u2022 How important this is relative to other things you'd like to see\n"
        "\n"
        "Platform admins review feature requests regularly. "
        "Community discussion and upvotes (reactions) help us prioritize!"
    ),
}


def manual_thread_message(channel_type: str) -> str | None:
    """Get the bot's welcome message for a manually created thread.

    Returns None if no message is defined for this channel_type.
    """
    return _MANUAL_MESSAGES.get(channel_type)
```

- [ ] **Step 2: Verify both functions work**

Run: `python -c "import messages; print(messages.manual_thread_message('feature_requests')[:40])"`
Expected: Prints `👋 **Thanks for the feature idea!**`

Run: `python -c "import messages; print(messages.manual_thread_message('unknown'))"`
Expected: Prints `None`

- [ ] **Step 3: Commit**

```bash
git add messages.py
git commit -m "feat: add manual thread welcome messages to messages.py"
```

---

## Task 4: Overhaul `cogs/thread_watcher.py`

**Files:**
- Modify: `cogs/thread_watcher.py` (full rewrite of `on_thread_create` method, lines 20-82)

The thread watcher needs to:
1. Check if the thread is app-created (DB record exists) or manual (no record)
2. Post the appropriate message from `messages.py`
3. Only post admin mentions for app-created threads

- [ ] **Step 1: Rewrite `cogs/thread_watcher.py`**

Replace the entire file content:

```python
"""Thread watcher: post instructions and tag admins on new forum threads."""

import asyncio
import logging

import discord
from discord.ext import commands

import config
import db
import messages

log = logging.getLogger(__name__)


class ThreadWatcher(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        # Only watch tracked forum channels
        if thread.parent_id not in config.FORUM_CHANNELS:
            return

        # Wait briefly for the webhook's first message to be posted
        await asyncio.sleep(2)

        forum_config = config.FORUM_CHANNELS[thread.parent_id]
        channel_type = forum_config["channel_type"]

        # Check if this is an app-created thread (has a DB request record)
        request = await db.get_request_by_thread(self.bot.pool, str(thread.id))

        if request:
            await self._handle_app_thread(thread, channel_type, request)
        else:
            await self._handle_manual_thread(thread, channel_type)

    async def _handle_app_thread(
        self,
        thread: discord.Thread,
        channel_type: str,
        request: dict,
    ) -> None:
        """Post instructions and admin mentions for app-created threads."""
        request_type = request["request_type"]
        instructions = messages.app_thread_message(channel_type, request_type)

        if not instructions:
            # Fallback: unknown request_type in this channel
            label = config.FORUM_CHANNELS[thread.parent_id]["label"]
            instructions = (
                f"\U0001f4cb **New request received!**\n"
                f"React \u2705 on the first message to mark this as {label.lower()}."
            )

        try:
            await thread.send(instructions)
        except discord.Forbidden:
            log.warning("Cannot send instructions to thread %s", thread.id)
            return

        # Post admin mentions if the request has a scene
        if not request["scene_id"]:
            return

        admins = await db.get_admins_for_scene(self.bot.pool, request["scene_id"])
        if not admins:
            return

        # Check who's already mentioned in the webhook's first message
        already_mentioned: set[str] = set()
        try:
            starter = await thread.fetch_message(thread.id)
            already_mentioned = {str(u.id) for u in starter.mentions}
        except Exception:
            pass

        # Build mention list for admins not already tagged
        mentions = []
        for admin in admins:
            if admin["discord_user_id"] and admin["discord_user_id"] not in already_mentioned:
                mentions.append(f"<@{admin['discord_user_id']}>")

        # Also check if the requester is in the server
        if request["discord_username"]:
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild:
                requester = discord.utils.find(
                    lambda m: m.name == request["discord_username"]
                    or str(m) == request["discord_username"],
                    guild.members,
                )
                if requester and str(requester.id) not in already_mentioned:
                    mentions.append(requester.mention)

        if mentions:
            try:
                await thread.send(" ".join(mentions))
            except discord.Forbidden:
                pass

    async def _handle_manual_thread(
        self,
        thread: discord.Thread,
        channel_type: str,
    ) -> None:
        """Post welcome/guidance for manually created threads."""
        welcome = messages.manual_thread_message(channel_type)

        if not welcome:
            return

        try:
            await thread.send(welcome)
        except discord.Forbidden:
            log.warning("Cannot send welcome to thread %s", thread.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ThreadWatcher(bot))
```

- [ ] **Step 2: Verify bot starts and cog loads**

Run: `source .venv/bin/activate && python bot.py`
Expected: Log shows `Loaded cog: cogs.thread_watcher` with no errors. Ctrl+C to stop.

- [ ] **Step 3: Commit**

```bash
git add cogs/thread_watcher.py
git commit -m "feat: overhaul thread watcher with channel-specific messages for app and manual threads"
```

---

## Task 5: Integration Testing

No automated test suite exists — verification is manual against the live Discord server. Start the bot locally and run through each scenario.

- [ ] **Step 1: Start the bot**

Run: `source .venv/bin/activate && python bot.py`
Expected: Bot connects, all 5 cogs load, command tree synced.

- [ ] **Step 2: Test app-created store request**

1. Go to DigiLab app → submit a test store request for an existing scene
2. Check `#scene-coordination` for:
   - Blue embed from webhook (digilab-app side — already verified)
   - Bot posts: "📋 **Store Request — Action Needed**" with 3-step checklist
   - Bot posts admin @mentions (separate message)

- [ ] **Step 3: Test app-created data error**

1. Go to DigiLab app → report a data error for an item in a specific scene
2. Check `#scene-coordination` for:
   - Orange embed from webhook
   - Bot posts: "🔍 **Data Error — Review Needed**" with 3-step checklist
   - Bot posts admin @mentions

- [ ] **Step 4: Test app-created data error (no scene fallback)**

1. Report a data error from a context without an active scene
2. Check `#bug-reports` for:
   - Orange embed from webhook
   - Bot posts: "🔍 **Data Error — Review Needed**" with the "couldn't be routed to a specific scene" variant

- [ ] **Step 5: Test app-created scene request**

1. Submit a "my area isn't listed" scene request via the app
2. Check `#scene-requests` for:
   - Purple embed from webhook
   - Bot posts: "🌍 **New Scene Request — Triage Needed**" with 4-step checklist

- [ ] **Step 6: Test app-created bug report**

1. Submit a bug report via the app
2. Check `#bug-reports` for:
   - Red embed from webhook
   - Bot posts: "🐛 **Bug Report — Triage Needed**" with 3-step checklist

- [ ] **Step 7: Test manual thread in `#scene-coordination`**

1. Manually create a new thread in `#scene-coordination`
2. Bot posts: "👋 **Welcome to Scene Coordination!**" with tips about `/admins`, Report Error button, and ✅ reactions
3. Bot does NOT post admin mentions

- [ ] **Step 8: Test manual thread in `#scene-requests`**

1. Manually create a new thread in `#scene-requests`
2. Bot posts: "👋 **Welcome to Scene Requests!**" with info checklist and app link
3. Bot does NOT post admin mentions

- [ ] **Step 9: Test manual thread in `#bug-reports`**

1. Manually create a new thread in `#bug-reports`
2. Bot posts: "👋 **Thanks for reporting a bug!**" with info checklist and app tip
3. Bot does NOT post admin mentions

- [ ] **Step 10: Test manual thread in `#feature-requests`**

1. Manually create a new thread in `#feature-requests`
2. Bot posts: "👋 **Thanks for the feature idea!**" with evaluation criteria and upvote note
3. Bot does NOT post admin mentions

- [ ] **Step 11: Test react-to-resolve still works**

1. On one of the app-created test threads, react ✅ on the first message (as an admin)
2. Bot posts "✅ **Resolved** by @you"
3. Thread gets the appropriate tag
4. `#bot-log` gets an entry

- [ ] **Step 12: Commit final state (if any tweaks were made during testing)**

```bash
git add -A
git commit -m "fix: adjustments from integration testing"
```
