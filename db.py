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


async def get_admins_for_scene(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    """Get admins responsible for a scene, as a geographic cascade.

    Returns rows tagged with an ``assignment_type`` and a numeric ``tier`` so callers can
    apply scene -> region -> global precedence (see ``select_tier_admins``):

    - tier 1 ("scene"): ``direct`` admins of the scene, plus ``child_metro`` admins for a
      ``state`` rollup scene (admins of metros within that state). This is what fixes the
      common "report lands on a state/country rollup, which has no direct admin" case where
      the cascade would otherwise fall straight through to global. Country rollups do NOT
      fan out to child metros (would mass-ping every admin in the country).
    - tier 2 ("regional"): regional admins matching the scene's country / state.
    - tier 3 ("global"): super + platform admins.
    """
    return await _fetch(pool,
        """
        -- Direct scene admins (tier 1)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u0.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u0.is_platform_admin = TRUE THEN 'platform_admin' END,
                   gar0.role, au.role
               ) AS role,
               aus.is_primary, 'direct' AS assignment_type, 1 AS tier
        FROM admin_user_scenes aus
        JOIN admin_users au ON aus.user_id = au.user_id
        LEFT JOIN "user" u0 ON u0.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles gar0 ON gar0.user_id = u0.id AND gar0.game_id = 'digimon'
        WHERE aus.scene_id = $1 AND au.is_active = TRUE

        UNION

        -- Child-metro admins for a state rollup scene (tier 1)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN uc.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN uc.is_platform_admin = TRUE THEN 'platform_admin' END,
                   garc.role, au.role
               ) AS role,
               FALSE AS is_primary, 'child_metro' AS assignment_type, 1 AS tier
        FROM scenes parent
        JOIN scenes child
          ON child.scene_type = 'metro'
         AND child.is_active = TRUE
         AND child.country = parent.country
         AND child.state_region = parent.state_region
        JOIN admin_user_scenes aus ON aus.scene_id = child.scene_id
        JOIN admin_users au ON aus.user_id = au.user_id
        LEFT JOIN "user" uc ON uc.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles garc ON garc.user_id = uc.id AND garc.game_id = 'digimon'
        WHERE parent.scene_id = $1 AND parent.scene_type = 'state' AND au.is_active = TRUE

        UNION

        -- Regional admins, country match + optional state match (tier 2)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u1.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u1.is_platform_admin = TRUE THEN 'platform_admin' END,
                   gar1.role, au.role
               ) AS role,
               FALSE AS is_primary, 'regional' AS assignment_type, 2 AS tier
        FROM admin_regions ar
        JOIN admin_users au ON ar.user_id = au.user_id
        LEFT JOIN "user" u1 ON u1.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles gar1 ON gar1.user_id = u1.id AND gar1.game_id = 'digimon'
        JOIN scenes s ON s.scene_id = $1
        WHERE au.is_active = TRUE
          AND ar.country = s.country
          AND (ar.state_region IS NULL OR ar.state_region = s.state_region)

        UNION

        -- Global admins: super + platform, legacy or new flags (tier 3)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u2.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u2.is_platform_admin = TRUE THEN 'platform_admin' END,
                   au.role
               ) AS role,
               FALSE AS is_primary, 'global' AS assignment_type, 3 AS tier
        FROM admin_users au
        LEFT JOIN "user" u2 ON u2.legacy_admin_id = au.user_id
        WHERE au.is_active = TRUE
          AND (au.role IN ('super_admin', 'platform_admin')
               OR u2.is_super_admin = TRUE
               OR u2.is_platform_admin = TRUE)

        ORDER BY tier, role, username
        """,
        scene_id,
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
    return await _fetchrow(pool,
        """
        SELECT id, request_type, scene_id, status, discord_username, discord_thread_id
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


async def get_global_admin_discord_ids(pool: asyncpg.Pool) -> list[str]:
    """Get Discord user IDs for all active global admins (super + platform).

    Checks both legacy admin_users.role and user boolean flags.
    """
    rows = await _fetch(pool,
        """
        SELECT DISTINCT au.discord_user_id
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        WHERE au.is_active = TRUE
          AND au.discord_user_id IS NOT NULL
          AND (au.role IN ('super_admin', 'platform_admin')
               OR u.is_super_admin = TRUE
               OR u.is_platform_admin = TRUE)
        """
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


async def get_dormant_scenes(pool: asyncpg.Pool, days: int = 60) -> list[asyncpg.Record]:
    """Scenes with no tournaments in the last N days."""
    return await _fetch(pool,
        """
        SELECT s.scene_id, s.display_name,
               MAX(t.event_date) AS last_tournament
        FROM scenes s
        LEFT JOIN stores st ON st.scene_id = s.scene_id
        LEFT JOIN tournaments t ON t.store_id = st.store_id
        WHERE s.scene_type IN ('metro', 'online') AND s.is_active = TRUE
        GROUP BY s.scene_id, s.display_name
        HAVING MAX(t.event_date) IS NULL OR MAX(t.event_date) < CURRENT_DATE - $1 * INTERVAL '1 day'
        ORDER BY MAX(t.event_date) NULLS FIRST
        """,
        days,
    )


async def get_unassigned_scenes(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Active scenes with no direct admin assignment."""
    return await _fetch(pool,
        """
        SELECT s.scene_id, s.display_name
        FROM scenes s
        WHERE s.scene_type IN ('metro', 'online') AND s.is_active = TRUE
          AND NOT EXISTS (
              SELECT 1 FROM admin_user_scenes aus
              JOIN admin_users au ON aus.user_id = au.user_id
              WHERE aus.scene_id = s.scene_id AND au.is_active = TRUE
          )
        ORDER BY s.display_name
        """
    )


async def get_recently_deactivated_stores(pool: asyncpg.Pool, days: int = 7) -> list[asyncpg.Record]:
    """Stores that were deactivated in the last N days."""
    return await _fetch(pool,
        """
        SELECT st.store_id, st.name, st.city, st.state,
               s.scene_id, s.display_name AS scene_name
        FROM stores st
        JOIN scenes s ON st.scene_id = s.scene_id
        WHERE st.is_active = FALSE
          AND st.updated_at >= CURRENT_DATE - $1 * INTERVAL '1 day'
        ORDER BY s.display_name, st.name
        """,
        days,
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


async def get_admin_scenes_for_user(pool: asyncpg.Pool, discord_user_id: str) -> list[int] | None:
    """Get all scene_ids a user is admin for (direct + regional).

    Returns None if user not found in DB. Returns empty list for global admins (super/platform).
    Callers should treat None as "no admin access" unless they've already verified
    the user's Discord role. An empty list means "admin but no scene assignments".
    """
    user = await _fetchrow(pool,
        """
        SELECT au.user_id,
               COALESCE(
                   CASE WHEN u.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u.is_platform_admin = TRUE THEN 'platform_admin' END,
                   gar.role,
                   au.role
               ) AS role
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        LEFT JOIN game_admin_roles gar ON gar.user_id = u.id AND gar.game_id = 'digimon'
        WHERE au.discord_user_id = $1 AND au.is_active = TRUE
        """,
        discord_user_id,
    )
    if not user:
        return None
    if user["role"] in config.GLOBAL_ADMIN_ROLES:
        return []  # empty = global access (super/platform admins can access all scenes)

    rows = await _fetch(pool,
        """
        -- Direct scenes
        SELECT scene_id FROM admin_user_scenes WHERE user_id = $1

        UNION

        -- Regional scenes
        SELECT s.scene_id
        FROM admin_regions ar
        JOIN scenes s ON ar.country = s.country
            AND (ar.state_region IS NULL OR ar.state_region = s.state_region)
        WHERE ar.user_id = $1 AND s.is_active = TRUE
        """,
        user["user_id"],
    )
    return [r["scene_id"] for r in rows]
