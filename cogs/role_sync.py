"""Periodic role sync: DB admin roles ↔ Discord roles."""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

import config
import db
from utils import log_to_discord

log = logging.getLogger(__name__)


class RoleSync(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.sync_roles.start()

    async def cog_unload(self) -> None:
        self.sync_roles.cancel()

    @tasks.loop(minutes=5)
    async def sync_roles(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            log.warning("Guild %s not found", config.GUILD_ID)
            return

        if len(guild.members) < 2:
            log.warning("Guild has <2 cached members — skipping sync (check GUILD_MEMBERS intent)")
            return

        admins = await db.get_active_admins(self.bot.pool)
        changes: list[str] = []

        # Build lookup: discord_user_id → expected role ID
        admin_lookup: dict[int, int] = {}
        for admin in admins:
            if admin["discord_user_id"]:
                try:
                    discord_id = int(admin["discord_user_id"])
                    expected_role = config.ROLE_MAP.get(admin["role"])
                    if expected_role:
                        admin_lookup[discord_id] = expected_role
                except ValueError:
                    continue

        # Forward pass: DB → Discord
        for discord_id, expected_role_id in admin_lookup.items():
            member = guild.get_member(discord_id)
            if not member:
                continue

            expected_role = guild.get_role(expected_role_id)
            if not expected_role:
                continue

            # Add expected role if missing
            if expected_role not in member.roles:
                try:
                    await member.add_roles(expected_role, reason="Datamon role sync")
                    changes.append(f"Added **{expected_role.name}** to {member.mention}")
                    await asyncio.sleep(1)
                except discord.Forbidden:
                    log.warning("Cannot add role %s to %s (insufficient permissions)", expected_role.name, member)

            # Remove other DigiLab roles that don't match
            for role in member.roles:
                if role.id in config.DIGILAB_ROLE_IDS and role.id != expected_role_id:
                    try:
                        await member.remove_roles(role, reason="Datamon role sync")
                        changes.append(f"Removed **{role.name}** from {member.mention} (expected {expected_role.name})")
                        await asyncio.sleep(1)
                    except discord.Forbidden:
                        log.warning("Cannot remove role %s from %s", role.name, member)

        # Reverse pass: Discord → DB (remove roles from non-admins)
        for member in guild.members:
            if member.id in admin_lookup or member.bot:
                continue

            for role in member.roles:
                if role.id in config.DIGILAB_ROLE_IDS:
                    try:
                        await member.remove_roles(role, reason="Datamon role sync — not in active admins")
                        changes.append(f"Removed **{role.name}** from {member.mention} (not in active admins)")
                        await asyncio.sleep(1)
                    except discord.Forbidden:
                        log.warning("Cannot remove role %s from %s", role.name, member)

        # Update bot status
        scene_count = await db.get_scene_count(self.bot.pool)
        activity = discord.Activity(type=discord.ActivityType.watching, name=f"{scene_count} scenes")
        await self.bot.change_presence(activity=activity)

        # Log changes only
        if changes:
            msg = "**Role Sync**\n" + "\n".join(changes)
            await log_to_discord(msg)
            log.info("Role sync: %d changes", len(changes))

    @sync_roles.before_loop
    async def before_sync(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleSync(bot))
