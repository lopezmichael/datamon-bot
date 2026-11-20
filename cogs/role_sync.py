"""Periodic role sync: DB admin roles ↔ Discord roles."""

import asyncio
import logging

import discord
from discord.ext import commands, tasks

import config
import db
from utils import (
    TRANSIENT_LOOP_EXCEPTIONS,
    LoopFailureAlerter,
    ProblemSetAlert,
    log_to_discord,
    run_bounded,
)

log = logging.getLogger(__name__)

# Game-role grants to make per tick. Every grant costs a Discord API call plus a
# 1-second courtesy sleep, and the first run after the roles are configured has
# ~200 to make — which would hold the 5-minute loop for three and a half minutes
# on top of whatever the tier pass is already doing. Capping spreads that first
# convergence over a few ticks (~25 minutes) and costs nothing afterwards, since
# a converged run makes zero grants. The remainder is always logged: a cap that
# truncates silently reads as "everyone is synced".
MAX_GAME_ROLE_GRANTS_PER_RUN = 40

# Hard ceiling on one run. A converged run takes seconds; the slowest legitimate
# one (a mass reverse pass after a config change, ~1 s per removal) takes a few
# minutes, and a cut-short run simply continues next tick, since every pass is
# idempotent. Without this, one await that never returns holds the loop forever
# with nothing logged — the shape of the 2026-10-27 incident. With it, the hang
# becomes an `IterationTimeout` the alerter reports, and the loop carries on.
RUN_TIMEOUT_SECONDS = 10 * 60


