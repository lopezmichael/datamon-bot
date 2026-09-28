"""Weekly scene health digest posted to #admin-digest.

# Name a scene once, the week it becomes a problem

The digest used to list every scene in a bad state, every Monday: 57 dormant, 40
unassigned, 31 of them in both lists, and 43 people tagged about scenes that had
been in that state for months. Nothing in it said what had changed, so nothing in
it was actionable, and the tags stopped meaning anything.

Now a scene is named only in the week it CROSSES a threshold — its game coverage
turns 30 (and 90) days old with no tournaments, its last tournament turns 60 days
old, or it turns 14 days old with no admin. Every other scene in those states is a
number on the "Standing" line, with a link to where the backlog is worked.

Crossing is computed, not remembered: a scene crossed a threshold this week if the
days since its date land in ``[threshold, threshold + 7)``. The digest runs every 7
days, so each scene lands in exactly one run's window per threshold, with no state
to store — which matters, because the bot may not write to the database. The cost:
a failed Monday loses that week's crossings from the named list (they still count
in Standing, and the loop's alerter reports the failure in #bot-log).
"""

import asyncio
import datetime
import logging

import discord
from discord.ext import commands, tasks

import config
import db
from utils import TRANSIENT_LOOP_EXCEPTIONS, LoopFailureAlerter, post_webhook

log = logging.getLogger(__name__)

# Run daily at 09:00 UTC, but only post on Mondays
DIGEST_TIME = datetime.time(hour=9, tzinfo=datetime.timezone.utc)

# The cadence, and so the width of every crossing window. Tied to the Monday gate
# in `weekly_digest`: change one without the other and scenes get named twice, or
# never.
WINDOW_DAYS = 7

# Days on the game with no tournament at which a scene is named. Two checkpoints:
# a month is "did onboarding stall?", a quarter is "is this scene real?".
NEVER_PLAYED_CHECKPOINTS = (30, 90)
# Days since the last tournament at which a scene counts as gone quiet.
QUIET_AFTER_DAYS = 60
# Days on the game without a direct admin before that is worth a name.
NO_ADMIN_AFTER_DAYS = 14

# Standing count key -> label. Each key is also the `?health=` value digilab-web's
# /admin/scenes filters on (src/lib/admin-scene-filters.ts), with the same
# thresholds, so a count here links to exactly the rows it counts.
STANDING_LABELS = (
    ("never_played", "never played"),
    ("quiet", f"quiet {QUIET_AFTER_DAYS}+ days"),
    ("no_admin", "without an admin"),
)


def _crossed(days: int, threshold: int) -> bool:
    """True if a count of days reached `threshold` within the last digest window."""
    return threshold <= days < threshold + WINDOW_DAYS


def classify_scene_health(
    scenes: list, today: datetime.date
) -> tuple[list[dict], dict[str, int]]:
    """Split one game's scenes into newly-crossed items and standing counts.

    Pure. `scenes` rows carry ``scene_id``, ``display_name``, ``joined_at`` (date),
    ``last_tournament`` (date or None) and ``has_admin`` — asyncpg Records from
    ``db.get_scene_health`` in production, dicts in tests.

    Returns ``(items, standing)``. An item is a scene that crossed at least one
    threshold this week, with every reason it did, so a scene that is both new-
    and-silent and unowned is one line, not two. ``standing`` counts every scene
    past each threshold, crossed this week or long ago.
    """
    items: list[dict] = []
    standing = {"never_played": 0, "quiet": 0, "no_admin": 0}

    for s in scenes:
        reasons: list[str] = []
        days_on = (today - s["joined_at"]).days if s["joined_at"] else 0

        last = s["last_tournament"]
        if last is None:
            if days_on >= NEVER_PLAYED_CHECKPOINTS[0]:
                standing["never_played"] += 1
            for checkpoint in NEVER_PLAYED_CHECKPOINTS:
                if _crossed(days_on, checkpoint):
                    reasons.append(f"{checkpoint} days in, no tournaments yet")
        else:
            quiet_days = (today - last).days
            if quiet_days >= QUIET_AFTER_DAYS:
                standing["quiet"] += 1
            if _crossed(quiet_days, QUIET_AFTER_DAYS):
                reasons.append(f"no tournaments since {last.strftime('%b %d')}")

        if not s["has_admin"]:
            if days_on >= NO_ADMIN_AFTER_DAYS:
                standing["no_admin"] += 1
            if _crossed(days_on, NO_ADMIN_AFTER_DAYS):
                reasons.append("no admin assigned")

        if reasons:
            items.append({
                "scene_id": s["scene_id"],
                "display_name": s["display_name"],
                "reasons": reasons,
            })

    return items, standing


