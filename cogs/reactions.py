"""React-to-resolve handler for forum threads."""

import logging

import discord
from discord.ext import commands

import config
import db
from utils import log_to_discord

log = logging.getLogger(__name__)


class Reactions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        # Only ✅ reactions
        if str(payload.emoji) != "\u2705":
            return

        # Ignore bot's own reactions
        if payload.user_id == self.bot.user.id:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        # Must be a thread
        channel = guild.get_channel_or_thread(payload.channel_id)
        if not isinstance(channel, discord.Thread):
            return

        # Parent must be a tracked forum channel
        if channel.parent_id not in config.FORUM_CHANNELS:
            return

        # Only react on the first message (thread starter = thread ID)
        if payload.message_id != channel.id:
            return

        forum_config = config.FORUM_CHANNELS[channel.parent_id]

        # DB lookup
        request = await db.get_request_by_thread(self.bot.pool, str(channel.id))

        if request:
            await self._resolve_app_thread(channel, forum_config, request, guild, payload)
        else:
            await self._resolve_manual_thread(channel, forum_config, guild, payload)

    async def _resolve_app_thread(
        self,
        channel: discord.Thread,
        forum_config: dict,
        request: dict,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Resolve an app-created thread (has DB record)."""
        if request["status"] == "resolved":
            return

        # Permission check: reactor must be admin for the request's scene or Platform Admin
        member = guild.get_member(payload.user_id)
        if not member:
            return

        has_platform = any(r.id == config.ROLE_PLATFORM_ADMIN for r in member.roles)
        if not has_platform:
            user_scenes = await db.get_admin_scenes_for_user(
                self.bot.pool, str(payload.user_id)
            )
            # None = not an admin at all; otherwise check scene-level access
            has_access = (
                user_scenes is not None
                and (
                    len(user_scenes) == 0  # super_admin (global access)
                    or not request["scene_id"]  # request has no scene
                    or request["scene_id"] in user_scenes
                )
            )
            if not has_access:
                # Remove reaction and DM user
                try:
                    msg = await channel.fetch_message(payload.message_id)
                    await msg.remove_reaction(payload.emoji, member)
                except discord.Forbidden:
                    pass

                try:
                    await member.send("You need admin access for this scene to resolve requests.")
                except discord.Forbidden:
                    pass
                return

        # Resolve in DB
        resolved = await db.resolve_request(
            self.bot.pool, str(channel.id), member.display_name
        )
        if not resolved:
            return

        # Add resolve tag + post confirmation
        tag_id = forum_config["resolve_tag"]
        label = forum_config["label"]

        await self._apply_resolve_tag(channel, guild, tag_id)

        try:
            await channel.send(f"\u2705 **{label}** by {member.mention}")
        except discord.Forbidden:
            pass

        # Log to #bot-log
        scene_info = f" in scene #{request['scene_id']}" if request["scene_id"] else ""
        await log_to_discord(
            f"Request #{request['id']} **{label.lower()}** by {member.mention}{scene_info}"
        )
        log.info("Request #%d resolved by %s", request["id"], member)

    async def _resolve_manual_thread(
        self,
        channel: discord.Thread,
        forum_config: dict,
        guild: discord.Guild,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """Resolve a manual thread (no DB record). Tag-only, any DigiLab admin can resolve."""
        member = guild.get_member(payload.user_id)
        if not member:
            return

        # Permission check: reactor must have any DigiLab role
        has_digilab_role = any(r.id in config.DIGILAB_ROLE_IDS for r in member.roles)
        if not has_digilab_role:
            try:
                msg = await channel.fetch_message(payload.message_id)
                await msg.remove_reaction(payload.emoji, member)
            except discord.Forbidden:
                pass

            try:
                await member.send("You need admin access to resolve threads.")
            except discord.Forbidden:
                pass
            return

        # Check if already tagged as resolved
        tag_id = forum_config["resolve_tag"]
        label = forum_config["label"]
        existing_tag_ids = {t.id for t in channel.applied_tags} if channel.applied_tags else set()
        if tag_id in existing_tag_ids:
            return

        # Add resolve tag + post confirmation
        await self._apply_resolve_tag(channel, guild, tag_id)

        try:
            await channel.send(f"\u2705 **{label}** by {member.mention}")
        except discord.Forbidden:
            pass

        await log_to_discord(
            f"Manual thread **{label.lower()}** by {member.mention} in {channel.mention}"
        )
        log.info("Manual thread %s resolved by %s", channel.id, member)

    async def _apply_resolve_tag(
        self,
        channel: discord.Thread,
        guild: discord.Guild,
        tag_id: int,
    ) -> None:
        """Add the resolve tag to a thread while preserving existing tags."""
        try:
            existing_tags = [t.id for t in channel.applied_tags] if channel.applied_tags else []
            if tag_id not in existing_tags:
                parent = guild.get_channel(channel.parent_id)
                if parent and isinstance(parent, discord.ForumChannel):
                    all_tags = {t.id: t for t in parent.available_tags}
                    new_tags = [all_tags[tid] for tid in existing_tags if tid in all_tags]
                    if tag_id in all_tags:
                        new_tags.append(all_tags[tag_id])
                    await channel.edit(applied_tags=new_tags)
        except discord.Forbidden:
            log.warning("Cannot edit tags on thread %s", channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reactions(bot))
