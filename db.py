"""Datamon Bot — Database connection pool and query helpers."""

import asyncio
import logging

import asyncpg

import config

log = logging.getLogger(__name__)


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
            )
            log.info("Database pool created (attempt %d)", attempt + 1)
            return pool
        except Exception:
            if attempt == 2:
                raise
            wait = 2**attempt  # 1s, 2s
            log.warning("DB pool creation failed (attempt %d), retrying in %ds", attempt + 1, wait)
            await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def get_active_admins(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """All active admins with their Discord user IDs and roles."""
    return await pool.fetch(
        """
        SELECT user_id, username, discord_user_id, role
        FROM admin_users
        WHERE is_active = TRUE
        """
    )


async def get_scenes(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """All active metro/online scenes for autocomplete cache."""
    return await pool.fetch(
        """
        SELECT scene_id, slug, display_name, country, state_region, continent
        FROM scenes
        WHERE scene_type IN ('metro', 'online') AND is_active = TRUE
        ORDER BY display_name
        """
    )


async def get_scene_by_slug(pool: asyncpg.Pool, slug: str) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        SELECT scene_id, slug, display_name, country, state_region, continent,
               latitude, longitude
        FROM scenes
        WHERE slug = $1 AND is_active = TRUE
        """,
        slug,
    )


async def get_admins_for_scene(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    """Get all admins for a scene (direct via admin_user_scenes + regional via admin_regions).

    Also includes super_admins (they have access to all scenes).
    """
    return await pool.fetch(
        """
        -- Direct scene admins
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id, au.role,
               aus.is_primary, 'direct' AS assignment_type
        FROM admin_user_scenes aus
        JOIN admin_users au ON aus.user_id = au.user_id
        WHERE aus.scene_id = $1 AND au.is_active = TRUE

        UNION

        -- Regional admins (country match, optional state match)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id, au.role,
               FALSE AS is_primary, 'regional' AS assignment_type
        FROM admin_regions ar
        JOIN admin_users au ON ar.user_id = au.user_id
        JOIN scenes s ON s.scene_id = $1
        WHERE au.is_active = TRUE
          AND ar.country = s.country
          AND (ar.state_region IS NULL OR ar.state_region = s.state_region)

        UNION

        -- Super admins (global access)
        SELECT DISTINCT au.user_id, au.username, au.discord_user_id, au.role,
               FALSE AS is_primary, 'global' AS assignment_type
        FROM admin_users au
        WHERE au.role = 'super_admin' AND au.is_active = TRUE

        ORDER BY role, username
        """,
        scene_id,
    )


async def get_stores_for_scene(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    return await pool.fetch(
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
    return await pool.fetchrow(
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
    return await pool.fetchrow(
        """
        SELECT id, request_type, scene_id, status, discord_username, discord_thread_id
        FROM admin_requests
        WHERE discord_thread_id = $1
        """,
        thread_id,
    )


async def resolve_request(pool: asyncpg.Pool, thread_id: str, resolved_by: str) -> bool:
    """Mark a request as resolved. Returns True if a row was updated."""
    result = await pool.execute(
        """
        UPDATE admin_requests
        SET status = 'resolved', resolved_at = NOW(), resolved_by = $2
        WHERE discord_thread_id = $1 AND status != 'resolved'
        """,
        thread_id,
        resolved_by,
    )
    return result == "UPDATE 1"


async def get_super_admin_discord_ids(pool: asyncpg.Pool) -> list[str]:
    """Get Discord user IDs for all active super_admins."""
    rows = await pool.fetch(
        """
        SELECT discord_user_id
        FROM admin_users
        WHERE role = 'super_admin' AND is_active = TRUE AND discord_user_id IS NOT NULL
        """
    )
    return [r["discord_user_id"] for r in rows if r["discord_user_id"]]


async def get_scene_count(pool: asyncpg.Pool) -> int:
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS cnt FROM scenes WHERE scene_type IN ('metro', 'online') AND is_active = TRUE"
    )
    return row["cnt"]


async def get_admin_scenes_for_user(pool: asyncpg.Pool, discord_user_id: str) -> list[int] | None:
    """Get all scene_ids a user is admin for (direct + regional).

    Returns None for super_admins (global access) or if user not found in DB.
    Callers should treat None as "no admin access" unless they've already verified
    the user's Discord role. An empty list means "admin but no scene assignments".
    """
    user = await pool.fetchrow(
        "SELECT user_id, role FROM admin_users WHERE discord_user_id = $1 AND is_active = TRUE",
        discord_user_id,
    )
    if not user:
        return None
    if user["role"] == "super_admin":
        return []  # empty = global access (super_admins can access all scenes)

    rows = await pool.fetch(
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
