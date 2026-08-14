"""Slash commands: /admins, /roster, /scene, /help."""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db
from utils import TRANSIENT_LOOP_EXCEPTIONS, LoopFailureAlerter

log = logging.getLogger(__name__)

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
        self.game_cache: dict[str, str] = {}  # game_id -> short_name
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
        # slash command must not spend a round trip resolving a display name.
        games = await db.get_active_games(self.bot.pool)
        self.game_cache = {r["game_id"]: r["short_name"] or r["game_id"] for r in games}

    def _game_label(self, game_id: str | None) -> str:
        """Display name for a game id, falling back to the id itself."""
        if not game_id:
            return "All games"
        return self.game_cache.get(game_id, game_id)

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

    # Shared autocomplete for the game argument
    async def game_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        lower = current.lower()
        return [
            app_commands.Choice(name=name, value=game_id)
            for game_id, name in sorted(self.game_cache.items())
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

        # Validate against the cached list so a typo gets a real answer instead of a
        # confidently empty one. Skipped when the cache is cold, since an empty cache
        # is our problem, not the caller's.
        if game and self.game_cache and game not in self.game_cache:
            await interaction.response.send_message(
                f"Unknown game `{game}`.", ephemeral=True
            )
            return

        # One query either way: passing game=None asks the cascade for every game at
        # once and tags each tier 1-2 row with the game its assignment belongs to.
        admins = await db.get_admins_for_scene(self.bot.pool, scene_row["scene_id"], game)
        if not admins:
            await interaction.response.send_message(
                f"No admins found for **{scene_row['display_name']}**.", ephemeral=True
            )
            return

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

        blocks = [
            f"**{self._game_label(game_id)}**\n" + "\n".join(lines)
            for game_id, lines in sorted(by_game.items())
        ]
        if globals_:
            label = "Global" if not game else f"Global ({self._game_label(game)})"
            blocks.append(f"**{label}**\n" + "\n".join(globals_))

        title = f"Admins — {scene_row['display_name']}"
        if game:
            title += f" ({self._game_label(game)})"
        embed = discord.Embed(
            title=title,
            description="\n\n".join(blocks),
            color=0x5865F2,
        )
        embed.set_footer(text=f"🔴 Platform  🟡 Regional  🟢 Scene")
        await interaction.response.send_message(embed=embed)

    # --- /roster ---
    @app_commands.command(name="roster", description="View stores and tournaments for a scene (admin only)")
    @app_commands.describe(scene="Scene slug (start typing to search)")
    @app_commands.autocomplete(scene=scene_autocomplete)
    async def roster_cmd(self, interaction: discord.Interaction, scene: str) -> None:
        scene_row = await db.get_scene_by_slug(self.bot.pool, scene)
        if not scene_row:
            await interaction.response.send_message(f"Scene `{scene}` not found.", ephemeral=True)
            return

        # Permission check: Platform Admin role OR admin for this scene
        has_platform = any(r.id == config.ROLE_PLATFORM_ADMIN for r in interaction.user.roles)
        if not has_platform:
            # game_id=None on purpose: /roster shows the scene's stores and lifetime
            # tournament counts, which are properties of the shared geography rather
            # than of one game. Scoping the permission check to a game would deny an
            # admin the roster of a scene they demonstrably administer. Same set of
            # scenes as before PR 4.
            user_scenes = await db.get_admin_scenes_for_user(
                self.bot.pool, str(interaction.user.id), None
            )
            # None = not an admin; empty list = global admin (super/platform)
            has_access = (
                user_scenes is not None
                and (len(user_scenes) == 0 or scene_row["scene_id"] in user_scenes)
            )
            if not has_access:
                await interaction.response.send_message(
                    "You need admin access for this scene.", ephemeral=True
                )
                return

        stores = await db.get_stores_for_scene(self.bot.pool, scene_row["scene_id"])
        if not stores:
            await interaction.response.send_message(
                f"No stores found for **{scene_row['display_name']}**.", ephemeral=True
            )
            return

        lines = []
        for s in stores:
            status = "" if s["is_active"] else " *(inactive)*"
            location = f"{s['city']}, {s['state']}" if s["state"] else s["city"]
            lines.append(f"**{s['name']}** — {location} ({s['tournament_count']} tournaments){status}")

        embed = discord.Embed(
            title=f"Roster — {scene_row['display_name']}",
            description="\n".join(lines),
            color=0x57F287,
        )
        await interaction.response.send_message(embed=embed)

    # --- /scene ---
    @app_commands.command(name="scene", description="View scene info and stats")
    @app_commands.describe(scene="Scene slug (start typing to search)")
    @app_commands.autocomplete(scene=scene_autocomplete)
    async def scene_cmd(self, interaction: discord.Interaction, scene: str) -> None:
        scene_row = await db.get_scene_by_slug(self.bot.pool, scene)
        if not scene_row:
            await interaction.response.send_message(f"Scene `{scene}` not found.", ephemeral=True)
            return

        stats = await db.get_scene_stats(self.bot.pool, scene_row["scene_id"])

        location_parts = []
        if scene_row["state_region"]:
            location_parts.append(scene_row["state_region"])
        if scene_row["country"]:
            location_parts.append(scene_row["country"])
        location = ", ".join(location_parts) or "—"

        embed = discord.Embed(
            title=scene_row["display_name"],
            url=f"{config.APP_BASE_URL}/?scene={scene_row['slug']}",
            color=0xED4245,
        )
        embed.add_field(name="Location", value=location, inline=True)
        if scene_row["continent"]:
            embed.add_field(name="Continent", value=scene_row["continent"].replace("_", " ").title(), inline=True)
        if stats:
            embed.add_field(name="Stores", value=str(stats["store_count"]), inline=True)
            embed.add_field(name="Tournaments", value=str(stats["tournament_count"]), inline=True)
            embed.add_field(name="Players", value=str(stats["player_count"]), inline=True)
        embed.set_footer(text="DigiLab — Digimon TCG Tournament Tracker")

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

        rows = await db.get_request_summary(self.bot.pool)
        if not rows:
            await interaction.followup.send("No requests found.", ephemeral=True)
            return

        total_open = 0
        lines = []
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
            lines.append(line)

        embed = discord.Embed(
            title=f"Open Requests — {total_open} total",
            description="\n".join(lines),
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
        scene_rows = await db.get_admin_scene_rows_for_user(
            self.bot.pool, str(interaction.user.id), None
        )

        embed = discord.Embed(
            title=f"Stats — {interaction.user.display_name}",
            color=0x5865F2,
        )

        if scene_rows is not None:
            if len(scene_rows) == 0:
                scene_count = "All (global)"
            else:
                distinct_scenes = {r["scene_id"] for r in scene_rows}
                per_game: dict[str, int] = {}
                for r in scene_rows:
                    per_game[r["game_id"]] = per_game.get(r["game_id"], 0) + 1
                scene_count = str(len(distinct_scenes))
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
            description="Discord bot for DigiLab — Digimon TCG Tournament Tracker",
            color=0x5865F2,
        )
        embed.add_field(
            name="Commands",
            value=(
                "**/admins** `[scene]` `[game]` — View admins for a scene\n"
                "**/roster** `[scene]` — View stores & tournaments (admin only)\n"
                "**/requests** — Open request summary (admin only)\n"
                "**/mystats** — Your admin stats (admin only)\n"
                "**/scene** `[scene]` — View scene info and stats\n"
                "**/help** — Show this message"
            ),
            inline=False,
        )
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
