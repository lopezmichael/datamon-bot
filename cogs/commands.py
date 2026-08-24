"""Slash commands: /admins, /roster, /scene, /help."""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
from utils import TRANSIENT_LOOP_EXCEPTIONS, LoopFailureAlerter

log = logging.getLogger(__name__)

# Discord's hard cap on an embed description. Over it the API answers 400 and the
# interaction shows "the application did not respond", so a scene with a lot of
# admins would break the command outright rather than show a long list.
EMBED_DESCRIPTION_LIMIT = 4096

# Room kept free for the "not shown" notice, sized for its longest rendering.
_TRUNCATION_NOTICE = "\n\n*+{n} more not shown*"
_TRUNCATION_RESERVE = len(_TRUNCATION_NOTICE.format(n=99999))


def fit_embed_description(
    blocks: list[str], limit: int = EMBED_DESCRIPTION_LIMIT
) -> str:
    """Join `blocks` into an embed description that fits, reporting what was cut.

    Whole blocks are kept or dropped, so a game's section never renders half a
    roster with no indication; the count in the notice is of dropped *entries*
    (every line under a block's header). A first block that cannot fit on its own
    is hard-truncated rather than dropped, since returning an empty description is
    the same 400 this function exists to avoid.

    Pure, so it is tested in tests/test_embed_fit.py.
    """
    kept: list[str] = []
    used = 0
    dropped = 0
    for block in blocks:
        separator = 2 if kept else 0  # the "\n\n" between blocks
        if used + separator + len(block) + _TRUNCATION_RESERVE <= limit:
            used += separator + len(block)
            kept.append(block)
        else:
            dropped += block.count("\n")  # lines below the header

    if not kept and blocks:
        kept = [blocks[0][: max(limit - _TRUNCATION_RESERVE, 0)]]
        dropped = sum(b.count("\n") for b in blocks) - kept[0].count("\n")

    text = "\n\n".join(kept)
    if dropped > 0:
        text += _TRUNCATION_NOTICE.format(n=dropped)
    return text


ROLE_EMOJI = {
    "super_admin": "\U0001f534",      # 🔴
    "platform_admin": "\U0001f534",   # 🔴 (same as super_admin)
    "regional_admin": "\U0001f7e1",   # 🟡
    "scene_admin": "\U0001f7e2",      # 🟢
}


