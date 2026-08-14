"""Datamon Bot — Database connection pool and query helpers."""

import asyncio
import logging

import asyncpg

import config

log = logging.getLogger(__name__)

# Neon drops connections that have sat idle, and the socket goes half-open: the
# pool still believes the connection is good (`is_closed()` is False, so the
# holder hands it straight out) and only the next query finds the corpse. In
# production that surfaced as ConnectionResetError [Errno 104] on the socket
# read, re-raised by asyncpg as ConnectionDoesNotExistError ("connection was
# closed in the middle of operation"), on the 5-minute role_sync and scene-cache
# loops.
#
# Deliberately narrow. asyncpg.InterfaceError belongs to the *other* family —
# "pool is closing", wrong argument count, "another operation is in progress" —
# all permanent for the call in question, so retrying them only delays the real
# error and files it in the logs under the wrong cause. Retry what we have
# actually observed, and let everything else surface as itself.
_DEAD_CONNECTION_ERRORS = (
    asyncpg.PostgresConnectionError,  # incl. ConnectionDoesNotExistError
    ConnectionError,                  # bare [Errno 104] before asyncpg wraps it
)

# Attempts *after* the first. More than one because the two 5-minute loops both
# gate on wait_until_ready() and so fire together: with queries in flight on
# separate connections, one retry can hit a second corpse before reaching a live
# one. Single-threaded, one retry would always be enough.
_DEAD_CONNECTION_RETRIES = 2

# Deliberately tiny. The failed connection is already discarded by the time we
# retry, so there is nothing to wait *for* — and the slash commands in
# cogs/commands.py query the DB before answering the interaction, with no
# defer() anywhere, so every second spent sleeping eats into Discord's 3-second
# response deadline.
_DEAD_CONNECTION_RETRY_DELAY = 0.05

# Note on max_inactive_connection_lifetime: leave it at asyncpg's 300s default.
# Setting it below the 5-minute loop interval looks like prevention but measures
# as a straight loss — asyncpg's idle sweep does not honour min_size (nothing
# refills the pool after `_deactivate_inactive_connection` nulls the holder), so
# a shorter lifetime leaves the pool empty for the rest of every cycle and makes
# each tick, and any slash command landing in that window, pay a fresh
# TCP+TLS+SCRAM handshake. Connections to our -pooler endpoint were measured
# alive at 420s idle, so it prevents nothing: PgBouncer re-attaches the backend
# transparently. The retry below is what actually handles the episodic drops.


async def create_pool() -> asyncpg.Pool:
    """Create a connection pool to Neon PostgreSQL with retry logic."""
    for attempt in range(3):
        try:
            pool = await asyncpg.create_pool(
                host=config.NEON_HOST,
                database=config.NEON_DATABASE,
                user=config.NEON_USER,
                password=config.NEON_PASSWORD,
                ssl="require",
                min_size=2,
                max_size=5,
                # The retry below only fires when a dead socket fails *fast* (an
                # RST comes back). If Neon or a load balancer blackholes the
                # connection instead, the query waits on TCP retransmit — minutes
                # — and retrying cannot help because no exception is ever raised
                # to catch. This bounds that hang.
                #
                # Sized to bound the hang, NOT to meet Discord's interaction
                # deadline — deferring is what protects that. Measured worst case
                # over every helper here is ~1.0s (get_active_admins, the
                # role_sync query), so 10s is ~10x headroom and still leaves room
                # for a Neon compute cold start. Do not tighten this toward the
                # 3-second interaction budget: it would spuriously kill the
                # 5-minute loops, which have no deadline at all.
                command_timeout=10.0,
            )
            log.info("Database pool created (attempt %d)", attempt + 1)
            return pool
        except Exception:
            if attempt == 2:
                raise
            wait = 2**attempt  # 1s, 2s
            log.warning("DB pool creation failed (attempt %d), retrying in %ds", attempt + 1, wait)
            await asyncio.sleep(wait)


