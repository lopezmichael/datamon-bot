"""React-to-resolve handler for forum threads."""

import logging

import discord
from discord.ext import commands

import config
import db
from utils import apply_resolve_tag, log_to_discord

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

        # Must be a thread. Fall back to fetch if not in cache —
        # `get_channel_or_thread` only checks active-thread cache, which can
        # miss after bot restarts or for low-activity / older threads.
        channel = guild.get_channel_or_thread(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
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
        # TERMINAL, not just 'resolved'. A request the web REJECTED is finished
        # and already carries the rejecter's attribution, but its status is
        # 'rejected' — so this guard used to wave it through and `resolve_request`
        # rewrote the row to resolved, destroying the original decision. The web's
        # reject tag lands on the thread but does not close it, so it stays
        # reactable; 15 rejected requests had live threads when this was found.
        if request["status"] in db.TERMINAL_STATUSES:
            return

        # Permission check: the reactor must hold admin access for the request's game.
        member = await self._get_member(guild, payload.user_id)
        if not member:
            return

        # The DB is the authority here, and it is asked on EVERY reaction. The
        # Discord "Platform Admin" role used to short-circuit this check, and must
        # not: that role is one flat badge across all games (role_sync grants it from
        # the strongest role a person holds in ANY game), so honoring it as a bypass
        # would let a Digimon-only platform admin resolve Gundam requests — the very
        # thing the per-game fallback exists to prevent. Treat the role as cosmetic
        # for authorization; `game_admin_roles` decides.
        access = await db.get_admin_access_for_user(
            self.bot.pool, str(payload.user_id), request["game_id"]
        )
        # Branch on the LEVEL, never on len(rows): 'scoped' with no rows means no
        # access at all, while 'global' carries no rows by design.
        if not access.covers(request["scene_id"]):
            # Remove reaction and DM user
            try:
                msg = await channel.fetch_message(payload.message_id)
                await msg.remove_reaction(payload.emoji, member)
            except discord.Forbidden:
                pass

            # Name the game. A Digimon scene admin reacting on a Gundam thread in
            # a shared forum is the common case for this denial, and "you need
            # admin access for this scene" reads as a mistake on our side when
            # they demonstrably do administer that scene — for the other game.
            game_label = self.bot.games.label(request["game_id"], default="")
            scope = f" for **{game_label}**" if game_label else ""
            try:
                await member.send(
                    f"You need admin access{scope} for this scene to resolve requests."
                )
            except discord.Forbidden:
                pass
            return

        # Resolve in DB
        resolved = await db.resolve_request(
            self.bot.pool, str(channel.id), str(member.id)
        )
        if not resolved:
            return

        # Add resolve tag + post confirmation
        label = forum_config["label"]

        await apply_resolve_tag(channel, guild, forum_config)

        game_label = self.bot.games.label(request["game_id"], default="")
        scope = f" \u2014 {game_label}" if game_label else ""
        try:
            await channel.send(f"\u2705 **{label}**{scope} by {member.mention}")
        except discord.Forbidden:
            pass

        # Log to #bot-log
        scene_info = f" in scene #{request['scene_id']}" if request["scene_id"] else ""
        await log_to_discord(
            f"Request #{request['id']} **{label.lower()}** by {member.mention}"
            f"{scene_info} ({self.bot.games.label(request['game_id'])})"
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
        member = await self._get_member(guild, payload.user_id)
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
        label = forum_config["label"]
        existing_tag_ids = {t.id for t in channel.applied_tags} if channel.applied_tags else set()
        if forum_config["resolve_tag"] in existing_tag_ids:
            return

        # Add resolve tag + post confirmation
        await apply_resolve_tag(channel, guild, forum_config)

        try:
            await channel.send(f"\u2705 **{label}** by {member.mention}")
        except discord.Forbidden:
            pass

        await log_to_discord(
            f"Manual thread **{label.lower()}** by {member.mention} in {channel.mention}"
        )
        log.info("Manual thread %s resolved by %s", channel.id, member)

    async def _get_member(self, guild: discord.Guild, user_id: int):
        member = guild.get_member(user_id)
        if member:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reactions(bot))
