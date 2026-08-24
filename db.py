"""Datamon Bot — Database connection pool and query helpers."""

import asyncio
import logging
from typing import NamedTuple

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


# Admin access levels (see AdminAccess / get_admin_access_for_user). Named
# constants rather than bare strings so a typo is an AttributeError, not a
# silently-denied admin.
ADMIN_ACCESS_NONE = "none"
ADMIN_ACCESS_GLOBAL = "global"
ADMIN_ACCESS_SCOPED = "scoped"


# `admin_requests.status` has no CHECK constraint. Production holds five values:
# pending, resolved and rejected, plus the pre-2026 terminal pair approved (30
# rows) and dismissed (3). Everything in these two sets is FINISHED — nobody owes
# it an action.
#
# They live here, not in a cog, because two surfaces have to agree on them and
# used not to: `cogs/archiver.py` heals threads by these sets while
# `get_request_summary` counted open as `status != 'resolved'`, which made every
# approved / rejected / dismissed row read as open. /requests reported **86 open
# when 12 were** — the command exists to say what needs attention and was off by
# 7x, in the safe-looking direction where the number is merely too big.
RESOLVED_STATUSES = frozenset({"resolved", "approved"})
REJECTED_STATUSES = frozenset({"rejected", "dismissed"})
TERMINAL_STATUSES = RESOLVED_STATUSES | REJECTED_STATUSES


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