async def _run(op, *args, **kwargs):
    """Run a pool operation, retrying if the pooled connection was already dead.

    A failed query leaves its holder with no connection, and `Pool._queue` is a
    LifoQueue — so the retry reclaims that same holder and it opens a fresh
    connection on acquire. Everything routed through here must therefore be safe
    to run more than once.
    """
    for attempt in range(_DEAD_CONNECTION_RETRIES + 1):
        try:
            return await op(*args, **kwargs)
        except _DEAD_CONNECTION_ERRORS:
            if attempt == _DEAD_CONNECTION_RETRIES:
                raise
            log.warning(
                "Dead DB connection on %s (attempt %d/%d) — retrying",
                getattr(op, "__name__", op), attempt + 1, _DEAD_CONNECTION_RETRIES + 1,
            )
            await asyncio.sleep(_DEAD_CONNECTION_RETRY_DELAY)


async def _fetch(pool: asyncpg.Pool, query: str, *args, **kwargs) -> list[asyncpg.Record]:
    return await _run(pool.fetch, query, *args, **kwargs)


async def _fetchrow(pool: asyncpg.Pool, query: str, *args, **kwargs) -> asyncpg.Record | None:
    return await _run(pool.fetchrow, query, *args, **kwargs)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def get_active_admins(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """All active admins with their Discord user IDs and roles.

    Resolves role from game_admin_roles (new) → user.is_super_admin → admin_users.role (legacy).
    """
    return await _fetch(pool,
        """
        SELECT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u.is_platform_admin = TRUE THEN 'platform_admin' END,
                   gar.role,
                   au.role
               ) AS role
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles gar ON gar.user_id = u.id AND gar.game_id = 'digimon'
        WHERE au.is_active = TRUE
        """
    )


async def get_scenes(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """All active metro/online scenes for autocomplete cache."""
    return await _fetch(pool,
        """
        SELECT scene_id, slug, display_name, country, state_region, continent
        FROM scenes
        WHERE scene_type IN ('metro', 'online') AND is_active = TRUE
        ORDER BY display_name
        """
    )


async def get_scene_by_slug(pool: asyncpg.Pool, slug: str) -> asyncpg.Record | None:
    return await _fetchrow(pool,
        """
        SELECT scene_id, slug, display_name, country, state_region, continent,
               latitude, longitude
        FROM scenes
        WHERE slug = $1 AND is_active = TRUE
        """,
        slug,
    )


# ---------------------------------------------------------------------------
# Tier 3 — the global fallback
# ---------------------------------------------------------------------------
#
# POLICY, locked 2026-08-13 with PR 4 (game-aware bot): **super admins answer for
# every game, always; platform admins answer only for the games they hold a
# `game_admin_roles` row for.** Before this the fallback was a flat "every active
# super/platform admin", which was harmless while every scene assignment was
# digimon — but tiers 1-2 are game-filtered now, so a Gundam report on a scene
# with no Gundam admin falls through to tier 3, and an unfiltered tier 3 would
# mass-ping the whole Digimon platform team for it.
#
# THIS PREDICATE MUST STAY MIRRORED with digilab-web's
# `src/lib/admin-digest-queries.ts` (getSceneAdminCandidates tier 3 +
# getGlobalAdminDiscordIds). That file is the behavioral source of truth for the
# cascade and, as of this writing, still carries the OLD unfiltered tier 3 with a
# comment saying it is faithful to this bot. Web ships its Phase 5 first, then
# this. A one-sided change here means one side pings people the other does not.
#
# The grandfather arm is a migration crutch, not policy: an admin flagged
# platform/super the legacy way (`admin_users.role`, `user.is_platform_admin`)
# who holds NO game_admin_roles row at all predates per-game roles, so excluding
# them would silently shrink today's Digimon fallback. Delete the arm once every
# platform admin holds an explicit per-game row.
def _global_admin_predicate(au: str, u: str, game: str) -> str:
    """SQL fragment for "is this admin in the tier-3 global fallback for `game`?".

    `au` / `u` are the query's aliases for admin_users / "user"; `game` is a
    placeholder like ``"$2"`` (NULL = "any game", used by the un-scoped callers).
    Every argument is written by this module — none is ever user input.
    """
    return f"""(
               -- Super admins answer for every game, always.
               {au}.role = 'super_admin'
               OR {u}.is_super_admin = TRUE
               -- Platform (or super) admins holding a role in THIS game.
               OR EXISTS (
                   SELECT 1 FROM game_admin_roles g
                   WHERE g.user_id = {u}.id
                     AND ({game}::text IS NULL OR g.game_id = {game}::text)
                     AND g.role IN ('platform_admin', 'super_admin')
               )
               -- Grandfather: legacy platform flag, no per-game rows at all.
               OR (({au}.role = 'platform_admin' OR {u}.is_platform_admin = TRUE)
                   AND NOT EXISTS (
                       SELECT 1 FROM game_admin_roles g2 WHERE g2.user_id = {u}.id
                   ))
           )"""


async def get_admins_for_scene(
    pool: asyncpg.Pool, scene_id: int, game_id: str | None
) -> list[asyncpg.Record]:
    """Get admins responsible for a scene *for one game*, as a geographic cascade.

    ``game_id`` is required, not defaulted, so every caller has to say which game it
    is asking about. Pass the request row's ``game_id``; pass None only for a display
    surface that wants every game at once (``/admins`` with no game argument), which
    tags each tier 1-2 row with the game its assignment belongs to.

    Returns rows tagged with an ``assignment_type`` and a numeric ``tier`` so callers can
    apply scene -> region -> global precedence (see ``select_tier_admins``):

    - tier 1 ("scene"): ``direct`` admins of the scene, plus ``child_metro`` admins for a
      ``state`` rollup scene (admins of metros within that state). This is what fixes the
      common "report lands on a state/country rollup, which has no direct admin" case where
      the cascade would otherwise fall straight through to global. Country rollups do NOT
      fan out to child metros (would mass-ping every admin in the country).
    - tier 2 ("regional"): regional admins matching the scene's country / state.
    - tier 3 ("global"): the fallback described above ``_global_admin_predicate``.

    Game scoping, tier by tier:
    - tiers 1-2 filter ``admin_user_scenes.game_id`` / ``admin_regions.game_id`` and
      resolve the role from that game's ``game_admin_roles`` row;
    - tier 3 goes through ``_global_admin_predicate``;
    - ``scenes`` carries no game column (shared geography, per-game membership lives in
      ``scene_games``), so no branch filters the scene itself.

    Every row in the three scoped tables is 'digimon' today, so with a 'digimon'
    argument this returns exactly what the pre-PR-4 query returned.
    """
    return await _fetch(pool,
        f"""
        -- Direct scene admins (tier 1)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u0.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u0.is_platform_admin = TRUE THEN 'platform_admin' END,
                   gar0.role, au.role
               ) AS role,
               aus.is_primary, 'direct' AS assignment_type, 1 AS tier,
               aus.game_id::text AS game_id
        FROM admin_user_scenes aus
        JOIN admin_users au ON aus.user_id = au.user_id
        LEFT JOIN "user" u0 ON u0.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles gar0
          ON gar0.user_id = u0.id AND gar0.game_id = aus.game_id
        WHERE aus.scene_id = $1 AND au.is_active = TRUE
          AND ($2::text IS NULL OR aus.game_id = $2::text)

        UNION

        -- Child-metro admins for a state rollup scene (tier 1)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN uc.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN uc.is_platform_admin = TRUE THEN 'platform_admin' END,
                   garc.role, au.role
               ) AS role,
               FALSE AS is_primary, 'child_metro' AS assignment_type, 1 AS tier,
               aus.game_id::text AS game_id
        FROM scenes parent
        JOIN scenes child
          ON child.scene_type = 'metro'
         AND child.is_active = TRUE
         AND child.country = parent.country
         AND child.state_region = parent.state_region
        JOIN admin_user_scenes aus ON aus.scene_id = child.scene_id
        JOIN admin_users au ON aus.user_id = au.user_id
        LEFT JOIN "user" uc ON uc.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles garc
          ON garc.user_id = uc.id AND garc.game_id = aus.game_id
        WHERE parent.scene_id = $1 AND parent.scene_type = 'state' AND au.is_active = TRUE
          AND ($2::text IS NULL OR aus.game_id = $2::text)

        UNION

        -- Regional admins, country match + optional state match (tier 2)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u1.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u1.is_platform_admin = TRUE THEN 'platform_admin' END,
                   gar1.role, au.role
               ) AS role,
               FALSE AS is_primary, 'regional' AS assignment_type, 2 AS tier,
               ar.game_id::text AS game_id
        FROM admin_regions ar
        JOIN admin_users au ON ar.user_id = au.user_id
        LEFT JOIN "user" u1 ON u1.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles gar1
          ON gar1.user_id = u1.id AND gar1.game_id = ar.game_id
        JOIN scenes s ON s.scene_id = $1
        WHERE au.is_active = TRUE
          AND ($2::text IS NULL OR ar.game_id = $2::text)
          AND ar.country = s.country
          AND (ar.state_region IS NULL OR ar.state_region = s.state_region)

        UNION

        -- Global fallback (tier 3). Not game-keyed as a ROW — the game restriction
        -- is in the predicate — so game_id is NULL and callers grouping by game
        -- must render these separately.
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u2.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u2.is_platform_admin = TRUE THEN 'platform_admin' END,
                   au.role
               ) AS role,
               FALSE AS is_primary, 'global' AS assignment_type, 3 AS tier,
               NULL::text AS game_id
        FROM admin_users au
        LEFT JOIN "user" u2 ON u2.legacy_admin_id = au.user_id
        WHERE au.is_active = TRUE
          AND {_global_admin_predicate("au", "u2", "$2")}

        ORDER BY tier, role, username
        """,
        scene_id,
        game_id,
    )


def select_tier_admins(admins: list[asyncpg.Record]) -> list[asyncpg.Record]:
    """Apply scene -> region -> global precedence to ``get_admins_for_scene`` rows.

    Returns the admins at the lowest ``tier`` that has at least one mentionable member
    (a non-null ``discord_user_id``). This is the single source of truth for "who should
    actually be pinged" so a covered metro never also pings regional/global admins.

    A row may appear under more than one tier (e.g. a direct admin who is also a global
    admin); de-dupe by ``discord_user_id`` is left to the caller, which also dedupes
    against whoever was already mentioned in the thread's starter message.
    """
    if not admins:
        return []
    for tier in (1, 2, 3):
        in_tier = [a for a in admins if a["tier"] == tier and a["discord_user_id"]]
        if in_tier:
            return in_tier
    return []


async def get_stores_for_scene(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    return await _fetch(pool,
        """
        SELECT s.store_id, s.name, s.city, s.state, s.is_active,
               COUNT(t.tournament_id) AS tournament_count
        FROM stores s
        LEFT JOIN tournaments t ON t.store_id = s.store_id
        WHERE s.scene_id = $1
        GROUP BY s.store_id, s.name, s.city, s.state, s.is_active
        ORDER BY s.name
        """,
        scene_id,
    )


async def get_scene_stats(pool: asyncpg.Pool, scene_id: int) -> asyncpg.Record | None:
    return await _fetchrow(pool,
        """
        SELECT
            (SELECT COUNT(*) FROM stores WHERE scene_id = $1 AND is_active = TRUE) AS store_count,
            (SELECT COUNT(*) FROM tournaments t
             JOIN stores s ON t.store_id = s.store_id
             WHERE s.scene_id = $1) AS tournament_count,
            (SELECT COUNT(*) FROM players WHERE home_scene_id = $1 AND is_active = TRUE) AS player_count
        """,
        scene_id,
    )


async def get_request_by_thread(pool: asyncpg.Pool, thread_id: str) -> asyncpg.Record | None:
    """The request row behind a forum thread.

    ``game_id`` is what makes the rest of the bot game-aware: thread_watcher, nudge
    and reactions all reach the cascade through this row, so the game travels with
    the request instead of being guessed. It is NOT NULL DEFAULT 'digimon' in the
    schema (since 2026-08-10) and coalesced anyway, because a NULL reaching a
    ``game_id = $n`` predicate matches nothing and would silently empty a mention
    list rather than fail loudly.
    """
    return await _fetchrow(pool,
        """
        SELECT id, request_type, scene_id, status, discord_username, discord_thread_id,
               COALESCE(game_id, 'digimon') AS game_id
        FROM admin_requests
        WHERE discord_thread_id = $1
        """,
        thread_id,
    )


async def resolve_request(pool: asyncpg.Pool, thread_id: str, resolved_by: str) -> bool:
    """Mark a request as resolved. Returns True if a row was updated.

    The bot's only write, and the reason `_run` demands idempotence: the
    ``status != 'resolved'`` guard makes a repeat a no-op, so a retry can never
    overwrite the original ``resolved_at`` / ``resolved_by``.

    There is one case the retry cannot resolve honestly: if the UPDATE commits
    and the connection then dies before we see the command tag, the retry matches
    nothing and this returns False, so `cogs/reactions.py` skips the tag and the
    confirmation message. Don't try to detect that by re-reading the row —
    "already resolved by this same user" is not distinguishable from a concurrent
    resolve, and treating it as success posts a duplicate ✅ into the thread and
    #bot-log. The archiver's `_heal_thread` pass already closes this gap: it
    finds resolved-but-untagged threads hourly and tags them. A missing
    confirmation message for up to an hour beats a duplicate one now.
    """
    result = await _run(
        pool.execute,
        """
        UPDATE admin_requests
        SET status = 'resolved', resolved_at = NOW(), resolved_by = $2
        WHERE discord_thread_id = $1 AND status != 'resolved'
        """,
        thread_id,
        resolved_by,
    )
    return result == "UPDATE 1"


async def get_global_admin_discord_ids(
    pool: asyncpg.Pool, game_id: str | None = None
) -> list[str]:
    """Discord user IDs for the active global admins of ``game_id``.

    Same tier-3 membership test as ``get_admins_for_scene`` (shared, so the two can
    never drift): super admins always, platform admins for the game they hold a role
    in, plus the legacy grandfather arm.

    ``game_id=None`` means "any game" and is the pre-PR-4 behavior. Pass it only where
    there is genuinely no game in hand — a manually created forum thread, which has no
    request row at all. Scene-less *requests* do have a game; pass theirs.
    """
    rows = await _fetch(pool,
        f"""
        SELECT DISTINCT au.discord_user_id
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        WHERE au.is_active = TRUE
          AND au.discord_user_id IS NOT NULL
          AND {_global_admin_predicate("au", "u", "$1")}
        """,
        game_id,
    )
    return [r["discord_user_id"] for r in rows if r["discord_user_id"]]


async def get_request_summary(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Get open request counts, oldest open, and avg resolution time per request_type."""
    return await _fetch(pool,
        """
        SELECT request_type,
               COUNT(*) FILTER (WHERE status != 'resolved') AS open_count,
               MIN(submitted_at) FILTER (WHERE status != 'resolved') AS oldest_open,
               COUNT(*) FILTER (WHERE status = 'resolved') AS resolved_count,
               AVG(resolved_at - submitted_at) FILTER (WHERE status = 'resolved') AS avg_resolution_time
        FROM admin_requests
        GROUP BY request_type
        ORDER BY open_count DESC
        """
    )


async def get_games_with_scene_coverage(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Active games that actually have scenes, most-covered first.

    The weekly digest's game list. Membership is the ``scene_games`` junction —
    ``scenes`` is shared geography with no game column — so a game with no active
    scene rows contributes no section rather than an empty one.

    Ordering is by coverage, not by a hardcoded game name: whichever game has the
    most scenes leads the digest, which keeps Digimon first today without the bot
    knowing that Digimon is special.
    """
    return await _fetch(pool,
        """
        SELECT g.game_id, g.short_name, x.scene_count
        FROM games g
        JOIN LATERAL (
            SELECT COUNT(*)::int AS scene_count
            FROM scene_games sg
            JOIN scenes s ON s.scene_id = sg.scene_id
            WHERE sg.game_id = g.game_id AND sg.is_active = TRUE
              AND s.is_active = TRUE AND s.scene_type IN ('metro', 'online')
        ) x ON TRUE
        WHERE g.is_active = TRUE AND x.scene_count > 0
        ORDER BY x.scene_count DESC, g.game_id
        """
    )


async def get_active_games(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every active game. Powers the `/admins` game argument's autocomplete."""
    return await _fetch(pool,
        "SELECT game_id, short_name FROM games WHERE is_active = TRUE ORDER BY game_id"
    )


async def get_games_for_scene(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    """Active games this scene is active for, via the ``scene_games`` junction."""
    return await _fetch(pool,
        """
        SELECT g.game_id, g.short_name
        FROM scene_games sg
        JOIN games g ON g.game_id = sg.game_id
        WHERE sg.scene_id = $1 AND sg.is_active = TRUE AND g.is_active = TRUE
        ORDER BY g.game_id
        """,
        scene_id,
    )


async def get_dormant_scenes(
    pool: asyncpg.Pool, game_id: str, days: int = 60
) -> list[asyncpg.Record]:
    """Scenes with no tournaments *of this game* in the last N days.

    Both halves are game-scoped: which scenes count (``scene_games``) and which
    tournaments count (``tournaments.game_id``). Without the second one a scene whose
    only recent event was another game's reads as healthy for every game.
    """
    return await _fetch(pool,
        """
        SELECT s.scene_id, s.display_name,
               MAX(t.event_date) AS last_tournament
        FROM scenes s
        JOIN scene_games sg ON sg.scene_id = s.scene_id
                           AND sg.game_id = $2::text AND sg.is_active = TRUE
        LEFT JOIN stores st ON st.scene_id = s.scene_id
        LEFT JOIN tournaments t ON t.store_id = st.store_id AND t.game_id = $2::text
        WHERE s.scene_type IN ('metro', 'online') AND s.is_active = TRUE
        GROUP BY s.scene_id, s.display_name
        HAVING MAX(t.event_date) IS NULL OR MAX(t.event_date) < CURRENT_DATE - $1 * INTERVAL '1 day'
        ORDER BY MAX(t.event_date) NULLS FIRST
        """,
        days,
        game_id,
    )


async def get_unassigned_scenes(pool: asyncpg.Pool, game_id: str) -> list[asyncpg.Record]:
    """Scenes active for this game with no direct admin assignment *for this game*.

    An admin assigned to a scene for Digimon does not cover it for Gundam, so the
    ``admin_user_scenes`` probe carries the game too. Every assignment row is
    'digimon' today, so a 'digimon' call returns exactly the pre-PR-4 list.
    """
    return await _fetch(pool,
        """
        SELECT s.scene_id, s.display_name
        FROM scenes s
        JOIN scene_games sg ON sg.scene_id = s.scene_id
                           AND sg.game_id = $1::text AND sg.is_active = TRUE
        WHERE s.scene_type IN ('metro', 'online') AND s.is_active = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM admin_user_scenes aus
              JOIN admin_users au ON aus.user_id = au.user_id
              WHERE aus.scene_id = s.scene_id AND aus.game_id = $1::text
                AND au.is_active = TRUE
          )
        ORDER BY s.display_name
        """,
        game_id,
    )


async def get_recently_deactivated_stores(
    pool: asyncpg.Pool, game_id: str, days: int = 7
) -> list[asyncpg.Record]:
    """Stores deactivated in the last N days, for the games they served.

    ``stores.is_active`` is global (a closed shop is closed for everyone), so the
    per-game filter is the ``store_games`` junction. A store carrying no junction
    rows at all predates that junction and is reported under every game rather than
    dropped — a duplicate line in a digest is recoverable, a store that silently
    stops being reported is not.
    """
    return await _fetch(pool,
        """
        SELECT st.store_id, st.name, st.city, st.state,
               s.scene_id, s.display_name AS scene_name
        FROM stores st
        JOIN scenes s ON st.scene_id = s.scene_id
        WHERE st.is_active = FALSE
          AND st.updated_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
          AND (
              EXISTS (
                  SELECT 1 FROM store_games sg
                  WHERE sg.store_id = st.store_id AND sg.game_id = $2::text
                    AND sg.is_active = TRUE
              )
              OR NOT EXISTS (
                  SELECT 1 FROM store_games sg2 WHERE sg2.store_id = st.store_id
              )
          )
        ORDER BY s.display_name, st.name
        """,
        days,
        game_id,
    )


async def get_admin_stats(pool: asyncpg.Pool, discord_user_id: str) -> asyncpg.Record:
    """Get resolution stats for an admin by their Discord user ID (resolved_by)."""
    return await _fetchrow(pool,
        """
        SELECT COUNT(*) AS resolved_count,
               AVG(resolved_at - submitted_at) AS avg_resolution_time,
               MIN(resolved_at) AS first_resolved,
               MAX(resolved_at) AS last_resolved
        FROM admin_requests
        WHERE resolved_by = $1 AND status = 'resolved'
        """,
        discord_user_id,
    )


async def get_scene_count(pool: asyncpg.Pool) -> int:
    row = await _fetchrow(pool,
        "SELECT COUNT(*) AS cnt FROM scenes WHERE scene_type IN ('metro', 'online') AND is_active = TRUE"
    )
    return row["cnt"]


async def get_admin_scene_rows_for_user(
    pool: asyncpg.Pool, discord_user_id: str, game_id: str | None
) -> list[asyncpg.Record] | None:
    """Scenes a user is admin for (direct + regional), each tagged with its game.

    ``game_id`` is required, not defaulted, so every caller states what it is asking:

    - a game id scopes both halves — the role resolution (that game's
      ``game_admin_roles`` row) and the assignment reads
      (``admin_user_scenes.game_id`` / ``admin_regions.game_id``). This is the
      authorization question: "may this person resolve *this request*", so pass the
      request's own game.
    - None asks "any game", the pre-PR-4 shape, for display and for game-neutral
      surfaces like ``/roster`` (stores and tournaments belong to the scene, which is
      shared geography). It never narrows anyone's access relative to today.

    Returns None if the user is not an active admin at all. Returns an empty list for
    global admins (super/platform), meaning "all scenes" — callers must keep treating
    empty as global access, not as "no assignments".
    """
    user = await _fetchrow(pool,
        """
        SELECT au.user_id,
               COALESCE(
                   CASE WHEN u.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u.is_platform_admin = TRUE THEN 'platform_admin' END,
                   -- The strongest per-game role in scope. game_admin_roles is
                   -- UNIQUE(user_id, game_id), so with a game argument this picks
                   -- the same single row the old `gar.game_id = 'digimon'` join did.
                   (SELECT g.role FROM game_admin_roles g
                     WHERE g.user_id = u.id
                       AND ($2::text IS NULL OR g.game_id = $2::text)
                     ORDER BY CASE g.role
                                WHEN 'super_admin' THEN 1
                                WHEN 'platform_admin' THEN 2
                                WHEN 'regional_admin' THEN 3
                                ELSE 4
                              END
                     LIMIT 1),
                   au.role
               ) AS role
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        WHERE au.discord_user_id = $1 AND au.is_active = TRUE
        """,
        discord_user_id,
        game_id,
    )
    if not user:
        return None
    if user["role"] in config.GLOBAL_ADMIN_ROLES:
        return []  # empty = global access (super/platform admins can access all scenes)

    return await _fetch(pool,
        """
        -- Direct scenes
        SELECT scene_id, game_id::text AS game_id
        FROM admin_user_scenes
        WHERE user_id = $1 AND ($2::text IS NULL OR game_id = $2::text)

        UNION

        -- Regional scenes
        SELECT s.scene_id, ar.game_id::text AS game_id
        FROM admin_regions ar
        JOIN scenes s ON ar.country = s.country
            AND (ar.state_region IS NULL OR ar.state_region = s.state_region)
        WHERE ar.user_id = $1 AND s.is_active = TRUE
          AND ($2::text IS NULL OR ar.game_id = $2::text)
        """,
        user["user_id"],
        game_id,
    )


async def get_admin_scenes_for_user(
    pool: asyncpg.Pool, discord_user_id: str, game_id: str | None
) -> list[int] | None:
    """``get_admin_scene_rows_for_user`` reduced to distinct scene ids.

    Same None / empty-list contract. Use this for permission checks; use the rows
    helper when the game each assignment belongs to matters (``/mystats``).
    """
    rows = await get_admin_scene_rows_for_user(pool, discord_user_id, game_id)
    if rows is None:
        return None
    seen: dict[int, None] = {}
    for r in rows:
        seen.setdefault(r["scene_id"], None)
    return list(seen)
