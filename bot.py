"""Datamon Bot — Entry point, bot subclass, lifecycle, cog loading."""

import logging

import asyncpg
import discord
from discord.ext import commands

import config
import db

log = logging.getLogger(__name__)


class DatamonBot(commands.Bot):
    pool: asyncpg.Pool

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        self.pool = await db.create_pool()

        cog_names = [
            "cogs.role_sync",
            "cogs.commands",
            "cogs.reactions",
            "cogs.archiver",
            "cogs.thread_watcher",
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
