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


async def post_webhook(url: str, message: str, username: str = "Datamon Bot") -> None:
    """POST a message to a Discord webhook. Fire-and-forget.

    Messages over the webhook size limit are split across several posts, sent
    in order over one session.
    """
    try:
        async with aiohttp.ClientSession() as session:
            webhook = discord.Webhook.from_url(url, session=session)
            for chunk in _chunk_message(message):
                await webhook.send(chunk, username=username)
    except Exception:
        log.exception("Failed to post to webhook")


async def log_to_discord(message: str) -> None:
    """Post a message to #bot-log via webhook. Fire-and-forget."""
    await post_webhook(config.WEBHOOK_BOT_LOG, message)


# How much of an exception's text to carry into an alert. Enough for a Postgres
# error with its query fragment; short enough that five of them can't crowd a
# channel.
MAX_ALERT_ERROR_CHARS = 300


class LoopFailureAlerter:
    """Per-loop failure tracking for the periodic tasks, posting to #bot-log.

    Every loop cog used to hand-roll this as a bare `_failure_alerted` bool, and
    that shape has two faults the 2026-08-09 incident showed off. The flag reset
    on *any* successful tick, so a flapping DB connection (fail, recover, fail)
    re-alerted every single cycle — four warnings in one evening for one cause.
    And the alert said only "check `railway logs`", which meant the alert itself
    carried nothing actionable; Railway keeps ~a week of stdout, so an overnight
    flap can outlive the only record of why it happened.

    So: require `threshold` *consecutive* failures before saying anything, carry
    the error text, and announce recovery so nobody has to guess whether it is
    still broken. Fast loops want a threshold above 1 — one blip that heals on
    the next tick is noise, not news. Loops that run hourly or slower should stay
    at 1, since a second data point is an hour or a week away.
    """

    def __init__(self, label: str, threshold: int = 1) -> None:
        self.label = label
        self.threshold = threshold
        self._consecutive = 0
        self._alerted = False

    async def failed(self, exc: BaseException) -> None:
        """Record a failed run, alerting once the threshold is reached."""
        self._consecutive += 1
        if self._alerted or self._consecutive < self.threshold:
            return

        self._alerted = True
        detail = f"{type(exc).__name__}: {exc}".strip()
        if len(detail) > MAX_ALERT_ERROR_CHARS:
            detail = detail[:MAX_ALERT_ERROR_CHARS - 1] + "…"
        runs = "run" if self._consecutive == 1 else "consecutive runs"
        await log_to_discord(
            f"⚠️ **{self.label}** failed {self._consecutive} {runs} — `{detail}`\n"
            "Silent until it recovers; `railway logs` has the traceback."
        )

    async def recovered(self) -> None:
        """Record a successful run, announcing recovery only if we alerted."""
        was_alerted, failures = self._alerted, self._consecutive
        self._consecutive = 0
        self._alerted = False
        if was_alerted:
            await log_to_discord(
                f"✅ **{self.label}** recovered after {failures} failed runs."
            )


async def check_forum_config(guild: discord.Guild) -> None:
    """Verify every configured forum channel and tag ID actually exists. Non-fatal.

    A wrong or deleted ID fails silently and *forever* otherwise: `apply_resolve_tag`
    logs a warning and bails, the New tag is skipped, and the symptom (threads never
    archiving, nudges never stopping) only surfaces weeks later — that investigation
    has already been paid for once. We hold a gateway connection with `available_tags`
    materialized on every ForumChannel, so the check costs nothing beyond the guild
    cache we already have. Run once per process, after the cache is populated.

    Alerts name the env var, since that is what has to be edited to fix it. The web
    app copies its tag IDs verbatim from ours, so this covers those transitively —
    apart from a bad transcription into their Vercel env, which is a deploy-time
    problem, not a runtime one.
    """
    problems: list[str] = []

    for channel_id, forum_config in config.FORUM_CHANNELS.items():
        channel_env = config.CHANNEL_ENV_NAMES.get(channel_id, "unknown channel")
        forum = guild.get_channel(channel_id)

        if forum is None:
            problems.append(f"`{channel_env}` ({channel_id}) — no such channel in the guild")
            continue
        if not isinstance(forum, discord.ForumChannel):
            problems.append(
                f"`{channel_env}` (#{forum.name}) — is a {type(forum).__name__}, not a forum"
            )
            continue

        # De-dupe first: new_tag is also an initial_tags member in every channel
        # that has both, and one bad ID should produce one alert line.
        configured: dict[int, str] = {}
        for key in ("resolve_tag", "reject_tag", "new_tag"):
            tag_id = forum_config.get(key)
            if tag_id is not None:
                configured.setdefault(tag_id, key)
        for tag_id in forum_config.get("initial_tags", []):
            configured.setdefault(tag_id, "initial_tags")

        available = {t.id for t in forum.available_tags}
        for tag_id, key in configured.items():
            if tag_id not in available:
                tag_env = config.TAG_ENV_NAMES.get(tag_id, "unknown tag")
                problems.append(
                    f"`{tag_env}` ({tag_id}) — set as `{key}` for #{forum.name}, "
                    "but that forum has no such tag"
                )

    if not problems:
        log.info("Forum config OK — %d channels, all tag IDs present", len(config.FORUM_CHANNELS))
        return

    for problem in problems:
        log.error("Forum config: %s", problem)

    await log_to_discord(
        "⚠️ **Forum config problems at startup** — these IDs are wrong or deleted, "
        "and everything that depends on them fails silently until they are fixed:\n"
        + "\n".join(f"• {problem}" for problem in problems)
    )