class RoleSync(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # 5-minute loop: require two consecutive failures so a single blip that
        # heals on the next tick never reaches #bot-log.
        self._alerter = LoopFailureAlerter("Role sync loop", threshold=2)
        # The two ways a run used to give up in silence, every five minutes,
        # until a human happened to read stdout. Posted when they start or
        # change, not per run. An admin whose Discord account is not in the
        # guild is NOT one of these: that is normal, and stays unreported.
        self._skip_alert = ProblemSetAlert("Role sync is skipping every run")
        self._forbidden_alert = ProblemSetAlert(
            "Role sync is not allowed to make these changes",
            hint="Usually the target role sits above the bot's own role, or the "
            "bot lost Manage Roles. Retried every run.",
        )

    async def cog_load(self) -> None:
        # 5-minute loop; four missed runs is news.
        self.bot.watchdog.watch(self.sync_roles, self._alerter, stale_after=20 * 60)

    async def cog_unload(self) -> None:
        self.bot.watchdog.unwatch(self.sync_roles)

    @tasks.loop(minutes=5)
    async def sync_roles(self) -> None:
        # An exception escaping the loop body kills the loop (the watchdog
        # restarts it, but a handled failure is the one that carries context)
        try:
            await run_bounded(self._run_sync(), RUN_TIMEOUT_SECONDS, "Role sync")
        except TRANSIENT_LOOP_EXCEPTIONS:
            # Let discord.ext.tasks retry these with its own backoff
            raise
        except Exception as exc:
            log.exception("Role sync run failed")
            await self._alerter.failed(exc)
            return
        await self._alerter.recovered()

    async def _run_sync(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if not guild:
            log.warning("Guild %s not found", config.GUILD_ID)
            await self._skip_alert.update([f"Guild {config.GUILD_ID} is not in the bot's cache"])
            return

        if len(guild.members) < 2:
            log.warning("Guild has <2 cached members — skipping sync (check GUILD_MEMBERS intent)")
            await self._skip_alert.update([
                "The guild has fewer than 2 cached members — check the "
                "GUILD_MEMBERS privileged intent"
            ])
            return
        await self._skip_alert.update([])

        admins = await db.get_active_admins(self.bot.pool)
        changes: list[str] = []
        forbidden: list[str] = []

        # Build lookup: discord_user_id → expected role ID
        admin_lookup: dict[int, int] = {}
        for admin in admins:
            if admin["discord_user_id"]:
                try:
                    discord_id = int(admin["discord_user_id"])
                    expected_role = config.ROLE_MAP.get(admin["role"])
                    if expected_role:
                        admin_lookup[discord_id] = expected_role
                except ValueError:
                    continue

        # Forward pass: DB → Discord
        for discord_id, expected_role_id in admin_lookup.items():
            member = guild.get_member(discord_id)
            if not member:
                continue

            expected_role = guild.get_role(expected_role_id)
            if not expected_role:
                continue

            # Add expected role if missing
            if expected_role not in member.roles:
                try:
                    await member.add_roles(expected_role, reason="Datamon role sync")
                    changes.append(f"Added **{expected_role.name}** to {member.mention}")
                    await asyncio.sleep(1)
                except discord.Forbidden:
                    log.warning("Cannot add role %s to %s (insufficient permissions)", expected_role.name, member)
                    forbidden.append(_forbidden_line("add", expected_role, member))

            # Remove other DigiLab roles that don't match
            for role in member.roles:
                if role.id in config.DIGILAB_ROLE_IDS and role.id != expected_role_id:
                    try:
                        await member.remove_roles(role, reason="Datamon role sync")
                        changes.append(f"Removed **{role.name}** from {member.mention} (expected {expected_role.name})")
                        await asyncio.sleep(1)
                    except discord.Forbidden:
                        log.warning("Cannot remove role %s from %s", role.name, member)
                        forbidden.append(_forbidden_line("remove", role, member))

        # Reverse pass: Discord → DB (remove roles from non-admins)
        for member in guild.members:
            if member.id in admin_lookup or member.bot:
                continue

            for role in member.roles:
                if role.id in config.DIGILAB_ROLE_IDS:
                    try:
                        await member.remove_roles(role, reason="Datamon role sync — not in active admins")
                        changes.append(f"Removed **{role.name}** from {member.mention} (not in active admins)")
                        await asyncio.sleep(1)
                    except discord.Forbidden:
                        log.warning("Cannot remove role %s from %s", role.name, member)
                        forbidden.append(_forbidden_line("remove", role, member))

        await self._sync_game_roles(guild, changes, forbidden)
        await self._forbidden_alert.update(forbidden)

        # Update bot status
        scene_count = await db.get_scene_count(self.bot.pool)
        activity = discord.Activity(type=discord.ActivityType.watching, name=f"{scene_count} scenes")
        await self.bot.change_presence(activity=activity)

        # Log changes only
        if changes:
            msg = "**Role Sync**\n" + "\n".join(changes)
            await log_to_discord(msg)
            log.info("Role sync: %d changes", len(changes))

    async def _sync_game_roles(
        self, guild: discord.Guild, changes: list[str], forbidden: list[str]
    ) -> None:
        """Grant each admin the Discord role for every game they administer.

        **Additive only. This never removes a game role, and that is the whole
        design.** The tier roles above are the bot's to own — it is the only thing
        that grants them, so it can safely take them back. A game role is not: the
        server's onboarding flow hands the same role to any member who says they
        play the game, and most of the older membership picked theirs by hand.
        A reverse pass here would read "not an admin for Gundam" and strip the
        @Gundam role off several hundred players who never claimed to be one.

        So the rule is one-directional: being an admin for a game is a reason to
        HAVE the role, never the only reason. Someone who stops administering a
        game keeps the role, the same as any other member who plays it; if that
        ever needs undoing it is a deliberate act by a human, not a loop.

        A game with no `DISCORD_GAME_ROLE_<GAME>` env var is skipped silently —
        that is the state every game starts in.
        """
        if not config.GAME_ROLES:
            return

        # discord_user_id → the game roles that user's assignments earn them.
        wanted: dict[int, set[int]] = {}
        for row in await db.get_admin_game_ids(self.bot.pool):
            role_id = config.GAME_ROLES.get(row["game_id"])
            if not role_id:
                continue
            try:
                discord_id = int(row["discord_user_id"])
            except (TypeError, ValueError):
                continue
            wanted.setdefault(discord_id, set()).add(role_id)

        granted = 0
        pending = 0
        for discord_id, role_ids in wanted.items():
            member = guild.get_member(discord_id)
            if not member:
                continue

            missing = role_ids - {r.id for r in member.roles}
            for role_id in sorted(missing):
                role = guild.get_role(role_id)
                if not role:
                    # A configured ID that is not a role in this guild. Warn once
                    # per pass per member rather than alerting: the boot-time
                    # forum check is the model, but roles have no equivalent yet.
                    log.warning("Game role %s not found in guild", role_id)
                    continue
                if granted >= MAX_GAME_ROLE_GRANTS_PER_RUN:
                    pending += 1
                    continue
                try:
                    await member.add_roles(role, reason="Datamon game-role sync")
                    changes.append(f"Added **{role.name}** to {member.mention} (game admin)")
                    granted += 1
                    await asyncio.sleep(1)
                except discord.Forbidden:
                    log.warning("Cannot add game role %s to %s", role.name, member)
                    forbidden.append(_forbidden_line("add", role, member))

        if pending:
            log.info(
                "Game-role sync capped at %d grants this run — %d still pending, "
                "next tick continues",
                MAX_GAME_ROLE_GRANTS_PER_RUN, pending,
            )

    @sync_roles.before_loop
    async def before_sync(self) -> None:
        await self.bot.wait_until_ready()


def _forbidden_line(action: str, role: discord.Role, member: discord.Member) -> str:
    """One stable line per refused change, so the dedupe can compare runs.

    Names the member by display name and ID rather than a mention: this is a
    permissions problem for whoever reads #bot-log, not something to ping the
    admin about.
    """
    return f"{action} **{role.name}** — {member.display_name} ({member.id})"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleSync(bot))
