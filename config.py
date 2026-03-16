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
CHANNEL_SCENE_COORDINATION: int = _require_int("DISCORD_CHANNEL_SCENE_COORDINATION")
CHANNEL_SCENE_REQUESTS: int = _require_int("DISCORD_CHANNEL_SCENE_REQUESTS")
CHANNEL_BUG_REPORTS: int = _require_int("DISCORD_CHANNEL_BUG_REPORTS")
CHANNEL_FEATURE_REQUESTS: int = _require_int("DISCORD_CHANNEL_FEATURE_REQUESTS")

# Forum tag IDs
TAG_RESOLVED: int = _require_int("DISCORD_TAG_RESOLVED")
TAG_ONBOARDED: int = _require_int("DISCORD_TAG_ONBOARDED")
TAG_FIXED: int = _require_int("DISCORD_TAG_FIXED")
TAG_SHIPPED: int = _require_int("DISCORD_TAG_SHIPPED")

# Forum channel → resolve config mapping
FORUM_CHANNELS: dict[int, dict] = {
    CHANNEL_SCENE_COORDINATION: {"resolve_tag": TAG_RESOLVED, "label": "Resolved"},
    CHANNEL_SCENE_REQUESTS: {"resolve_tag": TAG_ONBOARDED, "label": "Onboarded"},
    CHANNEL_BUG_REPORTS: {"resolve_tag": TAG_FIXED, "label": "Fixed"},
    CHANNEL_FEATURE_REQUESTS: {"resolve_tag": TAG_SHIPPED, "label": "Shipped"},
}

# Webhook for #bot-log
WEBHOOK_BOT_LOG: str = _require("DISCORD_WEBHOOK_BOT_LOG")

# Neon PostgreSQL
NEON_HOST: str = _require("NEON_HOST")
NEON_DATABASE: str = _require("NEON_DATABASE")
NEON_USER: str = _require("NEON_USER")
NEON_PASSWORD: str = _require("NEON_PASSWORD")

# Role mapping: DB role name → Discord role ID
ROLE_MAP: dict[str, int] = {
    "super_admin": ROLE_PLATFORM_ADMIN,
    "regional_admin": ROLE_REGIONAL_ADMIN,
    "scene_admin": ROLE_SCENE_ADMIN,
}

# Set of all DigiLab role IDs for quick membership checks
DIGILAB_ROLE_IDS: set[int] = {ROLE_PLATFORM_ADMIN, ROLE_REGIONAL_ADMIN, ROLE_SCENE_ADMIN}

# App base URL
APP_BASE_URL = "https://app.digilab.cards"

# Logging
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
