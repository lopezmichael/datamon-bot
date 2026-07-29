"""Auto-archive stale resolved threads."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config
from utils import TRANSIENT_LOOP_EXCEPTIONS, log_to_discord

log = logging.getLogger(__name__)

# Tag IDs that indicate a thread is "done", with their archive delay
COMPLETION_TAGS: dict[int, timedelta] = {
    config.TAG_RESOLVED: timedelta(hours=48),
    config.TAG_ONBOARDED: timedelta(hours=48),
    config.TAG_FIXED: timedelta(hours=48),
    config.TAG_SHIPPED: timedelta(hours=48),
    config.TAG_WONT_FIX: timedelta(weeks=1),
    config.TAG_NOT_PLANNED: timedelta(weeks=1),
    config.TAG_ON_HOLD: timedelta(weeks=1),
}


class Archiver(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._failure_alerted = False

    async def cog_load(self) -> None:
        self.archive_stale.start()

    async def cog_unload(self) -> None:
        self.archive_stale.cancel()

    @tasks.loop(hours=1)
    async def archive_stale(self) -> None:
        # An exception escaping the loop body kills the loop permanently
        try:
            await self._run_archive()
        except TRANSIENT_LOOP_EXCEPTIONS:
            # Let discord.ext.tasks retry these with its own backoff
            raise
        except Exception:
            log.exception("Archive run failed")
            if not self._failure_alerted:
                self._failure_alerted = True
                await log_to_discord(
                    "⚠️ Archive loop failed — check `railway logs`. "
                    "Alerting once until it recovers."
                )
            return
        self._failure_alerted = False

    async def _run_archive(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

        now = datetime.now(timezone.utc)
        archived: list[str] = []

        for channel_id in config.FORUM_CHANNELS:
            forum = guild.get_channel(channel_id)
            if not forum or not isinstance(forum, discord.ForumChannel):
                continue

            for thread in forum.threads:
                if thread.archived or thread.locked or thread.flags.pinned:
                    continue

                # Check for completion tag and determine archive threshold
                tag_ids = {t.id for t in thread.applied_tags} if thread.applied_tags else set()
                matching = tag_ids & COMPLETION_TAGS.keys()
                if not matching:
                    continue

                # Use the shortest threshold among matching tags
                threshold = min(COMPLETION_TAGS[tid] for tid in matching)

                # Check staleness via last_message_id snowflake
                if not thread.last_message_id:
                    continue

                last_msg_time = discord.utils.snowflake_time(thread.last_message_id)
                if now - last_msg_time < threshold:
                    continue

                try:
                    await thread.edit(archived=True)
                    archived.append(thread.name)
                    await asyncio.sleep(1)
                except discord.Forbidden:
                    log.warning("Cannot archive thread %s", thread.name)

        if archived:
            msg = "**Auto-Archive**\n" + "\n".join(f"• {name}" for name in archived)
            await log_to_discord(msg)
            log.info("Archived %d threads", len(archived))

    @archive_stale.before_loop
    async def before_archive(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Archiver(bot))
