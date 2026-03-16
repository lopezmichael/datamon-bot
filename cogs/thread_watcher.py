"""Thread watcher: post instructions and tag admins on new forum threads."""

import asyncio
import logging

import discord
from discord.ext import commands

import config
import db

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
        label = forum_config["label"]

        # Post instructions
        instructions = (
            f"\U0001f44b **New request received!**\n"
            f"React \u2705 on the first message to mark this as {label.lower()}."
        )

        try:
            await thread.send(instructions)
        except discord.Forbidden:
            log.warning("Cannot send instructions to thread %s", thread.id)
            return

        # Look up the request to find the scene and tag admins
        request = await db.get_request_by_thread(self.bot.pool, str(thread.id))
        if not request or not request["scene_id"]:
            return

        admins = await db.get_admins_for_scene(self.bot.pool, request["scene_id"])
        if not admins:
            return

        # Fetch the first message to see who's already mentioned
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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ThreadWatcher(bot))
