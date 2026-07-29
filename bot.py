"""Datamon Bot — Entry point, bot subclass, lifecycle, cog loading."""

import logging

import asyncpg
import discord
from discord.ext import commands

import config
import db
from utils import check_forum_config

log = logging.getLogger(__name__)


class DatamonBot(commands.Bot):
    pool: asyncpg.Pool

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self._forum_config_checked = False

    async def setup_hook(self) -> None:
        self.pool = await db.create_pool()

        cog_names = [
            "cogs.role_sync",
            "cogs.commands",
            "cogs.reactions",
            "cogs.archiver",
            "cogs.thread_watcher",
            "cogs.nudge",
            "cogs.digest",
        ]
        for cog in cog_names:
            await self.load_extension(cog)
            log.info("Loaded cog: %s", cog)

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Command tree synced to guild %s", config.GUILD_ID)

    async def on_ready(self) -> None:
        scene_count = await db.get_scene_count(self.pool)
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{scene_count} scenes",
        )
        await self.change_presence(activity=activity)
        log.info("Logged in as %s (watching %d scenes)", self.user, scene_count)

        if self.guilds:
            guild = self.guilds[0]
            if len(guild.members) < 2:
                log.warning(
                    "Guild has %d cached members — is the GUILD_MEMBERS privileged intent enabled?",
                    len(guild.members),
                )

        # Channel and tag IDs can't change under us mid-session in any way that
        # matters, and on_ready fires again on every gateway resume, so check once
        # per process. The flag is only set once we actually have the guild — a
        # missing guild here means the cache isn't ready, not that config is fine.
        if not self._forum_config_checked:
            digilab_guild = self.get_guild(config.GUILD_ID)
            if digilab_guild:
                self._forum_config_checked = True
                await check_forum_config(digilab_guild)
            else:
                log.error(
                    "Guild %s not in cache at on_ready — skipping forum config check",
                    config.GUILD_ID,
                )

    async def close(self) -> None:
        if hasattr(self, "pool") and self.pool:
            await self.pool.close()
            log.info("Database pool closed")
        await super().close()


def main() -> None:
    bot = DatamonBot()
    bot.run(config.BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
