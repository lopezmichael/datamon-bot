"""Shared utilities — kept separate from bot.py to avoid circular imports."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Iterable

import aiohttp
import discord
from discord.ext import tasks

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


def describe_exception(exc: BaseException) -> str:
    """`Type: text`, truncated to fit an alert."""
    detail = f"{type(exc).__name__}: {exc}".strip()
    if len(detail) > MAX_ALERT_ERROR_CHARS:
        detail = detail[:MAX_ALERT_ERROR_CHARS - 1] + "…"
    return detail


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

    It doubles as the loop's HEARTBEAT for `LoopWatchdog`: a run that ends in
    `failed()` or `recovered()` has completed, whatever its outcome, so both stamp
    `last_completed`. A run that re-raises a transient error for the library to
    retry stamps nothing, which is deliberate: a loop stuck retrying forever has
    stopped completing runs, and that is exactly what the watchdog reports.
    """

    def __init__(self, label: str, threshold: int = 1) -> None:
        self.label = label
        self.threshold = threshold
        self._consecutive = 0
        self._alerted = False
        # time.monotonic() of the last completed run; None until the first one.
        self.last_completed: float | None = None

    def beat(self) -> None:
        """Mark a run as completed without judging it.

        `failed()` and `recovered()` call this. Call it directly only for a tick
        that deliberately did nothing (the digest's non-Monday ticks), so the
        watchdog can tell an idle loop from a hung one.
        """
        self.last_completed = time.monotonic()

    async def failed(self, exc: BaseException) -> None:
        """Record a failed run, alerting once the threshold is reached."""
        self.beat()
        self._consecutive += 1
        if self._alerted or self._consecutive < self.threshold:
            return

        self._alerted = True
        detail = describe_exception(exc)
        runs = "run" if self._consecutive == 1 else "consecutive runs"
        await log_to_discord(
            f"⚠️ **{self.label}** failed {self._consecutive} {runs} — `{detail}`\n"
            "Silent until it recovers; `railway logs` has the traceback."
        )

    async def recovered(self) -> None:
        """Record a successful run, announcing recovery only if we alerted."""
        self.beat()
        was_alerted, failures = self._alerted, self._consecutive
        self._consecutive = 0
        self._alerted = False
        if was_alerted:
            await log_to_discord(
                f"✅ **{self.label}** recovered after {failures} failed runs."
            )


class IterationTimeout(Exception):
    """A loop run overran its `run_bounded` deadline and was cancelled.

    Deliberately NOT a TimeoutError. `TimeoutError` is a subclass of `OSError`,
    so a plain `asyncio.wait_for` timeout would be caught by the loop bodies'
    `except TRANSIENT_LOOP_EXCEPTIONS: raise` and handed to discord.ext.tasks,
    which retries it with backoff and tells nobody — the exact silence this
    exists to end. As a plain Exception it reaches `LoopFailureAlerter`.
    """


async def run_bounded(coro, seconds: float, label: str):
    """Await `coro`, cancelling it and raising `IterationTimeout` past `seconds`.

    Only OUR deadline becomes `IterationTimeout`. A TimeoutError raised from
    inside the work (an HTTP or DB client's own timeout) still propagates as
    itself, so the transient-retry classification of those is unchanged.
    """
    deadline = asyncio.timeout(seconds)
    try:
        async with deadline:
            return await coro
    except TimeoutError:
        if deadline.expired():
            raise IterationTimeout(
                f"{label} run exceeded {format_duration(seconds)} and was cancelled"
            ) from None
        raise


class ProblemSetAlert:
    """Post a recurring problem to #bot-log when it appears or changes, not every run.

    For the conditions a loop hits on every tick until a human fixes something
    (a role the bot cannot grant, a members cache that never filled): alerting
    per run would post every five minutes, and logging only to stdout is how
    these went unseen. So: post the full list when it differs from the last one
    posted, and once more when it clears. A restart forgets what was posted and
    reposts once, which is the right amount of noise for a fresh process.
    """

    MAX_LINES = 15

    def __init__(self, title: str, hint: str = "") -> None:
        self.title = title
        self.hint = hint
        self._posted: frozenset[str] = frozenset()

    def transition(self, problems: Iterable[str]) -> str | None:
        """The message to post for this run's problems, or None. Pure apart from state."""
        current = frozenset(problems)
        if current == self._posted:
            return None
        self._posted = current
        if not current:
            return f"✅ Cleared: **{self.title}**."

        lines = sorted(current)
        shown = "\n".join(f"• {line}" for line in lines[:self.MAX_LINES])
        if len(lines) > self.MAX_LINES:
            shown += f"\n…and {len(lines) - self.MAX_LINES} more"
        hint = f"\n{self.hint}" if self.hint else ""
        return (
            f"⚠️ **{self.title}**\n{shown}{hint}\n"
            "-# Posted again only when this list changes."
        )

    async def update(self, problems: Iterable[str]) -> None:
        message = self.transition(problems)
        if message:
            await log_to_discord(message)


