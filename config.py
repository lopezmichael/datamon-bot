"""Datamon Bot — Configuration and environment variable loading."""

import logging
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _require_int(name: str) -> int:
    return int(_require(name))


# Discord Bot
BOT_TOKEN: str = _require("DISCORD_BOT_TOKEN")
GUILD_ID: int = _require_int("DISCORD_GUILD_ID")

# Role IDs
ROLE_PLATFORM_ADMIN: int = _require_int("DISCORD_ROLE_PLATFORM_ADMIN")
ROLE_REGIONAL_ADMIN: int = _require_int("DISCORD_ROLE_REGIONAL_ADMIN")
ROLE_SCENE_ADMIN: int = _require_int("DISCORD_ROLE_SCENE_ADMIN")

# Channel IDs
CHANNEL_SCENE_REQUESTS: int = _require_int("DISCORD_CHANNEL_SCENE_REQUESTS")
CHANNEL_BUG_REPORTS: int = _require_int("DISCORD_CHANNEL_BUG_REPORTS")
CHANNEL_FEATURE_REQUESTS: int = _require_int("DISCORD_CHANNEL_FEATURE_REQUESTS")

# Forum tag IDs
TAG_ONBOARDED: int = _require_int("DISCORD_TAG_ONBOARDED")
TAG_FIXED: int = _require_int("DISCORD_TAG_FIXED")
TAG_SHIPPED: int = _require_int("DISCORD_TAG_SHIPPED")
TAG_NEW_BUG_REPORTS: int = _require_int("DISCORD_TAG_NEW_BUG_REPORTS")
TAG_UNDER_REVIEW_BUG_REPORTS: int = _require_int("DISCORD_TAG_UNDER_REVIEW_BUG_REPORTS")
TAG_CONFIRMED_BUG_REPORTS: int = _require_int("DISCORD_TAG_CONFIRMED_BUG_REPORTS")
TAG_WONT_FIX: int = _require_int("DISCORD_TAG_WONT_FIX")
TAG_NEW_FEATURE_REQUESTS: int = _require_int("DISCORD_TAG_NEW_FEATURE_REQUESTS")
TAG_PLANNED_FEATURE_REQUESTS: int = _require_int("DISCORD_TAG_PLANNED_FEATURE_REQUESTS")
TAG_NOT_PLANNED: int = _require_int("DISCORD_TAG_NOT_PLANNED")
TAG_NEW_SCENE_REQUESTS: int = _require_int("DISCORD_TAG_NEW_SCENE_REQUESTS")
TAG_NEEDS_MORE_INFO_SCENE_REQUESTS: int = _require_int("DISCORD_TAG_NEEDS_MORE_INFO_SCENE_REQUESTS")
TAG_NEEDS_ADMIN_SCENE_REQUESTS: int = _require_int("DISCORD_TAG_NEEDS_ADMIN_SCENE_REQUESTS")
TAG_ON_HOLD: int = _require_int("DISCORD_TAG_ON_HOLD")

# Forum channel → resolve config mapping.
#
# `reject_tag` is the channel's distinct "won't do" completion tag, mirroring
# `CHANNEL_ENV` in digilab-web `src/lib/discord.ts`: a rejection applies
# `reject_tag ?? resolve_tag`. Only #bug-reports has one there (Won't Fix);
# #feature-requests is absent from the web map entirely (the app does not thread
# to it yet), so nothing to mirror. No new env vars — every ID already exists.
FORUM_CHANNELS: dict[int, dict] = {
    CHANNEL_SCENE_REQUESTS: {
        "resolve_tag": TAG_ONBOARDED,
        "new_tag": TAG_NEW_SCENE_REQUESTS,
        "initial_tags": [TAG_NEW_SCENE_REQUESTS, TAG_NEEDS_MORE_INFO_SCENE_REQUESTS, TAG_NEEDS_ADMIN_SCENE_REQUESTS],
        "label": "Onboarded",
        "channel_type": "scene_requests",
    },
    CHANNEL_BUG_REPORTS: {
        "resolve_tag": TAG_FIXED,
        "reject_tag": TAG_WONT_FIX,
        "new_tag": TAG_NEW_BUG_REPORTS,
        "initial_tags": [TAG_NEW_BUG_REPORTS, TAG_UNDER_REVIEW_BUG_REPORTS, TAG_CONFIRMED_BUG_REPORTS],
        "label": "Fixed",
        "channel_type": "bug_reports",
    },
    CHANNEL_FEATURE_REQUESTS: {
        "resolve_tag": TAG_SHIPPED,
        "new_tag": TAG_NEW_FEATURE_REQUESTS,
        "initial_tags": [TAG_NEW_FEATURE_REQUESTS, TAG_PLANNED_FEATURE_REQUESTS],
        "label": "Shipped",
        "channel_type": "feature_requests",
    },
}


