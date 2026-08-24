"""Auto-archive stale resolved threads."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config
import db
from utils import TRANSIENT_LOOP_EXCEPTIONS, LoopFailureAlerter, apply_resolve_tag, log_to_discord

log = logging.getLogger(__name__)

# Tag IDs that indicate a thread is "done", with their archive delay
COMPLETION_TAGS: dict[int, timedelta] = {
    config.TAG_ONBOARDED: timedelta(hours=48),
    config.TAG_FIXED: timedelta(hours=48),
    config.TAG_SHIPPED: timedelta(hours=48),
    config.TAG_WONT_FIX: timedelta(weeks=1),
    config.TAG_NOT_PLANNED: timedelta(weeks=1),
    config.TAG_ON_HOLD: timedelta(weeks=1),
}

# The terminal status sets live in db.py — see the block above RESOLVED_STATUSES
# there for why. Read from db directly rather than aliasing: nothing imports them
# from this module, and a local alias is one more name to keep in step.


class Archiver(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Hourly loop — a second data point is an hour away, so alert on the
        # first failure rather than sitting on it.
        self._alerter = LoopFailureAlerter("Archive loop")

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
        except Exception as exc:
            log.exception("Archive run failed")
            await self._alerter.failed(exc)
            return
        await self._alerter.recovered()

    async def _run_archive(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

        now = datetime.now(timezone.utc)
        archived: list[str] = []
        healed: list[str] = []
        healed_rejected: list[str] = []

        for channel_id, forum_config in config.FORUM_CHANNELS.items():
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
                    # No completion tag — but the app may have closed the
                    # request without tagging (its tagging is fire-and-forget).
                    # Heal it here; the next pass archives it normally.
                    # Healing touches the DB and Discord, so it is isolated:
                    # one bad thread or a Neon blip must not abort the pass or
                    # lose the batch summary. Archiving already-tagged threads
                    # needs no DB at all and must keep working regardless.
                    try:
                        await self._heal_thread(
                            guild, thread, forum_config, healed, healed_rejected
                        )
                    except Exception:
                        log.exception("Heal failed for thread %s", thread.id)
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

        if healed:
            msg = "**Healed (resolved in app, tag applied)**\n" + "\n".join(
                f"• {name}" for name in healed
            )
            await log_to_discord(msg)
            log.info("Healed %d resolved-but-untagged threads", len(healed))
        if healed_rejected:
            msg = "**Healed (rejected in app, tag applied)**\n" + "\n".join(
                f"• {name}" for name in healed_rejected
            )
            await log_to_discord(msg)
            log.info("Healed %d rejected-but-untagged threads", len(healed_rejected))
        if archived:
            msg = "**Auto-Archive**\n" + "\n".join(f"• {name}" for name in archived)
            await log_to_discord(msg)
            log.info("Archived %d threads", len(archived))

    async def _heal_thread(
        self,
        guild: discord.Guild,
        thread: discord.Thread,
        forum_config: dict,
        healed: list[str],
        healed_rejected: list[str],
    ) -> None:
        """Tag a thread whose request is already finished in the app.

        Only called for threads missing the channel's completion tag. Threads
        with no DB row (manual threads, digest threads) are left alone, as are
        requests in any still-open status.

        Both outcomes do the same thing — apply a completion tag and hand the
        thread back to the normal tag-driven threshold path — because that is
        the contract the web app implements. `resolveDiscordThread`
        (digilab-web `src/lib/discord.ts`) applies `rejectTag ?? resolveTag` on a
        rejection, so healing a row the app rejected must land on the same tag
        the app would have applied. Won't Fix / Not Planned archive after a week,
        the resolve tags after 48 hours.

        Archiving the thread directly instead would have been wrong: Discord
        un-archives a thread the moment anyone replies, and the thread would come
        back still untagged — so the next hourly pass re-archives it, forever.
        A completion tag is durable; an archived flag is not.
        """
        # Skip the bot's own threads before touching the DB — they never have a
        # request row, so each would otherwise cost a query per thread per hour
        # forever. (Purely defensive since the weekly digest moved to a webhook
        # post; the bot creates no forum threads today.)
        if self.bot.user and thread.owner_id == self.bot.user.id:
            return

        request = await db.get_request_by_thread(self.bot.pool, str(thread.id))
        if not request:
            return

        status = request["status"]

        if status in db.RESOLVED_STATUSES:
            if await apply_resolve_tag(thread, guild, forum_config):
                healed.append(thread.name)
                log.info("Healed thread %s — resolved in app but untagged", thread.id)
                await asyncio.sleep(1)
            return

        if status in db.REJECTED_STATUSES:
            tag_id = forum_config.get("reject_tag") or forum_config["resolve_tag"]
            if await apply_resolve_tag(thread, guild, forum_config, tag_id):
                healed_rejected.append(thread.name)
                log.info("Healed thread %s — rejected in app but untagged", thread.id)
                await asyncio.sleep(1)

    @archive_stale.before_loop
    async def before_archive(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Archiver(bot))
