"""Weekly scene health digest posted to #admin-digest."""

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


def format_game_section(
    game_name: str,
    dormant: list,
    unassigned: list,
    deactivated: list,
    mentions: dict[str, list[str]],
) -> str | None:
    """Render one game's block of the weekly digest, or None if it has nothing to say.

    Pure (no DB, no Discord), so it can be exercised without either (see tests/).
    Rows are anything supporting ``row["key"]``: asyncpg Records in production,
    plain dicts in tests.

    Shape is deliberately the pre-PR-4 digest plus one header line: with a single
    game covered, the message reads as it always has with a "**Digimon**" line on
    top. `mentions` maps a Discord user id to the scenes *within this game* they
    answer for, so a Gundam admin is never listed under Digimon's scenes.
    """
    sections = []

    if dormant:
        lines = []
        for r in dormant:
            if r["last_tournament"]:
                lines.append(
                    f"\u2022 **{r['display_name']}** \u2014 last tournament "
                    f"{r['last_tournament'].strftime('%b %d')}"
                )
            else:
                lines.append(f"\u2022 **{r['display_name']}** \u2014 no tournaments on record")
        sections.append("**Scenes with no tournaments in 60+ days:**\n" + "\n".join(lines))

    if unassigned:
        lines = [f"\u2022 **{r['display_name']}**" for r in unassigned]
        sections.append("**Scenes with no assigned admin:**\n" + "\n".join(lines))

    if deactivated:
        lines = [f"\u2022 **{r['name']}** ({r['scene_name']})" for r in deactivated]
        sections.append("**Stores deactivated this week:**\n" + "\n".join(lines))

    if not sections:
        return None

    if mentions:
        lines = []
        for uid, scenes in mentions.items():
            scene_list = ", ".join(scenes[:5])
            if len(scenes) > 5:
                scene_list += f" +{len(scenes) - 5} more"
            lines.append(f"<@{uid}> \u2014 {scene_list}")
        sections.append("\n".join(lines))

    return f"__**{game_name}**__\n\n" + "\n\n".join(sections)


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
        # Only run on Mondays
        if discord.utils.utcnow().weekday() != 0:
            return

        games = await db.get_games_with_scene_coverage(self.bot.pool)
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
        sections: list[str] = []
        for game in games:
            section = await self._game_section(game)
            if section:
                sections.append(section)

        # Skip if every game is healthy
        if not sections:
            log.info("Scene health digest: all clear, skipping post")
            return

        date_str = discord.utils.utcnow().strftime("%b %d, %Y")
        message = (
            f"\U0001f4ca **Weekly Scene Health Check \u2014 {date_str}**\n\n"
            + "\n\n".join(sections)
        )

        await post_webhook(config.WEBHOOK_ADMIN_DIGEST, message)

    async def _game_section(self, game) -> str | None:
        """Gather and render one game's section, or None if that game is healthy."""
        game_id = game["game_id"]

        dormant, unassigned, deactivated = await asyncio.gather(
            db.get_dormant_scenes(self.bot.pool, game_id, 60),
            db.get_unassigned_scenes(self.bot.pool, game_id),
            db.get_recently_deactivated_stores(self.bot.pool, game_id, 7),
        )

        if not dormant and not unassigned and not deactivated:
            return None

        scene_names: dict[int, str] = {}
        for r in dormant:
            scene_names[r["scene_id"]] = r["display_name"]
        for r in unassigned:
            scene_names[r["scene_id"]] = r["display_name"]
        for r in deactivated:
            scene_names.setdefault(r["scene_id"], r["scene_name"])

        mention_parts: dict[str, list[str]] = {}  # discord_user_id -> scene names
        for scene_id in set(scene_names.keys()):
            # Cascade scoped to this game, so a scene covered for Digimon but not for
            # Gundam pings the right team in each section.
            admins = db.select_tier_admins(
                await db.get_admins_for_scene(self.bot.pool, scene_id, game_id)
            )
            scene_name = scene_names.get(scene_id, "Unknown")

            seen: set[str] = set()
            for a in admins:
                did = a["discord_user_id"]
                if did and did not in seen:
                    seen.add(did)
                    mention_parts.setdefault(did, [])
                    mention_parts[did].append(scene_name)

        return format_game_section(
            game["short_name"] or game_id, dormant, unassigned, deactivated, mention_parts
        )

    @weekly_digest.before_loop
    async def before_digest(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))