async def get_active_admins(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """All active admins with their Discord user IDs and roles.

    Resolves role from user.is_super_admin → user.is_platform_admin →
    game_admin_roles → admin_users.role (legacy).

    The game_admin_roles read is the **strongest role across every game**, not one
    game's. Discord roles are a single flat namespace — there is no "Scene Admin
    (Gundam)" role to grant — so the sync has to answer "what is this person, at
    most?", per the PR 4 plan (A3.1). game_admin_roles is UNIQUE(user_id, game_id),
    so while every row is digimon this picks exactly the row the old
    `gar.game_id = 'digimon'` join picked.
    """
    return await _fetch(pool,
        """
        SELECT au.user_id, au.username, au.discord_user_id,
               COALESCE(
                   CASE WHEN u.is_super_admin = TRUE THEN 'super_admin' END,
                   CASE WHEN u.is_platform_admin = TRUE THEN 'platform_admin' END,
                   (SELECT g.role FROM game_admin_roles g
                     WHERE g.user_id = u.id
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
# "Is this admin global for this game?" — one predicate, three uses
# ---------------------------------------------------------------------------
#
# POLICY, locked 2026-08-13 with PR 4 (game-aware bot): **super admins answer for
# every game, always; a platform admin answers for a game only if they hold a
# `game_admin_roles` row for it.** Before this it was a flat "every active
# super/platform admin", which was harmless while every scene assignment was
# digimon — but tiers 1-2 are game-filtered now, so a Gundam report on a scene
# with no Gundam admin falls through to the fallback, and an unfiltered fallback
# would mass-ping the whole Digimon platform team for it.
#
# THIS IS A VERBATIM TRANSCRIPTION of digilab-web's Phase 5 predicate on branch
# `feat/admin-game-scoping` (`src/lib/admin-digest-queries.ts`, tier 3 of
# getSceneAdminCandidates + getGlobalAdminDiscordIds), which ships BEFORE this
# bot. Two details of theirs that are easy to "improve" and must not be:
#   * the legacy half is `admin_users.role` / `user.is_super_admin` /
#     `user.is_platform_admin` — a role that exists ONLY as a game_admin_roles
#     row does not make someone global on its own; and
#   * the per-game half only asks whether a `game_admin_roles` row EXISTS for the
#     game. **The row's `role` is deliberately not consulted.** Adding
#     `AND g.role IN (...)` would desync the two sides.
#
# It is also the authority for *who may resolve a request* (see
# `get_admin_access_for_user`), so mention rights and resolve rights cannot drift
# apart. The same shape is web's `hasPlatformAccess(ctx) && isGameAdmin(ctx, g)`.
def _global_admin_predicate(au: str, u: str, game: str) -> str:
    """SQL fragment for "is this admin global for `game`?".

    `au` / `u` are the query's aliases for admin_users / "user"; `game` is a
    placeholder like ``"$2"`` (NULL = "any game", used by the un-scoped callers).
    Every argument is written by this module — none is ever user input.
    """
    return f"""(
               -- Super admins answer for every game, always.
               {au}.role = 'super_admin'
               OR {u}.is_super_admin = TRUE
               -- Platform tier, holding a role row in THIS game. The row's role
               -- is not consulted: presence is the game gate, tier is the legacy
               -- flag. (Verbatim from web Phase 5 — do not "tighten" this.)
               OR (({au}.role = 'platform_admin' OR {u}.is_platform_admin = TRUE)
                   AND EXISTS (
                       SELECT 1 FROM game_admin_roles g
                       WHERE g.user_id = {u}.id
                         AND ({game}::text IS NULL OR g.game_id = {game}::text)
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


async def get_stores_for_scene(
    pool: asyncpg.Pool, scene_id: int, game_id: str | None = None
) -> list[asyncpg.Record]:
    """Stores in a scene with their tournament counts, optionally scoped to a game.

    ``stores`` carries no game column — a shop is a shop — so per-game membership
    lives in the ``store_games`` junction (98 active Gundam rows, 614 Digimon),
    exactly as it does on the web side. ``tournaments`` DOES carry ``game_id``.

    With a game: only stores active for it, counting only its tournaments. Without:
    every store, counting every game's tournaments — which is the honest answer to
    an unscoped question, and `/roster` now says so in the header rather than
    letting a blended number pass as one game's.
    """
    return await _fetch(pool,
        """
        SELECT s.store_id, s.name, s.city, s.state, s.is_active,
               COUNT(t.tournament_id) AS tournament_count
        FROM stores s
        LEFT JOIN tournaments t
          ON t.store_id = s.store_id
         AND ($2::text IS NULL OR t.game_id = $2::text)
        WHERE s.scene_id = $1
          AND ($2::text IS NULL OR EXISTS (
                SELECT 1 FROM store_games sg
                WHERE sg.store_id = s.store_id
                  AND sg.game_id = $2::text AND sg.is_active = TRUE
              ))
        GROUP BY s.store_id, s.name, s.city, s.state, s.is_active
        ORDER BY s.name
        """,
        scene_id,
        game_id,
    )


async def get_scene_stats(
    pool: asyncpg.Pool, scene_id: int, game_id: str
) -> asyncpg.Record | None:
    """Store / tournament / player counts for a scene, for ONE game.

    ``game_id`` is required, with no blended mode, because the blended total is
    the exact thing that made this function wrong: `/scene austin` reported "168
    tournaments, 281 players" — every one of them Gundam — under a footer reading
    "Digimon TCG Tournament Tracker". Refusing to compute that number is a
    stronger guarantee than documenting that it is dangerous.

    `/scene` with no game argument calls `get_scene_stats_by_game` instead, which
    answers per game. `get_stores_for_scene` DOES keep an optional game, because
    `/roster` genuinely wants both and says which it is showing.
    """
    return await _fetchrow(pool,
        """
        SELECT
            (SELECT COUNT(*) FROM stores s
             WHERE s.scene_id = $1 AND s.is_active = TRUE
               AND EXISTS (
                     SELECT 1 FROM store_games sg
                     WHERE sg.store_id = s.store_id
                       AND sg.game_id = $2 AND sg.is_active = TRUE
                   )) AS store_count,
            -- Tournaments are gated on t.game_id alone, NOT on the store_games
            -- junction, mirroring digilab-web queries.ts: hanging the history off
            -- the junction would make a scene whose only venue later closed lose
            -- its whole event record.
            (SELECT COUNT(*) FROM tournaments t
             JOIN stores s ON t.store_id = s.store_id
             WHERE s.scene_id = $1 AND t.game_id = $2) AS tournament_count,
            (SELECT COUNT(*) FROM players p
             WHERE p.home_scene_id = $1 AND p.is_active = TRUE
               AND p.game_id = $2) AS player_count
        """,
        scene_id,
        game_id,
    )


async def get_scene_stats_by_game(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    """One stats row per game the scene is active for, in one round trip.

    Driven by ``scene_games``, so a game the scene joined but has no activity for
    yet renders as a row of zeros rather than vanishing — "active here, nothing
    happening" and "not active here" are different facts and the reader has to be
    able to tell them apart. (Austin is active for both games and has 0 Digimon
    tournaments; that zero is the useful part.)
    """
    return await _fetch(pool,
        """
        SELECT g.game_id, g.short_name,
            (SELECT COUNT(*) FROM stores s
             JOIN store_games sg ON sg.store_id = s.store_id
             WHERE s.scene_id = $1 AND s.is_active = TRUE
               AND sg.game_id = g.game_id AND sg.is_active = TRUE) AS store_count,
            (SELECT COUNT(*) FROM tournaments t
             JOIN stores s ON t.store_id = s.store_id
             WHERE s.scene_id = $1 AND t.game_id = g.game_id) AS tournament_count,
            (SELECT COUNT(*) FROM players p
             WHERE p.home_scene_id = $1 AND p.is_active = TRUE
               AND p.game_id = g.game_id) AS player_count
        FROM scene_games x
        JOIN games g ON g.game_id = x.game_id
        WHERE x.scene_id = $1 AND x.is_active = TRUE
        ORDER BY g.game_id
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

    The bot's only write, and the reason `_run` demands idempotence: the guard
    makes a repeat a no-op, so a retry can never overwrite the original
    ``resolved_at`` / ``resolved_by``.

    That guard is ``NOT IN TERMINAL_STATUSES``, not ``!= 'resolved'``, and the
    difference is a real defect that stood until 2026-08-24. A request the web
    rejected is finished — it carries a ``resolved_at`` and a ``resolved_by``
    naming whoever rejected it — but its status is ``'rejected'``, so the old
    predicate matched, and a ✅ reaction on the still-open thread **flipped a
    rejection into a resolution and destroyed the original decision's
    attribution**. 15 rejected requests had live Discord threads when this was
    found. The bot's one sanctioned write must never reverse a call the web owns.

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
        WHERE discord_thread_id = $1 AND status <> ALL($3::text[])
        """,
        thread_id,
        resolved_by,
        sorted(TERMINAL_STATUSES),
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
    """Open counts, oldest open and average resolution time per game and request type.

    Two fixes over the pre-multi-game version, both of which made the number say
    something other than what `/requests` claims to answer:

    * **Open is "not terminal", not "not resolved".** The old filter was
      ``status != 'resolved'``, so all 41 rejected, 30 approved and 3 dismissed
      rows counted as awaiting action. The command reported 86 open against a real
      12. See TERMINAL_STATUSES.
    * **Grouped by game.** Gundam's requests used to fold into the same
      per-request-type line as Digimon's, so a Gundam-only backlog was
      indistinguishable from a Digimon one on a screen that admins triage from.

    Only rows with something open come back. An earlier version returned every
    game/type pair "so the averages are available", which was not true of any
    caller: `/requests` renders `avg_resolution_time` only on rows it has already
    kept for having open work, so a zero-open row's average was computed, sent
    and dropped.
    """
    return await _fetch(pool,
        """
        SELECT COALESCE(game_id, 'digimon') AS game_id,
               request_type,
               COUNT(*) FILTER (WHERE status <> ALL($1::text[])) AS open_count,
               MIN(submitted_at) FILTER (WHERE status <> ALL($1::text[])) AS oldest_open,
               COUNT(*) FILTER (WHERE status = ANY($2::text[])) AS resolved_count,
               AVG(resolved_at - submitted_at)
                 FILTER (WHERE status = ANY($2::text[])) AS avg_resolution_time
        FROM admin_requests
        GROUP BY 1, 2
        HAVING COUNT(*) FILTER (WHERE status <> ALL($1::text[])) > 0
        ORDER BY open_count DESC, 1, 2
        """,
        sorted(TERMINAL_STATUSES),
        sorted(RESOLVED_STATUSES),
    )


async def get_admin_game_ids(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Discord user ID → each game that user is an admin for. Powers game-role sync.

    Union of the three places a per-game admin relationship is recorded, because
    no single one of them is complete: `game_admin_roles` is the tier grant (169
    Digimon / 11 Gundam), `admin_user_scenes` the scene assignments (187 / 9), and
    `admin_regions` the regional coverage (8 / 2). Someone can hold a scene for a
    game without a role row, and vice versa; either makes them that game's admin
    for the purpose of a Discord game role.

    Super and platform admins are deliberately NOT fanned out to every game here.
    A game role says "this person works on this game", and the honest answer for a
    platform admin is the games they actually hold rows in — which is exactly what
    the tier-3 predicate above already uses to decide who gets paged.
    """
    return await _fetch(pool,
        """
        SELECT DISTINCT au.discord_user_id, x.game_id::text AS game_id
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        JOIN LATERAL (
            SELECT g.game_id FROM game_admin_roles g WHERE g.user_id = u.id
            UNION
            SELECT aus.game_id FROM admin_user_scenes aus WHERE aus.user_id = au.user_id
            UNION
            SELECT ar.game_id FROM admin_regions ar WHERE ar.user_id = au.user_id
        ) x ON TRUE
        WHERE au.is_active = TRUE AND au.discord_user_id IS NOT NULL
        """
    )


# ---------------------------------------------------------------------------
# Games: which ones are live, and what to call them
# ---------------------------------------------------------------------------
#
# **`games.is_active` DOES NOT DECIDE ANYTHING HERE, AND MUST NOT.** It is a
# second, divergent answer to "is this game live", and it is wrong right now:
# Gundam is `is_active = FALSE` while carrying 36 active `scene_games` rows, 11
# `game_admin_roles`, 9 scene assignments, 2 admin regions, 344 tournaments and
# 36 requests. digilab-web learned this the expensive way — its daily badge cron
# read the column, returned `["digimon"]`, and every claimed Gundam account went
# without a badge refresh from launch to 2026-08-21. The failure is silent by
# construction: the query is valid, the loop runs, the response says ok.
# `conventions.test.ts` now fails any web file that selects games by it, and
# `docs/references/multi-game-debt.md` is the ledger.
#
# The bot had the same bug in three places, with the same silence: the weekly
# digest skipped Gundam's scenes entirely, `/admins game:gundam` answered
# "Unknown game", and a Gundam block on `/admins` rendered under the raw id.
#
# Web's replacement is its `activeGameIds()` registry, a TypeScript module we
# cannot import. The bot's equivalent is the data itself: **a game is live here
# if it has active scenes.** That is the fact every surface below actually cares
# about, it cannot drift from a flag nobody remembers to flip, and game #3 needs
# no code change — it appears the moment it has a scene. Today it yields Digimon
# (186 scenes) and Gundam (16); One Piece, Fusion World and Union Arena are
# catalogue-only rows with zero scenes and correctly stay out.

async def get_live_games(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Games with active scene coverage, most-covered first.

    The one answer to "which games does this Discord actually coordinate?" — used
    by the weekly digest for its sections and by `/admins` / `/scene` / `/roster`
    for their game arguments, so a game can never be reported on but unpickable.

    Membership is the ``scene_games`` junction (``scenes`` is shared geography
    with no game column), so a game with no active scene rows contributes nothing
    rather than an empty section.

    Ordering is by coverage, not by a hardcoded game name: whichever game has the
    most scenes leads, which keeps Digimon first today without the bot knowing
    that Digimon is special.
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
        WHERE x.scene_count > 0
        ORDER BY x.scene_count DESC, g.game_id
        """
    )


async def get_game_labels(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every game row's display name — labelling only, never a liveness test.

    Deliberately unfiltered, and a different question from `get_live_games`. The
    admin cascade is not games-table-filtered at all, so it can hand back a row
    for a game with no scene coverage (a stale assignment). That row still has to
    render as "Gundam", not as `gundam`, or the reader sees a raw id and cannot
    tell a real game from a typo. Reading the column for DISPLAY is fine; reading
    it to decide what code DOES is what the comment above forbids.
    """
    return await _fetch(pool, "SELECT game_id, short_name FROM games ORDER BY game_id")


async def get_games_for_scene(pool: asyncpg.Pool, scene_id: int) -> list[asyncpg.Record]:
    """Games this scene is active for, via the ``scene_games`` junction."""
    return await _fetch(pool,
        """
        SELECT g.game_id, g.short_name
        FROM scene_games sg
        JOIN games g ON g.game_id = sg.game_id
        WHERE sg.scene_id = $1 AND sg.is_active = TRUE
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

    Membership is orphan-tolerant, like ``get_recently_deactivated_stores``: a scene
    with no ``scene_games`` rows at all is reported under every game rather than
    dropped. A junction-less scene is precisely the kind of misconfiguration a health
    digest exists to surface, and hiding it is the worse failure. (Zero such scenes
    today.)
    """
    return await _fetch(pool,
        """
        SELECT s.scene_id, s.display_name,
               MAX(t.event_date) AS last_tournament
        FROM scenes s
        LEFT JOIN stores st ON st.scene_id = s.scene_id
        LEFT JOIN tournaments t ON t.store_id = st.store_id AND t.game_id = $2::text
        WHERE s.scene_type IN ('metro', 'online') AND s.is_active = TRUE
          AND (
              EXISTS (
                  SELECT 1 FROM scene_games sg
                  WHERE sg.scene_id = s.scene_id AND sg.game_id = $2::text
                    AND sg.is_active = TRUE
              )
              OR NOT EXISTS (
                  SELECT 1 FROM scene_games sg2 WHERE sg2.scene_id = s.scene_id
              )
          )
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

    Membership is orphan-tolerant for the same reason as ``get_dormant_scenes``: a
    scene with no ``scene_games`` rows is unassigned *and* unregistered, which is
    more worth reporting, not less.
    """
    return await _fetch(pool,
        """
        SELECT s.scene_id, s.display_name
        FROM scenes s
        WHERE s.scene_type IN ('metro', 'online') AND s.is_active = TRUE
          AND (
              EXISTS (
                  SELECT 1 FROM scene_games sg
                  WHERE sg.scene_id = s.scene_id AND sg.game_id = $1::text
                    AND sg.is_active = TRUE
              )
              OR NOT EXISTS (
                  SELECT 1 FROM scene_games sg2 WHERE sg2.scene_id = s.scene_id
              )
          )
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
    """Resolution stats for an admin by their Discord user ID (``resolved_by``).

    Counts RESOLVED_STATUSES, not the bare ``'resolved'`` string this used to
    match. `/requests` and `/mystats` report the same two facts off the same two
    columns, and with two different predicates they answered differently by
    construction — an ``approved`` row counted toward the queue's average and not
    toward the person who closed it. Rejections and dismissals are deliberately
    excluded: they are terminal, but they are not resolutions.

    No live Discord account is affected today (every ``approved`` row's
    ``resolved_by`` is a service account, not a snowflake), so this is the
    definition being made consistent before it can matter, not a repair.
    """
    return await _fetchrow(pool,
        """
        SELECT COUNT(*) AS resolved_count,
               AVG(resolved_at - submitted_at) AS avg_resolution_time,
               MIN(resolved_at) AS first_resolved,
               MAX(resolved_at) AS last_resolved
        FROM admin_requests
        WHERE resolved_by = $1 AND status = ANY($2::text[])
        """,
        discord_user_id,
        sorted(RESOLVED_STATUSES),
    )


async def get_scene_count(pool: asyncpg.Pool) -> int:
    row = await _fetchrow(pool,
        "SELECT COUNT(*) AS cnt FROM scenes WHERE scene_type IN ('metro', 'online') AND is_active = TRUE"
    )
    return row["cnt"]


class AdminAccess(NamedTuple):
    """What a Discord user may do about requests in one game.

    ``level`` is the whole answer and callers MUST branch on it:

    - ``'none'``   — not an active admin (or, for a game-scoped ask, not one here).
    - ``'global'`` — answers for every scene in this game. Super admins always;
      platform-tier admins for the games they hold a ``game_admin_roles`` row for,
      per ``_global_admin_predicate``.
    - ``'scoped'`` — answers only for ``rows``, the scene assignments they hold in
      this game (direct + regional).

    **Never infer access from ``len(rows)``.** That was a real bug: the old helper
    returned a bare empty list for BOTH "global admin" and "admin with no
    assignments in this game", so a Digimon-only scene admin reacting on a Gundam
    request read as global and could resolve every Gundam request in the server.
    An empty ``rows`` under ``'scoped'`` means the opposite of global: nothing.
    """

    level: str
    rows: tuple[asyncpg.Record, ...] = ()

    @property
    def scene_ids(self) -> set[int]:
        return {r["scene_id"] for r in self.rows}

    def covers(self, scene_id: int | None) -> bool:
        """May this user act on something attached to ``scene_id``?

        ``scene_id=None`` is a scene-less request (bug report, new-scene request).
        Those have no scene to match, so the test becomes "do they administer
        anything at all in this game" — which is what keeps a Digimon scene admin
        out of a Gundam bug report while leaving today's Digimon behavior intact.
        """
        if self.level == ADMIN_ACCESS_GLOBAL:
            return True
        if self.level != ADMIN_ACCESS_SCOPED:
            return False
        if scene_id is None:
            return bool(self.rows)
        return scene_id in self.scene_ids


async def get_admin_access_for_user(
    pool: asyncpg.Pool, discord_user_id: str, game_id: str | None
) -> AdminAccess:
    """Resolve a Discord user's admin access, for one game or across all of them.

    ``game_id`` is required, not defaulted, so every caller states what it is asking:

    - a game id scopes everything — whether they are global here
      (``_global_admin_predicate``, the same test that decides who gets @mentioned)
      and which assignments count (``admin_user_scenes.game_id`` /
      ``admin_regions.game_id``). This is the authorization question, so pass the
      request's own game.
    - None asks "any game", for display and for game-neutral surfaces like
      ``/roster`` (stores and tournaments belong to the scene, which is shared
      geography). It never narrows anyone's access relative to today.

    Two round trips at most, and only one for a global admin.
    """
    user = await _fetchrow(pool,
        f"""
        SELECT au.user_id,
               -- COALESCE because the predicate evaluates to NULL, not FALSE, for
               -- an admin with no linked "user" row (NULL flags). SQL's WHERE drops
               -- those rows, so NULL already means "not global" everywhere else;
               -- spell it out here rather than lean on Python's falsy None.
               COALESCE({_global_admin_predicate("au", "u", "$2")}, FALSE) AS is_global
        FROM admin_users au
        LEFT JOIN "user" u ON u.legacy_admin_id = au.user_id
        WHERE au.discord_user_id = $1 AND au.is_active = TRUE
        """,
        discord_user_id,
        game_id,
    )
    if not user:
        return AdminAccess(ADMIN_ACCESS_NONE)
    if user["is_global"]:
        return AdminAccess(ADMIN_ACCESS_GLOBAL)

    rows = await _fetch(pool,
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
    return AdminAccess(ADMIN_ACCESS_SCOPED, tuple(rows))