def format_duration(seconds: float) -> str:
    """Human-sized duration for an alert: `45 s`, `20 min`, `3.5 h`, `2.0 d`."""
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} d"


# How often the watchdog looks. Cheap: a few attribute reads per loop.
WATCHDOG_CHECK_SECONDS = 60
# Restart backoff for a loop that keeps dying: first restart immediately, then
# 1, 2, 4 … minutes, capped at an hour.
RESTART_BACKOFF_BASE_SECONDS = 60
RESTART_BACKOFF_CAP_SECONDS = 3600
# Consecutive deaths at which the one escalation ("it is crash-looping") posts.
# The first death always posts; between and after, stdout only.
CRASH_LOOP_ALERT_AT = 3


def restart_delay(deaths: int) -> float:
    """Seconds to wait before restarting a loop that has died `deaths` times in a row."""
    if deaths <= 1:
        return 0.0
    return float(min(RESTART_BACKOFF_BASE_SECONDS * 2 ** (deaths - 2), RESTART_BACKOFF_CAP_SECONDS))


def is_stale(last_completed: float | None, since: float, now: float, stale_after: float) -> bool:
    """True when nothing has completed for longer than `stale_after`.

    Measured from the later of the last completed run and `since` (when the
    loop was last started), so a loop is not stale merely for being new.
    """
    reference = max(last_completed or since, since)
    return now - reference > stale_after


def describe_exit(task: asyncio.Task | None) -> str:
    """Why a loop's task is no longer running, for the death alert."""
    if task is None:
        return "never started"
    if not task.done():
        return "still running"
    if task.cancelled():
        # discord.ext.tasks treats any CancelledError as a shutdown: it stops the
        # loop WITHOUT calling the `.error` handler, so this is the death no
        # error hook would ever have seen.
        return "CancelledError — cancelled by something other than shutdown"
    exc = task.exception()
    if exc is None:
        return "returned without an error"
    return describe_exception(exc)


@dataclass
class _Watched:
    loop: tasks.Loop
    alerter: LoopFailureAlerter
    stale_after: float
    since: float
    deaths: int = 0
    restart_at: float | None = None
    stale_alerted: bool = False


