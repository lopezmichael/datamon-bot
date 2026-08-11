"""Stale thread lifecycle: nudge twice, then auto-archive unresolved threads."""

import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

import config
import db
from utils import TRANSIENT_LOOP_EXCEPTIONS, LoopFailureAlerter, log_to_discord

log = logging.getLogger(__name__)

# Thresholds measured from the last real activity (human reply or webhook
# starter). Our own bot messages — nudges, instructions, resolve confirmations —
# do not reset the clock.
FIRST_NUDGE_AT = timedelta(days=3)
SECOND_NUDGE_AT = timedelta(days=21)
AUTO_ARCHIVE_AT = timedelta(days=30)

# Channels to nudge and their status tags (threads with these tags are "in progress", not done)
NUDGE_CHANNELS: dict[int, set[int]] = {
    config.CHANNEL_BUG_REPORTS: {
        config.TAG_NEW_BUG_REPORTS,
        config.TAG_UNDER_REVIEW_BUG_REPORTS,
        config.TAG_CONFIRMED_BUG_REPORTS,
    },
    config.CHANNEL_SCENE_REQUESTS: {
        config.TAG_NEW_SCENE_REQUESTS,
        config.TAG_NEEDS_MORE_INFO_SCENE_REQUESTS,
        config.TAG_NEEDS_ADMIN_SCENE_REQUESTS,
    },
}

# Completion tags — threads with these are resolved/closed, don't nudge
DONE_TAGS: set[int] = {
    config.TAG_FIXED,
    config.TAG_WONT_FIX,
    config.TAG_ONBOARDED,
    config.TAG_ON_HOLD,
    config.TAG_SHIPPED,
    config.TAG_NOT_PLANNED,
}


class Nudge(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Daily loop — waiting for a second failure would mean 24 hours of
        # silence, so alert on the first.
        self._alerter = LoopFailureAlerter("Stale nudge loop")

    async def cog_load(self) -> None:
        self.nudge_stale.start()

    async def cog_unload(self) -> None:
        self.nudge_stale.cancel()

    @tasks.loop(hours=24)
    async def nudge_stale(self) -> None:
        # An exception escaping the loop body kills the loop permanently
        try:
            await self._run_nudge()
        except TRANSIENT_LOOP_EXCEPTIONS:
            # Let discord.ext.tasks retry these with its own backoff
            raise
        except Exception as exc:
            log.exception("Stale nudge run failed")
            await self._alerter.failed(exc)
            return
        await self._alerter.recovered()

    async def _run_nudge(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

        now = discord.utils.utcnow()
        nudged: list[str] = []
        archived: list[str] = []

        for channel_id, status_tags in NUDGE_CHANNELS.items():
            forum = guild.get_channel(channel_id)
            if not forum or not isinstance(forum, discord.ForumChannel):
                continue

            for thread in forum.threads:
                if thread.archived or thread.locked or thread.flags.pinned:
                    continue

                tag_ids = {t.id for t in thread.applied_tags} if thread.applied_tags else set()

                # Skip if already resolved/closed
                if tag_ids & DONE_TAGS:
                    continue

                # Only act on threads with a status tag (or untagged manual threads)
                if tag_ids and not (tag_ids & status_tags):
                    continue

                # Fast path: skip threads with very recent activity of any kind
                if not thread.last_message_id:
                    continue
                if now - discord.utils.snowflake_time(thread.last_message_id) < FIRST_NUDGE_AT:
                    continue

                action = await self._evaluate(thread, now)
                if action is None:
                    continue
                kind, days = action

                if kind == "nudge":
                    mentions = await self._get_mentions(thread, channel_id)
                    try:
                        await thread.send(
                            f"\U0001f514 **Reminder** — This thread has had no activity for {days} days. "
                            f"{mentions}"
                        )
                        nudged.append(thread.name)
                        await asyncio.sleep(1)
                    except discord.Forbidden:
                        log.warning("Cannot nudge thread %s", thread.name)
                elif kind == "archive":
                    try:
                        await thread.send(
                            f"\U0001f4a4 Closing this thread — no response in {days} days. "
                            f"Reply to reopen if you still need help."
                        )
                        await thread.edit(archived=True)
                        archived.append(thread.name)
                        await asyncio.sleep(1)
                    except discord.Forbidden:
                        log.warning("Cannot auto-archive thread %s", thread.name)

        if nudged:
            msg = "**Stale Nudges**\n" + "\n".join(f"• {name}" for name in nudged)
            await log_to_discord(msg)
            log.info("Nudged %d stale threads", len(nudged))
        if archived:
            msg = "**Auto-Archived (no response)**\n" + "\n".join(
                f"• {name}" for name in archived
            )
            await log_to_discord(msg)
            log.info("Auto-archived %d unresolved threads", len(archived))

    async def _evaluate(self, thread: discord.Thread, now):
        """Decide what to do with a stale-looking thread.

        Walks recent history to find the latest "real" activity (anything not
        posted by this bot — webhook starter, human reply, etc.) and counts
        our own nudge messages newer than that activity. Returns:
            ("nudge", days)   — send a reminder
            ("archive", days) — close the thread
            None              — skip
        """
        me_id = self.bot.user.id if self.bot.user else None
        last_real_activity = None
        nudges_after_activity = 0

        try:
            async for msg in thread.history(limit=30):
                if msg.author.id == me_id:
                    if msg.content.startswith("\U0001f514"):
                        nudges_after_activity += 1
                    continue
                last_real_activity = msg.created_at
                break
        except discord.HTTPException:
            return None

        if last_real_activity is None:
            return None

        age = now - last_real_activity

        if age >= AUTO_ARCHIVE_AT:
            return ("archive", age.days)
        if age >= SECOND_NUDGE_AT and nudges_after_activity < 2:
            return ("nudge", age.days)
        if age >= FIRST_NUDGE_AT and nudges_after_activity < 1:
            return ("nudge", age.days)
        return None

    async def _get_mentions(self, thread: discord.Thread, channel_id: int) -> str:
        """Build mention string for the relevant admins."""
        # Check if this is an app-created thread with a scene
        request = await db.get_request_by_thread(self.bot.pool, str(thread.id))

        if request and request["scene_id"]:
            admins = db.select_tier_admins(
                await db.get_admins_for_scene(self.bot.pool, request["scene_id"])
            )
            parts = []
            seen: set[str] = set()
            for a in admins:
                did = a["discord_user_id"]
                if did and did not in seen:
                    seen.add(did)
                    parts.append(f"<@{did}>")
            if parts:
                return " ".join(parts)

        # Fallback: ping global admins
        admin_ids = await db.get_global_admin_discord_ids(self.bot.pool)
        if admin_ids:
            return " ".join(f"<@{uid}>" for uid in admin_ids)

        return ""

    @nudge_stale.before_loop
    async def before_nudge(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nudge(bot))
