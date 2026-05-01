"""Slash commands: /admins, /roster, /scene, /help."""

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import db

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

    async def cog_load(self) -> None:
        await self._refresh_cache()
        self.refresh_scene_cache.start()

    async def cog_unload(self) -> None:
        self.refresh_scene_cache.cancel()

    async def _refresh_cache(self) -> None:
        scenes = await db.get_scenes(self.bot.pool)
        self.scene_cache = [(r["slug"], r["display_name"]) for r in scenes if r["slug"]]

    @tasks.loop(minutes=5)
    async def refresh_scene_cache(self) -> None:
        await self._refresh_cache()

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

    # --- /admins ---
    @app_commands.command(name="admins", description="View admins for a scene")
    @app_commands.describe(scene="Scene slug (start typing to search)")
    @app_commands.autocomplete(scene=scene_autocomplete)
    async def admins_cmd(self, interaction: discord.Interaction, scene: str) -> None:
        scene_row = await db.get_scene_by_slug(self.bot.pool, scene)
        if not scene_row:
            await interaction.response.send_message(f"Scene `{scene}` not found.", ephemeral=True)
            return

        admins = await db.get_admins_for_scene(self.bot.pool, scene_row["scene_id"])
        if not admins:
            await interaction.response.send_message(
                f"No admins found for **{scene_row['display_name']}**.", ephemeral=True
            )
            return

        lines = []
        for a in admins:
            emoji = ROLE_EMOJI.get(a["role"], "")
            mention = f"<@{a['discord_user_id']}>" if a["discord_user_id"] else a["username"]
            primary = " (primary)" if a["is_primary"] else ""
            assignment = f" *{a['assignment_type']}*" if a["assignment_type"] != "direct" else ""
            lines.append(f"{emoji} {mention}{primary}{assignment}")

        embed = discord.Embed(
            title=f"Admins — {scene_row['display_name']}",
            description="\n".join(lines),
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
            user_scenes = await db.get_admin_scenes_for_user(
                self.bot.pool, str(interaction.user.id)
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

        rows = await db.get_request_summary(self.bot.pool)
        if not rows:
            await interaction.response.send_message("No requests found.", ephemeral=True)
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
        await interaction.response.send_message(embed=embed, ephemeral=True)

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

        stats = await db.get_admin_stats(self.bot.pool, str(interaction.user.id))

        # Scene count
        user_scenes = await db.get_admin_scenes_for_user(
            self.bot.pool, str(interaction.user.id)
        )

        embed = discord.Embed(
            title=f"Stats — {interaction.user.display_name}",
            color=0x5865F2,
        )

        if user_scenes is not None:
            scene_count = "All (global)" if len(user_scenes) == 0 else str(len(user_scenes))
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

        await interaction.response.send_message(embed=embed, ephemeral=True)

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
                "**/admins** `[scene]` — View admins for a scene\n"
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