def classify_game_roles(
    configured, live_ids: set[str], known_ids: set[str]
) -> dict[str, list[str]]:
    """Sort configured game roles into the four states described on `check_game_roles`.

    Pure — no Discord, no DB, no config — so it is tested in tests/test_game_copy.py.
    `configured` is any iterable of game ids (in practice `config.GAME_ROLES`),
    `live_ids` the games with scene coverage, `known_ids` every row in `games`.

    The distinction that matters is `staged` vs `no_row`: both are configured and
    grant nothing today, but a staged game starts working on its own and a game
    with no row never will. Collapsing them into one "not granting" bucket is what
    would let a typo hide behind a legitimately early role.
    """
    return {
        "syncing": sorted(g for g in configured if g in live_ids),
        "staged": sorted(g for g in configured if g in known_ids and g not in live_ids),
        "no_row": sorted(g for g in configured if g not in known_ids),
        "unconfigured": sorted(g for g in live_ids if g not in configured),
    }


async def check_game_roles(guild: discord.Guild, games) -> None:
    """Classify every configured game role at boot, and say so. Non-fatal.

    `config.GAME_ROLES` is the one optional ID set in the bot, because the roles
    are mid-rollout and deliberately run AHEAD of the platform — roles exist in
    Discord for games DigiLab has not launched, and in one case (naruto) for a
    game with no `games` row at all. That is the right way to stage them, and it
    is also why this check has to be precise: a role configured for a game the
    database has never heard of behaves *identically* to a typo'd env var.
    Both parse, both are valid snowflakes, both grant nothing, forever, in
    silence. The bot cannot tell intent apart from a slip — but it can name the
    state exactly, so a human reading one log line can.

    Five states, only one of which is actionable on its own:

    * **syncing** — configured, and the game has scene coverage. Granting today.
    * **staged** — configured, has a `games` row, no scene coverage yet. Grants
      nothing only because no admin holds that game; `_sync_game_roles` follows
      ADMIN ROWS, not coverage, so the first `game_admin_roles` row for it starts
      handing the role out. Nothing to do, but it is not inert by design.
    * **no game row** — configured, but no row in `games`. Cannot grant until the
      row exists. Deliberate for a game announced before launch; a typo otherwise.
    * **unconfigured** — live, with admins, and no env var. Those admins silently
      get no role, so this logs at WARNING even though nothing is broken.
    * **bad role ID** — points at no role in this guild. Wrong or deleted; alerted.
    """
    configured = config.GAME_ROLES
    buckets = classify_game_roles(
        configured, games.live_ids(), set(games.labels)
    )
    syncing = buckets["syncing"]
    staged = buckets["staged"]
    no_row = buckets["no_row"]
    unconfigured = buckets["unconfigured"]

    bad_id: list[str] = []
    for game_id, role_id in sorted(configured.items()):
        if guild.get_role(role_id) is None:
            bad_id.append(
                f"`DISCORD_GAME_ROLE_{game_id.upper()}` ({role_id}) — no such role in the guild"
            )

    log.info(
        "Game roles — syncing: %s | staged for launch: %s | no `games` row: %s",
        ", ".join(syncing) or "none",
        ", ".join(staged) or "none",
        ", ".join(no_row) or "none",
    )
    if no_row:
        # Not an error: staging a role ahead of the game is supported. But it is
        # the one state a typo also produces, so it never scrolls past unlabelled.
        log.info(
            "Game roles for %s are configured but have no row in `games` — nothing "
            "can be granted for them, since grants follow admin assignments and no "
            "assignment can name a game that does not exist. Expected for an "
            "announced game; check the spelling against `games.game_id` otherwise.",
            ", ".join(no_row),
        )
    if unconfigured:
        log.warning(
            "Game roles — LIVE games with no DISCORD_GAME_ROLE_* var: %s. Their "
            "admins will never be granted a game role.",
            ", ".join(unconfigured),
        )

    if bad_id:
        for problem in bad_id:
            log.error("Game role config: %s", problem)
        await log_to_discord(
            "⚠️ **Game role problems at startup** — these IDs are wrong or deleted, so "
            "those games' admins will never be granted their role:\n"
            + "\n".join(f"• {problem}" for problem in bad_id)
        )


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
