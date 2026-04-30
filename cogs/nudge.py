"""Stale thread nudges: remind admins about unresolved threads."""

import asyncio
import logging
from datetime import timedelta

import discord
from discord.ext import commands, tasks

import config
import db
from utils import log_to_discord

log = logging.getLogger(__name__)

STALE_THRESHOLD = timedelta(days=3)

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
}


class Nudge(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.nudge_stale.start()

    async def cog_unload(self) -> None:
        self.nudge_stale.cancel()

    @tasks.loop(hours=24)
    async def nudge_stale(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

        now = discord.utils.utcnow()
        nudged: list[str] = []

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

                # Only nudge threads that have a status tag (or no tags at all — manual threads)
                if tag_ids and not (tag_ids & status_tags):
                    continue

                # Check staleness
                if not thread.last_message_id:
                    continue

                last_msg_time = discord.utils.snowflake_time(thread.last_message_id)
                if now - last_msg_time < STALE_THRESHOLD:
                    continue

                days = (now - last_msg_time).days
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

        if nudged:
            msg = "**Stale Nudges**\n" + "\n".join(f"\u2022 {name}" for name in nudged)
            await log_to_discord(msg)
            log.info("Nudged %d stale threads", len(nudged))

    async def _get_mentions(self, thread: discord.Thread, channel_id: int) -> str:
        """Build mention string for the relevant admins."""
        # Check if this is an app-created thread with a scene
        request = await db.get_request_by_thread(self.bot.pool, str(thread.id))

        if request and request["scene_id"]:
            admins = await db.get_admins_for_scene(self.bot.pool, request["scene_id"])
            has_scene_admins = any(
                a["assignment_type"] in ("direct", "regional") for a in admins
            )
            parts = []
            for a in admins:
                if has_scene_admins and a["assignment_type"] == "global":
                    continue
                if a["discord_user_id"]:
                    parts.append(f"<@{a['discord_user_id']}>")
            if parts:
                return " ".join(parts)

        # Fallback: ping super admins
        admin_ids = await db.get_super_admin_discord_ids(self.bot.pool)
        if admin_ids:
            return " ".join(f"<@{uid}>" for uid in admin_ids)

        return ""

    @nudge_stale.before_loop
    async def before_nudge(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Nudge(bot))
