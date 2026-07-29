"""Shared utilities — kept separate from bot.py to avoid circular imports."""

import logging

import aiohttp
import discord

import config

log = logging.getLogger(__name__)

# Mirrors discord.ext.tasks Loop._valid_exception: re-raise these from loop
# bodies so the library's backoff-retry handles transient failures.
TRANSIENT_LOOP_EXCEPTIONS = (
    OSError,
    aiohttp.ClientError,
    discord.ConnectionClosed,
    discord.GatewayNotFound,
)


async def log_to_discord(message: str) -> None:
    """Post a message to #bot-log via webhook. Fire-and-forget."""
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(config.WEBHOOK_BOT_LOG, session=session)
            await webhook.send(message, username="Datamon Bot")
    except Exception:
        log.exception("Failed to log to Discord")