def _mentions(ids: list[str] | None) -> str:
    return " · " + " ".join(f"<@{i}>" for i in ids) if ids else ""


def format_game_section(
    game_name: str,
    items: list[dict],
    closures: list,
    standing: dict[str, int],
    mentions: dict[int, list[str]],
    scenes_url: str,
) -> str | None:
    """Render one game's block of the weekly digest, or None if nothing is new.

    Pure (no DB, no Discord), so it can be exercised without either (see tests/).
    `mentions` maps a scene id to the Discord ids answerable for it in THIS game,
    and each line carries its own, so a tagged admin sees which line is theirs.

    `scenes_url` is the game's /admin/scenes. Each Standing count links to it
    filtered (`?health=<key>`); the angle brackets suppress Discord's preview card.

    Standing counts alone never produce a section: they are context for what is
    new, not news. A game where nothing crossed a threshold and no store closed
    stays out of the digest entirely.
    """
    if not items and not closures:
        return None

    sections = []

    if items:
        lines = [
            f"• **{i['display_name']}** — {'; '.join(i['reasons'])}"
            + _mentions(mentions.get(i["scene_id"]))
            for i in items
        ]
        sections.append("**New this week:**\n" + "\n".join(lines))

    if closures:
        lines = [
            f"• **{r['name']}** ({r['scene_name']})"
            + _mentions(mentions.get(r["scene_id"]))
            for r in closures
        ]
        sections.append("**Stores closed this week:**\n" + "\n".join(lines))

    parts = [
        f"[{standing[key]} {label}](<{scenes_url}?health={key}>)"
        for key, label in STANDING_LABELS
        if standing.get(key)
    ]
    if parts:
        # `-#` is Discord's subtext: present, but visibly not the point.
        sections.append(f"-# Standing: {' · '.join(parts)}")

    return f"__**{game_name}**__\n" + "\n\n".join(sections)


