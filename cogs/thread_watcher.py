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
        # Skip threads we posted ourselves (e.g. the weekly digest). App threads come
        # from a webhook, whose owner_id is the webhook's — not the bot user's.
        if self.bot.user and thread.owner_id == self.bot.user.id:
            return

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
        # The game travels with the request row, so the thread can say whose it is.
        # #scene-requests and #bug-reports are shared across games and the teams
        # are not \u2014 an untitled "New Scene Request" makes every admin open it to
        # find out whether it is theirs.
        game_label = self.bot.games.label(request["game_id"], default="")
        instructions = messages.app_thread_message(channel_type, request_type, game_label)

        if not instructions:
            # Fallback: unknown request_type in this channel
            label = config.FORUM_CHANNELS[thread.parent_id]["label"]
            scope = f" ({game_label})" if game_label else ""
            instructions = (
                f"\U0001f4cb **New request received!**{scope}\n"
                f"React \u2705 on the first message to mark this as {label.lower()}."
            )

        try:
            await thread.send(instructions)
        except discord.Forbidden:
            log.warning("Cannot send instructions to thread %s", thread.id)
            return

        # Resolve who to tag. The bot owns admin tagging end to end (the web app no longer
        # @mentions admins). Since the web app's Phase 2 deploy the only app threads that
        # land here are scene_request (#scene-requests) and bug_report (#bug-reports)
        # (store_request / data_error flow through the admin UI and the daily
        # #admin-digest), and both forums are the super admins' to triage, for every
        # game. Platform admins are game SMEs, not forum triage, so they are not paged —
        # and neither is the scene cascade, which would route a scene-carrying request
        # to scene admins who do not own these channels either.
        admin_ids = await db.get_super_admin_discord_ids(self.bot.pool)
        if not admin_ids:
            return

        # Check who's already mentioned in the webhook's first message
        already_mentioned: set[str] = set()
        try:
            starter = await thread.fetch_message(thread.id)
            already_mentioned = {str(u.id) for u in starter.mentions}
        except Exception:
            log.debug("Could not fetch starter message for thread %s", thread.id, exc_info=True)

        # Build mention list for admins not already tagged (de-dupe)
        mentions = []
        seen: set[str] = set()
        for did in admin_ids:
            if did and did not in already_mentioned and did not in seen:
                seen.add(did)
                mentions.append(f"<@{did}>")

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
        # Auto-apply "New" tag if the channel has one
        forum_config = config.FORUM_CHANNELS[thread.parent_id]
        new_tag_id = forum_config.get("new_tag")
        if new_tag_id:
            try:
                guild = self.bot.get_guild(config.GUILD_ID)
                parent = guild.get_channel(thread.parent_id) if guild else None
                if parent and isinstance(parent, discord.ForumChannel):
                    all_tags = {t.id: t for t in parent.available_tags}
                    if new_tag_id in all_tags:
                        # Re-fetch thread to get current tags (avoid race with Discord)
                        fresh = parent.get_thread(thread.id)
                        existing = list(fresh.applied_tags) if fresh and fresh.applied_tags else list(thread.applied_tags or [])
                        if len(existing) < 5 and new_tag_id not in {t.id for t in existing}:
                            existing.append(all_tags[new_tag_id])
                            await thread.edit(applied_tags=existing)
            except discord.HTTPException:
                log.warning("Cannot apply New tag to thread %s", thread.id)

        # No request row here, so no game. The copy asks the poster which game they
        # mean and names the ones DigiLab covers, rather than assuming.
        welcome = messages.manual_thread_message(
            channel_type, self.bot.games.live_labels()
        )

        if not welcome:
            return

        try:
            await thread.send(welcome)
        except discord.Forbidden:
            log.warning("Cannot send welcome to thread %s", thread.id)
            return

        # Mention the super admins, who own all three forums (see _handle_app_thread).
        if channel_type in ("scene_requests", "bug_reports", "feature_requests"):
            admin_ids = await db.get_super_admin_discord_ids(self.bot.pool)
            if admin_ids:
                mentions = " ".join(f"<@{uid}>" for uid in admin_ids)
                try:
                    await thread.send(mentions)
                except discord.Forbidden:
                    pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ThreadWatcher(bot))
