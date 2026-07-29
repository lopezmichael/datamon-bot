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

# Discord's hard cap on tags applied to a forum thread.
MAX_THREAD_TAGS = 5

# Webhook messages cap at 2000 characters; leave room for the username header
# and any trailing newline a caller tacks on.
MAX_LOG_CHARS = 1900


def _chunk_message(message: str, limit: int = MAX_LOG_CHARS) -> list[str]:
    """Split a message into webhook-sized chunks on line boundaries, in order.

    Discord rejects webhook payloads over 2000 characters with a 400, which
    `log_to_discord` swallows — so a long batch summary (the archiver's first
    heal pass posts one bullet per thread) would vanish entirely. Splitting on
    newlines keeps every bullet intact; a single line longer than the limit has
    nowhere to break and is hard-split as a last resort.
    """
    if len(message) <= limit:
        return [message]

    chunks: list[str] = []
    current = ""
    for line in message.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue

        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


async def log_to_discord(message: str) -> None:
    """Post a message to #bot-log via webhook. Fire-and-forget.

    Messages over the webhook size limit are split across several posts, sent
    in order over one session.
    """
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(config.WEBHOOK_BOT_LOG, session=session)
            for chunk in _chunk_message(message):
                await webhook.send(chunk, username="Datamon Bot")
    except Exception:
        log.exception("Failed to log to Discord")


async def apply_resolve_tag(
    channel: discord.Thread,
    guild: discord.Guild,
    forum_config: dict,
    tag_id: int | None = None,
) -> bool:
    """Add a completion tag and remove initial/status tags from a thread.

    `tag_id` defaults to the channel's `resolve_tag`; pass a different completion
    tag (e.g. the channel's `reject_tag`) to retire the thread under that label.

    Tag merging mirrors the web app's `mergeThreadTags`
    (`digilab-web/src/lib/discord.ts`): strip the status tags first, then keep
    only as many of the remaining tags as fit under Discord's 5-tag cap, so the
    completion tag always survives. The old `[...current, tag]` shape could send
    six tags on a busy thread; Discord answers 400, and the resulting
    `HTTPException` used to escape past the `discord.Forbidden` catch — in the
    reaction path that happens *after* the DB row is already resolved, leaving a
    resolved-but-untagged thread the archiver would never touch.

    Returns True if the thread's tags were edited.
    """
    if tag_id is None:
        tag_id = forum_config["resolve_tag"]
    strip_tags = set(forum_config.get("initial_tags", []))
    try:
        existing_tags = [t.id for t in channel.applied_tags] if channel.applied_tags else []

        # Already settled: completion tag present, no status tags left.
        if tag_id in existing_tags and not strip_tags & set(existing_tags):
            return False

        parent = guild.get_channel(channel.parent_id)
        if not parent or not isinstance(parent, discord.ForumChannel):
            return False

        all_tags = {t.id: t for t in parent.available_tags}
        if tag_id not in all_tags:
            # Misconfigured or deleted tag ID. Bail without stripping: an
            # untagged thread is re-examined on every pass, forever.
            log.warning(
                "Tag %s is not available in forum %s — skipping thread %s",
                tag_id, parent.id, channel.id,
            )
            return False

        kept = [
            all_tags[tid] for tid in existing_tags
            if tid in all_tags and tid != tag_id and tid not in strip_tags
        ]
        new_tags = kept[:MAX_THREAD_TAGS - 1] + [all_tags[tag_id]]
        await channel.edit(applied_tags=new_tags)
        return True
    except discord.HTTPException as exc:
        # Covers Forbidden (403) and the 400 an over-cap or invalid tag edit returns.
        log.warning(
            "Cannot edit tags on thread %s (HTTP %s): %s",
            channel.id, exc.status, exc.text,
        )
    return False