class Commands(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.scene_cache: list[tuple[str, str]] = []  # (slug, display_name)
        # 5-minute loop, and the cache stays usable while stale — two
        # consecutive failures before alerting.
        self._alerter = LoopFailureAlerter("Scene cache refresh loop", threshold=2)

    async def cog_load(self) -> None:
        await self._refresh_cache()
        self.refresh_scene_cache.start()

    async def cog_unload(self) -> None:
        self.refresh_scene_cache.cancel()

    async def _refresh_cache(self) -> None:
        scenes = await db.get_scenes(self.bot.pool)
        self.scene_cache = [(r["slug"], r["display_name"]) for r in scenes if r["slug"]]
        # Games ride the same loop: the list changes once a year at most, and a
        # slash command must not spend a round trip resolving a display name. The
        # cache lives on the bot (games.py) because thread_watcher and the digest
        # need the same answers this cog does.
        await self.bot.games.refresh(self.bot.pool)

    def _game_label(self, game_id: str | None) -> str:
        return self.bot.games.label(game_id)

    async def _game_ok(
        self, interaction: discord.Interaction, game: str | None
    ) -> bool:
        """Validate a `game` argument, replying and returning False if it is bogus.

        A cold cache must not become a silent yes: refresh once rather than
        skipping validation, or an arbitrary string binds, matches no assignment
        row, and renders a plausible-looking empty answer for a game that does not
        exist.

        Validated against `known_ids` — every game that exists — NOT the live set.
        Autocomplete offers only live games, so this path is for someone who typed
        an id by hand, and rejecting a real-but-not-yet-covered game would
        reproduce the exact symptom this change fixed: "Unknown game" about a game
        DigiLab has. A typo is still rejected; a real game answers honestly with
        nothing.
        """
        await self.bot.games.ensure(self.bot.pool)
        if game and game not in self.bot.games.known_ids():
            await interaction.response.send_message(
                f"Unknown game `{game}`.", ephemeral=True
            )
            return False
        return True

    @tasks.loop(minutes=5)
    async def refresh_scene_cache(self) -> None:
        # An exception escaping the loop body kills the loop permanently,
        # freezing scene autocomplete on a stale cache
        try:
            await self._refresh_cache()
        except TRANSIENT_LOOP_EXCEPTIONS:
            # Let discord.ext.tasks retry these with its own backoff
            raise
        except Exception as exc:
            log.exception("Scene cache refresh failed")
            await self._alerter.failed(exc)
            return
        await self._alerter.recovered()

    @refresh_scene_cache.before_loop
    async def before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    # Shared autocomplete for scene slug
    async def scene_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        lower = current.lower()
        matches = [
            app_commands.Choice(name=display, value=slug)
            for slug, display in self.scene_cache
            if lower in slug.lower() or lower in display.lower()
        ]
        return matches[:25]

    # Shared autocomplete for the game argument. Offers the LIVE games — the ones
    # with actual scene coverage — in coverage order, so the game this server
    # mostly runs is the first suggestion. One Piece / Fusion World / Union Arena
    # exist as catalogue rows with no scenes and are correctly not offered.
    async def game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        lower = current.lower()
        return [
            app_commands.Choice(name=name, value=game_id)
            for game_id, name in self.bot.games.live_choices()
            if lower in game_id.lower() or lower in name.lower()
        ][:25]

    # --- /admins ---
    @app_commands.command(name="admins", description="View admins for a scene")
    @app_commands.describe(
        scene="Scene slug (start typing to search)",
        game="Game to scope to (default: every game this scene is active for)",
    )
    @app_commands.autocomplete(scene=scene_autocomplete, game=game_autocomplete)
    async def admins_cmd(
        self, interaction: discord.Interaction, scene: str, game: str | None = None
    ) -> None:
        scene_row = await db.get_scene_by_slug(self.bot.pool, scene)
        if not scene_row:
            await interaction.response.send_message(f"Scene `{scene}` not found.", ephemeral=True)
            return

        if not await self._game_ok(interaction, game):
            return

        # game=None asks the cascade for every game at once and tags each tier 1-2
        # row with the game its assignment belongs to.
        admins = await db.get_admins_for_scene(self.bot.pool, scene_row["scene_id"], game)

        # Which games get a block. The cascade only knows games that HAVE an
        # assignment here, so the default answer also reads scene_games: a game the
        # scene is active for with nobody assigned must render as "no admins yet",
        # not vanish, or the reader cannot tell that apart from "not active here".
        if game:
            show_games = [game]
        else:
            show_games = [
                r["game_id"] for r in await db.get_games_for_scene(
                    self.bot.pool, scene_row["scene_id"]
                )
            ]

        # Tier 1-2 rows group under their game; tier 3 is the global fallback and is
        # not scene-keyed, so it gets its own trailing group.
        by_game: dict[str, list[str]] = {}
        globals_: list[str] = []
        for a in admins:
            emoji = ROLE_EMOJI.get(a["role"], "")
            mention = f"<@{a['discord_user_id']}>" if a["discord_user_id"] else a["username"]
            primary = " (primary)" if a["is_primary"] else ""
            assignment = f" *{a['assignment_type']}*" if a["assignment_type"] != "direct" else ""
            line = f"{emoji} {mention}{primary}{assignment}"
            if a["game_id"]:
                by_game.setdefault(a["game_id"], []).append(line)
            else:
                globals_.append(line)

        # Active-for-this-scene games first, in their own order, then any game that
        # has assignments without an active junction row (stale membership, worth
        # seeing rather than hiding).
        ordered = list(show_games) + [g for g in sorted(by_game) if g not in show_games]
        blocks = [
            f"**{self._game_label(gid)}**\n"
            + ("\n".join(by_game[gid]) if by_game.get(gid) else "*No admins assigned yet*")
            for gid in ordered
        ]
        if globals_:
            label = "Global" if not game else f"Global ({self._game_label(game)})"
            blocks.append(f"**{label}**\n" + "\n".join(globals_))

        if not blocks:
            await interaction.response.send_message(
                f"No admins found for **{scene_row['display_name']}**.", ephemeral=True
            )
            return

        title = f"Admins \u2014 {scene_row['display_name']}"
        if game:
            title += f" ({self._game_label(game)})"
        embed = discord.Embed(
            title=title,
            description=fit_embed_description(blocks),
            color=0x5865F2,
        )
        embed.set_footer(text=f"🔴 Platform  🟡 Regional  🟢 Scene")
        await interaction.response.send_message(embed=embed)

    # --- /roster ---
    @app_commands.command(name="roster", description="View stores and tournaments for a scene (admin only)")
    @app_commands.describe(
        scene="Scene slug (start typing to search)",
        game="Game to scope to (default: every game, tournament counts combined)",
    )
    @app_commands.autocomplete(scene=scene_autocomplete, game=game_autocomplete)
    async def roster_cmd(
        self, interaction: discord.Interaction, scene: str, game: str | None = None
    ) -> None:
        scene_row = await db.get_scene_by_slug(self.bot.pool, scene)
        if not scene_row:
            await interaction.response.send_message(f"Scene `{scene}` not found.", ephemeral=True)
            return

        # Permission check: Platform Admin role OR admin for this scene.
        #
        # The Discord role still counts HERE, unlike the resolve path in
        # cogs/reactions.py: /roster only displays stores and lifetime tournament
        # counts for shared geography, it writes nothing and it is not game-scoped,
        # so the flat badge is an adequate gate for a read. Authorization that
        # changes data asks the DB per game instead.
        has_platform = any(r.id == config.ROLE_PLATFORM_ADMIN for r in interaction.user.roles)
        if not has_platform:
            # game_id=None on purpose: a roster is a property of the shared geography
            # rather than of one game, so scoping the check would deny an admin the
            # roster of a scene they demonstrably administer. Same set of scenes as
            # before PR 4.
            access = await db.get_admin_access_for_user(
                self.bot.pool, str(interaction.user.id), None
            )
            if not access.covers(scene_row["scene_id"]):
                await interaction.response.send_message(
                    "You need admin access for this scene.", ephemeral=True
                )
                return

        # Game validated only after the permission check, so an unauthorized caller
        # gets the access denial rather than feedback about which games exist.
        if not await self._game_ok(interaction, game):
            return

        stores = await db.get_stores_for_scene(self.bot.pool, scene_row["scene_id"], game)
        if not stores:
            scope = f" for **{self._game_label(game)}**" if game else ""
            await interaction.response.send_message(
                f"No stores found for **{scene_row['display_name']}**{scope}.", ephemeral=True
            )
            return

        lines = []
        for s in stores:
            status = "" if s["is_active"] else " *(inactive)*"
            location = f"{s['city']}, {s['state']}" if s["state"] else s["city"]
            lines.append(f"**{s['name']}** — {location} ({s['tournament_count']} tournaments){status}")

        title = f"Roster — {scene_row['display_name']}"
        if game:
            title += f" ({self._game_label(game)})"
        embed = discord.Embed(
            title=title,
            description=fit_embed_description(["\n".join(lines)]),
            color=0x57F287,
        )
        # Say what the number covers. Unscoped, those tournament counts blend every
        # game the store runs — which is a defensible default for a roster but
        # reads as one game's figure under a bot that used to only have one.
        embed.set_footer(
            text=f"Tournament counts: {self._game_label(game)}"
            if game
            else "Tournament counts cover every game — pass `game:` to scope"
        )
        await interaction.response.send_message(embed=embed)

    # --- /scene ---
    @app_commands.command(name="scene", description="View scene info and stats")
    @app_commands.describe(
        scene="Scene slug (start typing to search)",
        game="Game to scope to (default: a breakdown per game the scene runs)",
    )
    @app_commands.autocomplete(scene=scene_autocomplete, game=game_autocomplete)
    async def scene_cmd(
        self, interaction: discord.Interaction, scene: str, game: str | None = None
    ) -> None:
        """Scene info card, per game.

        Stores, tournaments and players are all per-game facts — `tournaments`
        and `players` carry `game_id`, stores hang off the `store_games`
        junction — and this card used to blend them into one set of numbers under
        a footer that said "Digimon TCG Tournament Tracker". Austin is active for
        both games with 0 Digimon tournaments and 168 Gundam ones, so the card
        read "Tournaments 168 · Players 281" for a scene with no Digimon activity
        at all. Nothing about that display was flagged as approximate.

        So: with a game, everything is scoped to it. Without, the card shows one
        line per game the scene is active for — including games sitting at zero,
        which is the number that tells an admin the scene joined and never
        started.
        """
        scene_row = await db.get_scene_by_slug(self.bot.pool, scene)
        if not scene_row:
            await interaction.response.send_message(f"Scene `{scene}` not found.", ephemeral=True)
            return

        if not await self._game_ok(interaction, game):
            return

        location_parts = []
        if scene_row["state_region"]:
            location_parts.append(scene_row["state_region"])
        if scene_row["country"]:
            location_parts.append(scene_row["country"])
        location = ", ".join(location_parts) or "—"

        title = scene_row["display_name"]
        if game:
            title += f" ({self._game_label(game)})"
        embed = discord.Embed(
            title=title,
            url=f"{config.APP_BASE_URL}/?scene={scene_row['slug']}",
            color=0xED4245,
        )
        embed.add_field(name="Location", value=location, inline=True)
        if scene_row["continent"]:
            embed.add_field(name="Continent", value=scene_row["continent"].replace("_", " ").title(), inline=True)

        if game:
            stats = await db.get_scene_stats(self.bot.pool, scene_row["scene_id"], game)
            if stats:
                embed.add_field(name="Stores", value=str(stats["store_count"]), inline=True)
                embed.add_field(name="Tournaments", value=str(stats["tournament_count"]), inline=True)
                embed.add_field(name="Players", value=str(stats["player_count"]), inline=True)
        else:
            # One query, one row per game — no fan-out per game.
            rows = await db.get_scene_stats_by_game(self.bot.pool, scene_row["scene_id"])
            if rows:
                embed.add_field(
                    name="Games",
                    value=" · ".join(self._game_label(r["game_id"]) for r in rows),
                    inline=False,
                )
                for r in rows:
                    embed.add_field(
                        name=self._game_label(r["game_id"]),
                        value=(
                            f"{r['store_count']} stores\n"
                            f"{r['tournament_count']} tournaments\n"
                            f"{r['player_count']} players"
                        ),
                        inline=True,
                    )
            else:
                # No scene_games rows at all. Not the same as "no activity", and
                # the difference is actionable — the scene exists but is joined to
                # nothing, so no game's digest will ever mention it.
                embed.add_field(
                    name="Games",
                    value="*Not active for any game yet*",
                    inline=False,
                )

        embed.set_footer(text=self.bot.games.tagline())

        await interaction.response.send_message(embed=embed)

    # --- /requests ---
    @app_commands.command(name="requests", description="View open request summary (admin only)")
    async def requests_cmd(self, interaction: discord.Interaction) -> None:
        # Permission check: any DigiLab role
        has_role = any(r.id in config.DIGILAB_ROLE_IDS for r in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message(
                "You need admin access to view request stats.", ephemeral=True
            )
            return

        # Every response below is ephemeral and everything past here hits the DB,
        # so defer before the first query: a Neon cold start or a retried dead
        # connection can outrun Discord's 3-second interaction deadline, and past
        # it the reply is rejected outright with "the application did not
        # respond". Deferring buys 15 minutes. The permission check above runs
        # off cached Discord roles, so it still answers instantly.
        await interaction.response.defer(ephemeral=True)

        # Grouped by game, because the queue is no longer one team's. The query
        # returns only rows with open work, so an empty result means exactly one
        # thing.
        rows = await db.get_request_summary(self.bot.pool)
        if not rows:
            await interaction.followup.send("✅ Nothing open right now.", ephemeral=True)
            return

        total_open = 0
        by_game: dict[str, list[str]] = {}
        for r in rows:
            total_open += r["open_count"]
            label = r["request_type"].replace("_", " ").title()
            line = f"**{label}** — {r['open_count']} open"
            if r["oldest_open"]:
                days = (discord.utils.utcnow() - r["oldest_open"]).days
                line += f" (oldest: {days}d ago)"
            if r["avg_resolution_time"]:
                avg_hours = r["avg_resolution_time"].total_seconds() / 3600
                if avg_hours >= 24:
                    line += f" · avg resolve: {avg_hours / 24:.1f}d"
                else:
                    line += f" · avg resolve: {avg_hours:.1f}h"
            by_game.setdefault(r["game_id"], []).append(line)

        # Busiest game first, so whatever needs attention leads.
        ordered = sorted(by_game, key=lambda g: (-len(by_game[g]), g))
        blocks = [
            f"__**{self._game_label(gid)}**__\n" + "\n".join(by_game[gid])
            for gid in ordered
        ]

        embed = discord.Embed(
            title=f"Open Requests — {total_open} total",
            description=fit_embed_description(blocks),
            color=0xFEE75C,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- /mystats ---
    @app_commands.command(name="mystats", description="View your admin stats (admin only)")
    async def mystats_cmd(self, interaction: discord.Interaction) -> None:
        # Permission check: any DigiLab role
        has_role = any(r.id in config.DIGILAB_ROLE_IDS for r in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message(
                "You need admin access to view your stats.", ephemeral=True
            )
            return

        # Ephemeral throughout, and three queries follow — defer before the
        # first. See requests_cmd for why.
        await interaction.response.defer(ephemeral=True)

        stats = await db.get_admin_stats(self.bot.pool, str(interaction.user.id))

        # Scene count, across every game (rows carry the game each assignment
        # belongs to, so a multi-game admin gets a breakdown instead of a total
        # that quietly double-counts a scene they hold for two games).
        access = await db.get_admin_access_for_user(
            self.bot.pool, str(interaction.user.id), None
        )

        embed = discord.Embed(
            title=f"Stats — {interaction.user.display_name}",
            color=0x5865F2,
        )

        # Read the level, not the row count: 'scoped' with no rows is an admin with
        # no assignments anywhere, which is the opposite of "All (global)".
        if access.level == db.ADMIN_ACCESS_GLOBAL:
            embed.add_field(name="Scenes Managed", value="All (global)", inline=True)
        elif access.level == db.ADMIN_ACCESS_SCOPED:
            per_game: dict[str, int] = {}
            for r in access.rows:
                per_game[r["game_id"]] = per_game.get(r["game_id"], 0) + 1
            scene_count = str(len(access.scene_ids))
            if len(per_game) > 1:
                scene_count += "\n" + ", ".join(
                    f"{self._game_label(gid)}: {n}" for gid, n in sorted(per_game.items())
                )
            embed.add_field(name="Scenes Managed", value=scene_count, inline=True)

        if stats and stats["resolved_count"]:
            embed.add_field(name="Requests Resolved", value=str(stats["resolved_count"]), inline=True)

            if stats["avg_resolution_time"]:
                avg_hours = stats["avg_resolution_time"].total_seconds() / 3600
                if avg_hours >= 24:
                    avg_str = f"{avg_hours / 24:.1f} days"
                else:
                    avg_str = f"{avg_hours:.1f} hours"
                embed.add_field(name="Avg Resolution Time", value=avg_str, inline=True)

            if stats["first_resolved"]:
                embed.add_field(
                    name="Active Since",
                    value=stats["first_resolved"].strftime("%b %d, %Y"),
                    inline=True,
                )
            if stats["last_resolved"]:
                embed.add_field(
                    name="Last Resolved",
                    value=stats["last_resolved"].strftime("%b %d, %Y"),
                    inline=True,
                )
        else:
            embed.description = "No resolved requests yet — react \u2705 on a thread to get started!"

        await interaction.followup.send(embed=embed, ephemeral=True)

    # --- /help ---
    @app_commands.command(name="help", description="Show bot commands and info")
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Datamon Bot",
            description=f"Discord bot for {self.bot.games.tagline()}",
            color=0x5865F2,
        )
        embed.add_field(
            name="Commands",
            value=(
                "**/admins** `[scene]` `[game]` — View admins for a scene\n"
                "**/roster** `[scene]` `[game]` — View stores & tournaments (admin only)\n"
                "**/requests** — Open request summary, by game (admin only)\n"
                "**/mystats** — Your admin stats (admin only)\n"
                "**/scene** `[scene]` `[game]` — View scene info and stats\n"
                "**/help** — Show this message"
            ),
            inline=False,
        )
        # Naming the games explicitly, not just in the tagline: this is the one
        # command whose whole job is telling someone what the bot can do, and
        # "which games does this cover" is now part of that answer.
        games = self.bot.games.live_labels()
        if games:
            embed.add_field(name="Games covered", value=" · ".join(games), inline=False)
        embed.add_field(
            name="Features",
            value=(
                "• **Role Sync** — Keeps Discord roles in sync with DB admin roles\n"
                "• **React to Resolve** — React ✅ on a forum thread to resolve it\n"
                "• **Auto-Archive** — Resolved threads are archived after 48h\n"
                "• **Thread Watcher** — Posts instructions on new forum threads"
            ),
            inline=False,
        )
        embed.add_field(
            name="Links",
            value=f"[DigiLab]({config.APP_BASE_URL})",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Commands(bot))
