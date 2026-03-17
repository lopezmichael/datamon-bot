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
            log.debug("Could not fetch starter message for thread %s", thread.id, exc_info=True)

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
            return

        # Mention platform admins for manual scene requests and bug reports
        if channel_type in ("scene_requests", "bug_reports"):
            admin_ids = await db.get_super_admin_discord_ids(self.bot.pool)
            if admin_ids:
                mentions = " ".join(f"<@{uid}>" for uid in admin_ids)
                try:
                    await thread.send(mentions)
                except discord.Forbidden:
                    pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ThreadWatcher(bot))
