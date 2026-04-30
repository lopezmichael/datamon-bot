"""Weekly scene health digest posted to #scene-coordination."""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

import config
import db

log = logging.getLogger(__name__)


class Digest(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.weekly_digest.start()

    async def cog_unload(self) -> None:
        self.weekly_digest.cancel()

    @tasks.loop(hours=168)  # 7 days
    async def weekly_digest(self) -> None:
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
        try:
            thread, _ = await forum.create_thread(
                name=f"Weekly Scene Health Check",
                content=f"\U0001f4ca **Weekly Scene Health Check**\n\n{body}",
            )
        except discord.Forbidden:
            log.warning("Cannot create digest thread in scene coordination")
            return

        # Post per-scene admin mentions for dormant and unassigned scenes
        scene_ids = set()
        for r in dormant:
            scene_ids.add(r["scene_id"])
        for r in unassigned:
            scene_ids.add(r["scene_id"])
        for r in deactivated:
            scene_ids.add(r["scene_id"])

        mention_parts: dict[str, list[str]] = {}  # discord_user_id -> list of scene names
        for scene_id in scene_ids:
            admins = await db.get_admins_for_scene(self.bot.pool, scene_id)
            # Find the scene display name from our results
            scene_name = None
            for collection in (dormant, unassigned, deactivated):
                for r in collection:
                    if r["scene_id"] == scene_id:
                        scene_name = r.get("display_name") or r.get("scene_name")
                        break
                if scene_name:
                    break

            has_scene_admins = any(
                a["assignment_type"] in ("direct", "regional") for a in admins
            )
            for a in admins:
                if has_scene_admins and a["assignment_type"] == "global":
                    continue
                if a["discord_user_id"]:
                    mention_parts.setdefault(a["discord_user_id"], [])
                    if scene_name:
                        mention_parts[a["discord_user_id"]].append(scene_name)

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

    @weekly_digest.before_loop
    async def before_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))