def _env_names(prefix: str) -> dict[int, str]:
    """Reverse-map the IDs defined above back to the env vars they came from.

    Only used by the boot-time forum check in `utils.check_forum_config`, so a
    bad ID reads as "DISCORD_TAG_FIXED is wrong" instead of a bare snowflake.
    Derived rather than hand-written so a new ID can never be added without its
    name: every constant here is `DISCORD_` + its own name.
    """
    return {
        value: f"DISCORD_{name}"
        for name, value in globals().items()
        if name.startswith(prefix) and isinstance(value, int)
    }


CHANNEL_ENV_NAMES: dict[int, str] = _env_names("CHANNEL_")
TAG_ENV_NAMES: dict[int, str] = _env_names("TAG_")

# Webhook for #bot-log
WEBHOOK_BOT_LOG: str = _require("DISCORD_WEBHOOK_BOT_LOG")

# Webhook for #admin-digest — the weekly scene-health digest posts here. The web
# app's daily request digest posts to the same channel via its own env var.
WEBHOOK_ADMIN_DIGEST: str = _require("DISCORD_WEBHOOK_ADMIN_DIGEST")

# Neon PostgreSQL
NEON_HOST: str = _require("NEON_HOST")
NEON_DATABASE: str = _require("NEON_DATABASE")
NEON_USER: str = _require("NEON_USER")
NEON_PASSWORD: str = _require("NEON_PASSWORD")

# Role mapping: DB role name → Discord role ID
ROLE_MAP: dict[str, int] = {
    "super_admin": ROLE_PLATFORM_ADMIN,
    "platform_admin": ROLE_PLATFORM_ADMIN,
    "regional_admin": ROLE_REGIONAL_ADMIN,
    "scene_admin": ROLE_SCENE_ADMIN,
}

# Roles with global scene access (treated equivalently in datamon)
GLOBAL_ADMIN_ROLES: set[str] = {"super_admin", "platform_admin"}

# Set of all DigiLab role IDs for quick membership checks.
#
# The three TIER roles only. Deliberately does not include the per-game roles
# below, and must not: role_sync's reverse pass strips every role in this set
# from anyone who is not an active admin, and a game role is also handed out by
# Discord onboarding to members who self-select a game. Adding them here would
# make the bot quietly revoke people's own onboarding picks every five minutes.
DIGILAB_ROLE_IDS: set[int] = {ROLE_PLATFORM_ADMIN, ROLE_REGIONAL_ADMIN, ROLE_SCENE_ADMIN}


def _game_roles() -> dict[str, int]:
    """Per-game Discord roles, keyed by `game_id`, from `DISCORD_GAME_ROLE_<GAME>`.

    So `DISCORD_GAME_ROLE_DIGIMON=123` maps game_id 'digimon' to role 123. Env
    discovery rather than three named `_require_int` constants for the reason the
    rest of PR 4 exists: a fourth game must not be a code change. Add the var,
    restart, done.

    **Optional, unlike every other ID in this file.** The bot fails fast on a
    missing env var everywhere else because those IDs are load-bearing — without
    them a loop silently no-ops. This one degrades honestly: no var for a game
    means that game's role simply is not synced, which is the correct behaviour
    while the roles are still being rolled out, and is visible in #bot-log's
    startup line rather than inferred.
    """
    prefix = "DISCORD_GAME_ROLE_"
    roles: dict[str, int] = {}
    for name, value in os.environ.items():
        if not name.startswith(prefix) or not value.strip():
            continue
        game_id = name[len(prefix):].lower()
        try:
            roles[game_id] = int(value)
        except ValueError:
            raise RuntimeError(f"{name} must be a Discord role ID, got {value!r}")
    return roles


# game_id → Discord role ID. Granted additively by role_sync, never removed.
GAME_ROLES: dict[str, int] = _game_roles()

# App base URL
APP_BASE_URL = "https://digilab.cards"

# Logging
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
