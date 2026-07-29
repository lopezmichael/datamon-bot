"""Weekly scene health digest posted to #scene-coordination."""

import asyncio
import datetime
import logging

import discord
from discord.ext import commands, tasks

import config
import db
from utils import TRANSIENT_LOOP_EXCEPTIONS, log_to_discord

log = logging.getLogger(__name__)

# Run daily at 09:00 UTC, but only post on Mondays
DIGEST_TIME = datetime.time(hour=9, tzinfo=datetime.timezone.utc)


class Digest(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._failure_alerted = False

    async def cog_load(self) -> None:
        self.weekly_digest.start()

    async def cog_unload(self) -> None:
        self.weekly_digest.cancel()

    @tasks.loop(time=DIGEST_TIME)
    async def weekly_digest(self) -> None:
        # An exception escaping the loop body kills the loop permanently
        try:
            await self._run_digest()
        except TRANSIENT_LOOP_EXCEPTIONS:
            # Let discord.ext.tasks retry these with its own backoff. Only work
            # before thread creation can raise these — see _run_digest.
            raise
        except Exception:
            log.exception("Weekly digest run failed")
            if not self._failure_alerted:
                self._failure_alerted = True
                await log_to_discord(
                    "⚠️ Weekly digest loop failed — check `railway logs`. "
                    "Alerting once until it recovers."
                )
            return
        self._failure_alerted = False

    async def _run_digest(self) -> None:
        # Only run on Mondays
        if discord.utils.utcnow().weekday() != 0:
            return

        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            return

        forum = guild.get_channel(config.CHANNEL_SCENE_COORDINATION)
        if not forum or not isinstance(forum, discord.ForumChannel):
            log.warning("Scene coordination channel not found or not a forum")
            return

        dormant, unassigned, deactivated = await asyncio.gather(
            db.get_dormant_scenes(self.bot.pool, 60),
            db.get_unassigned_scenes(self.bot.pool),
            db.get_recently_deactivated_stores(self.bot.pool, 7),
        )

        # Skip if everything is healthy
        if not dormant and not unassigned and not deactivated:
            log.info("Scene health digest: all clear, skipping post")
            return

        # Build the digest body
        sections = []

        if dormant:
            lines = []
            for r in dormant:
                if r["last_tournament"]:
                    lines.append(f"\u2022 **{r['display_name']}** \u2014 last tournament {r['last_tournament'].strftime('%b %d')}")
                else:
                    lines.append(f"\u2022 **{r['display_name']}** \u2014 no tournaments on record")
            sections.append("**Scenes with no tournaments in 60+ days:**\n" + "\n".join(lines))

        if unassigned:
            lines = [f"\u2022 **{r['display_name']}**" for r in unassigned]
            sections.append("**Scenes with no assigned admin:**\n" + "\n".join(lines))

        if deactivated:
            lines = [
                f"\u2022 **{r['name']}** ({r['scene_name']})"
                for r in deactivated
            ]
            sections.append("**Stores deactivated this week:**\n" + "\n".join(lines))

        body = "\n\n".join(sections)

        # Create a forum thread
        date_str = discord.utils.utcnow().strftime("%b %d, %Y")
        try:
            thread, _ = await forum.create_thread(
                name=f"Weekly Scene Health Check \u2014 {date_str}",
                content=f"\U0001f4ca **Weekly Scene Health Check**\n\n{body}",
            )
        except discord.HTTPException:
            log.warning("Cannot create digest thread in scene coordination", exc_info=True)
            return

        # The digest is now posted. Nothing below may propagate \u2014 a retried run
        # would create a duplicate thread, so swallow everything from here on.
        try:
            # Build a mapping of scene_id -> display name from all results
            scene_names: dict[int, str] = {}
            for r in dormant:
                scene_names[r["scene_id"]] = r["display_name"]
            for r in unassigned:
                scene_names[r["scene_id"]] = r["display_name"]
            for r in deactivated:
                scene_names.setdefault(r["scene_id"], r["scene_name"])

            # Post per-scene admin mentions
            scene_ids = set(scene_names.keys())
            mention_parts: dict[str, list[str]] = {}  # discord_user_id -> list of scene names
            for scene_id in scene_ids:
                admins = db.select_tier_admins(
                    await db.get_admins_for_scene(self.bot.pool, scene_id)
                )
                scene_name = scene_names.get(scene_id, "Unknown")

                seen: set[str] = set()
                for a in admins:
                    did = a["discord_user_id"]
                    if did and did not in seen:
                        seen.add(did)
                        mention_parts.setdefault(did, [])
                        mention_parts[did].append(scene_name)

            if mention_parts:
                lines = []
                for uid, scenes in mention_parts.items():
                    scene_list = ", ".join(scenes[:5])
                    if len(scenes) > 5:
                        scene_list += f" +{len(scenes) - 5} more"
                    lines.append(f"<@{uid}> \u2014 {scene_list}")

                try:
                    await thread.send("\n".join(lines))
                except discord.Forbidden:
                    pass

                await asyncio.sleep(1)
        except Exception:
            log.exception("Digest posted but admin mentions failed")

    @weekly_digest.before_loop
    async def before_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))