class LoopWatchdog:
    """Notices a periodic loop that has died or hung, says so, and restarts the dead.

    The incident that built this: role_sync stopped doing anything on or about
    2026-10-27 while the process kept running, and nobody noticed for three and
    a half weeks, because a dead loop is silent by construction. Its last word
    (if it had one) went to stdout, and Railway keeps a week of that.
    discord.ext.tasks ends a loop in three ways nothing in the body can report:

    * an exception escapes the body — `.error` fires, the loop stops for good;
    * a stray `CancelledError` — the loop stops and `.error` is NOT called;
    * an await that never returns — the loop is "running" and does nothing.

    And a fourth that only looks alive: a body re-raising a transient error
    (TimeoutError is an OSError) is retried with backoff forever, silently.

    So the watchdog judges from outside, every minute: a watched loop whose task
    has ended is DEAD (alert with the exit reason, restart with backoff); one
    whose `LoopFailureAlerter` heartbeat is older than `stale_after` is STALE
    (alert once, announce when it completes again). Stale loops are not
    restarted: cancelling mid-run is not safe for every body, and role_sync, the
    one where it is, bounds its own runs with `run_bounded` instead.

    The watchdog is a plain task, not a `tasks.loop`, so it is not subject to
    the failure modes above: it absorbs stray cancellations, alerts if its own
    check keeps failing, and restarts itself from a done-callback if it ever
    ends without `stop()`.
    """

    def __init__(self) -> None:
        self._watched: dict[int, _Watched] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._check_alerter = LoopFailureAlerter("Loop watchdog", threshold=3)
        self._death_post: asyncio.Task | None = None

    # -- registration --------------------------------------------------------

    def watch(self, loop: tasks.Loop, alerter: LoopFailureAlerter, stale_after: float) -> None:
        """Start `loop` and keep watching it. `stale_after` is in seconds.

        Call this in place of `loop.start()` (enforced by test_conventions).
        Pick `stale_after` at a few times the loop's real period, so one slow
        run is not news but a stopped one is.
        """
        self._watched[id(loop)] = _Watched(loop, alerter, stale_after, since=time.monotonic())
        loop.start()

    def unwatch(self, loop: tasks.Loop) -> None:
        """Stop watching and cancel `loop` — for `cog_unload`."""
        self._watched.pop(id(loop), None)
        loop.cancel()

    # -- the check -----------------------------------------------------------

    async def check(self, now: float) -> None:
        """One pass over every watched loop. `now` is a time.monotonic() value."""
        for w in list(self._watched.values()):
            label = w.alerter.label
            if not w.loop.is_running():
                await self._handle_dead(w, label, now)
                continue

            if w.deaths and w.alerter.last_completed is not None and w.alerter.last_completed > w.since:
                await log_to_discord(
                    f"✅ **{label}** is running again — completed a run after "
                    f"{w.deaths} restart{'s' if w.deaths != 1 else ''}."
                )
                w.deaths = 0

            stale = is_stale(w.alerter.last_completed, w.since, now, w.stale_after)
            if stale and not w.stale_alerted:
                w.stale_alerted = True
                reference = max(w.alerter.last_completed or w.since, w.since)
                log.error("%s has not completed a run in %.0fs", label, now - reference)
                await log_to_discord(
                    f"⏳ **{label}** has not completed a run in "
                    f"{format_duration(now - reference)} (expected within "
                    f"{format_duration(w.stale_after)}). It is either hung on an await or "
                    "stuck retrying a transient error — `railway logs` will show which. "
                    "Not restarted automatically."
                )
            elif not stale and w.stale_alerted:
                w.stale_alerted = False
                await log_to_discord(f"✅ **{label}** completed a run again.")

    async def _handle_dead(self, w: _Watched, label: str, now: float) -> None:
        if w.restart_at is None:
            # Newly noticed. A death supersedes a pending stale alert, and the
            # restart below resets `since`, so a stale recovery would be a lie.
            w.stale_alerted = False
            w.deaths += 1
            reason = describe_exit(w.loop.get_task())
            delay = restart_delay(w.deaths)
            w.restart_at = now + delay
            log.error("%s stopped (%s); death %d, restarting in %.0fs", label, reason, w.deaths, delay)
            if w.deaths == 1:
                await log_to_discord(
                    f"💀 **{label}** stopped — `{reason}`. Restarting it now; "
                    "`railway logs` has the traceback."
                )
            elif w.deaths == CRASH_LOOP_ALERT_AT:
                await log_to_discord(
                    f"💀 **{label}** has died {w.deaths} times in a row (latest: "
                    f"`{reason}`). Restarting with backoff up to "
                    f"{format_duration(RESTART_BACKOFF_CAP_SECONDS)}; silent until it "
                    "completes a run."
                )

        if now >= w.restart_at:
            w.restart_at = None
            w.since = now
            try:
                w.loop.start()
            except RuntimeError:
                # Something else already restarted it; that is fine.
                log.info("%s was already running at restart time", label)

    # -- the watchdog's own lifecycle ---------------------------------------

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="datamon-loop-watchdog")
        self._task.add_done_callback(self._on_done)

    def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self.check(time.monotonic())
                await self._check_alerter.recovered()
                await asyncio.sleep(WATCHDOG_CHECK_SECONDS)
            except asyncio.CancelledError:
                # A real cancel (stop(), interpreter shutdown) is a request on
                # THIS task. Anything else is a stray from something we awaited,
                # and must not end the one loop that watches the others.
                if asyncio.current_task().cancelling():
                    raise
                log.error("Loop watchdog absorbed a stray CancelledError")
                await asyncio.sleep(WATCHDOG_CHECK_SECONDS)
            except Exception as exc:
                log.exception("Loop watchdog check failed")
                await self._check_alerter.failed(exc)
                await asyncio.sleep(WATCHDOG_CHECK_SECONDS)

    def _on_done(self, task: asyncio.Task) -> None:
        if self._stopping or task.cancelled():
            return
        reason = describe_exit(task)
        log.error("Loop watchdog ended unexpectedly (%s) — restarting it", reason)
        # Held on self: asyncio keeps only a weak reference to a bare task.
        self._death_post = task.get_loop().create_task(
            log_to_discord(f"💀 **Loop watchdog** ended unexpectedly — `{reason}`. Restarting it.")
        )
        self.start()


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