class Digest(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Weekly loop — the next data point is seven days out. Alert on the
        # first failure or the digest silently misses a week.
        self._alerter = LoopFailureAlerter("Weekly digest loop")

    async def cog_load(self) -> None:
        self.weekly_digest.start()

    async def cog_unload(self) -> None:
        self.weekly_digest.cancel()

    @tasks.loop(time=DIGEST_TIME)
    async def weekly_digest(self) -> None:
        # The loop ticks daily and only Monday is a real run, so the weekday gate
        # lives HERE rather than inside _run_digest: with it further in, every
        # non-Monday tick reached `recovered()` and posted "the digest recovered"
        # the morning after a Monday failure, having done nothing at all.
        if discord.utils.utcnow().weekday() != 0:
            return

        # An exception escaping the loop body kills the loop permanently
        try:
            await self._run_digest()
        except TRANSIENT_LOOP_EXCEPTIONS:
            # Let discord.ext.tasks retry these with its own backoff. Everything
            # that can raise these runs before the webhook post (which swallows
            # its own errors), so a retry can never double-post.
            raise
        except Exception as exc:
            log.exception("Weekly digest run failed")
            await self._alerter.failed(exc)
            return
        await self._alerter.recovered()

    async def _run_digest(self) -> None:
        # Coverage-derived, NOT `games.is_active` — that flag is FALSE for Gundam
        # while Gundam has 16 active scenes, so the pre-fix digest silently
        # reported on Digimon only and read as a complete picture. See the block
        # above `db.get_live_games`.
        games = await db.get_live_games(self.bot.pool)
        if not games:
            # "Couldn't find out" must never render as "nothing to report". Every
            # section below is per-game, so an empty game list produces a silent,
            # healthy-looking no-post, the exact shape that hid four broken card
            # syncs. Raise instead: the loop's alerter says so in #bot-log.
            raise RuntimeError(
                "No active games with scene coverage: cannot build the weekly digest"
            )

        # All DB work happens before the post, so a transient failure retried by the
        # loop can never double-post.
        #
        # Per game, not per run: one game's bad query (a dropped column, a permission
        # gap on a table only it touches) used to cost the whole digest, including the
        # games that were fine. A failed section becomes a visible line instead, so
        # the week is still reported AND the breakage is legible to the admins reading
        # it \u2014 not just to whoever greps #bot-log.
        sections: list[str] = []
        failures: list[str] = []
        for game in games:
            label = game["short_name"] or game["game_id"]
            try:
                section = await self._game_section(game)
            except TRANSIENT_LOOP_EXCEPTIONS:
                # Network-class: let the loop's own backoff retry the whole run.
                raise
            except Exception as exc:
                log.exception("Digest section failed for game %s", game["game_id"])
                failures.append(
                    f"\u26a0\ufe0f **{label}** \u2014 section failed "
                    f"(`{type(exc).__name__}`); check `railway logs`"
                )
                continue
            if section:
                sections.append(section)

        # Skip only if nothing crossed a threshold in any game AND nothing broke.
        # A standing backlog alone is not news (see the module docstring).
        if not sections and not failures:
            log.info("Scene health digest: nothing new this week, skipping post")
            return

        date_str = discord.utils.utcnow().strftime("%b %d, %Y")
        message = (
            f"\U0001f4ca **Weekly Scene Health Check \u2014 {date_str}**\n\n"
            + "\n\n".join(sections + failures)
        )

        await post_webhook(config.WEBHOOK_ADMIN_DIGEST, message)

    async def _game_section(self, game) -> str | None:
        """Gather and render one game's section, or None if nothing is new for it."""
        game_id = game["game_id"]

        scenes, closures = await asyncio.gather(
            db.get_scene_health(self.bot.pool, game_id),
            db.get_store_closures(self.bot.pool, game_id, WINDOW_DAYS),
        )
        items, standing = classify_scene_health(
            scenes, discord.utils.utcnow().date()
        )

        if not items and not closures:
            return None

        # A scene nobody owns locally falls through to the cascade's global tier, which
        # counts platform admins. They are game SMEs, not scene triage, so that
        # fallback pings the super admins instead — same owners as the forums.
        super_admin_ids = await db.get_super_admin_discord_ids(self.bot.pool)

        mentions: dict[int, list[str]] = {}  # scene_id -> discord user ids
        for scene_id in {i["scene_id"] for i in items} | {r["scene_id"] for r in closures}:
            # Cascade scoped to this game, so a scene covered for Digimon but not for
            # Gundam pings the right team in each section.
            admins = db.select_tier_admins(
                await db.get_admins_for_scene(self.bot.pool, scene_id, game_id)
            )
            if admins and admins[0]["tier"] == 3:
                admin_ids = super_admin_ids
            else:
                admin_ids = [a["discord_user_id"] for a in admins]
            # dict.fromkeys: de-dupe, keep order (a row can repeat across tiers).
            mentions[scene_id] = list(dict.fromkeys(d for d in admin_ids if d))

        # The game's own host: digilab-web 301s /admin/* from a game host to the
        # admin host, query intact, and sets the admin game cookie on the way — so
        # a Gundam link opens the Gundam list whichever game the admin last viewed.
        scenes_url = f"{config.game_site_url(game_id)}/admin/scenes"

        return format_game_section(
            game["short_name"] or game_id, items, closures, standing, mentions, scenes_url
        )

    @weekly_digest.before_loop
    async def before_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))
