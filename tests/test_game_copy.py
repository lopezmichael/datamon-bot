"""Pure-function tests for the game-aware copy layer.

No DB, no Discord, no network. Covers the two places a game name reaches a human
and can be wrong without anyone noticing for a week: `messages.py`'s thread
templates, and `games.GameCache`'s labelling and tagline.

The "no template hardcodes a game name" rule is NOT here — it lives in
`tests/test_conventions.py`, which walks every module's string literals instead
of the two dicts this file used to name by hand. That version went green when a
third template dict was added, which is exactly the failure a ratchet must not
have.

What these actually pin down is the DEGRADED path. The happy path — two live
games, a request that carries one — is obvious in review and obvious in Discord.
The failure modes are not: a cold cache renders a heading with "None" in it, an
unknown id renders a raw database key, an empty games list renders a sentence
with an empty parenthetical. All three ship silently, because none of them raise
and all of them look like copy nobody got around to editing.

Run it with the venv's interpreter (asyncpg must import for `games` -> `db`):

    .venv/bin/python tests/test_game_copy.py

`config` is stubbed before the import because it fail-fasts on missing env vars
by design, and a formatting test must not need Discord credentials to run.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("config", types.ModuleType("config"))

import messages  # noqa: E402
from games import GameCache  # noqa: E402
from utils import classify_game_roles  # noqa: E402


def _cache(live: list[str], labels: dict[str, str] | None = None) -> GameCache:
    """A GameCache populated without touching the database.

    `labels` defaults to a name per live id, which is the real invariant: both
    queries read the same `games` table, so live is always a subset of labels.
    """
    cache = GameCache()
    cache.live = live
    cache.labels = labels if labels is not None else {g: g.title() for g in live}
    return cache


# ---------------------------------------------------------------------------
# messages.app_thread_message — threads that DO know their game
# ---------------------------------------------------------------------------

def test_app_thread_names_its_game() -> None:
    out = messages.app_thread_message("scene_requests", "scene_request", "Gundam")
    assert out.splitlines()[0] == "🌍 **New Scene Request (Gundam) — Triage Needed**"


def test_app_thread_without_a_game_reads_exactly_as_before() -> None:
    """No game must degrade to the pre-multi-game wording, not to "(None)"."""
    for empty in (None, ""):
        out = messages.app_thread_message("bug_reports", "bug_report", empty)
        assert out.splitlines()[0] == "🐛 **Bug Report — Triage Needed**"
        assert "None" not in out


def test_unknown_request_type_is_none_not_a_crash() -> None:
    assert messages.app_thread_message("bug_reports", "not_a_type", "Gundam") is None
    assert messages.app_thread_message("not_a_channel", "bug_report", "Gundam") is None


# ---------------------------------------------------------------------------
# messages.manual_thread_message — threads that CANNOT know their game
# ---------------------------------------------------------------------------

def test_manual_thread_asks_for_the_game_in_the_title() -> None:
    """No request row means no game, so the copy asks — via the title, not a tag.

    A per-game forum tag would spend one of Discord's five slots per channel,
    which the status tags already need. A title is free and searchable.
    """
    out = messages.manual_thread_message("scene_requests", ["Digimon", "Gundam"])
    assert "put the game in your thread title (Digimon or Gundam)" in out


def test_manual_thread_lists_three_games_readably() -> None:
    out = messages.manual_thread_message(
        "scene_requests", ["Digimon", "Gundam", "One Piece"]
    )
    assert "(Digimon, Gundam or One Piece)" in out
    assert "thread title" in out


def test_manual_thread_with_no_games_drops_the_parenthetical() -> None:
    """A cold cache must not produce "title ()" or a filler noun — just the ask."""
    for channel in ("scene_requests", "bug_reports", "feature_requests"):
        for empty in (None, [], [""]):
            out = messages.manual_thread_message(channel, empty)
            assert "thread title" in out
            assert "()" not in out


def test_unknown_manual_channel_is_none() -> None:
    assert messages.manual_thread_message("not_a_channel", ["Digimon"]) is None


# ---------------------------------------------------------------------------
# games.GameCache
# ---------------------------------------------------------------------------

# A cache shaped like production: two games covered, five games known.
_LABELS = {
    "digimon": "Digimon", "gundam": "Gundam", "onepiece": "One Piece",
    "fusionworld": "Fusion World", "unionarena": "Union Arena",
}


def test_label_falls_back_to_the_id_never_to_a_guess() -> None:
    """An unlabelled id is ugly and true. A defaulted one is tidy and wrong."""
    cache = _cache(["digimon"], {"digimon": "Digimon"})
    assert cache.label("gundam") == "gundam"
    assert cache.label("digimon") == "Digimon"


def test_label_covers_games_with_no_scene_coverage() -> None:
    """`labels` is a superset of `live` — a stale assignment still renders a name."""
    cache = _cache(["digimon"], _LABELS)
    assert cache.label("onepiece") == "One Piece"
    assert "onepiece" not in cache.live_ids()


def test_live_is_what_the_bot_reports_on() -> None:
    cache = _cache(["digimon", "gundam"], _LABELS)
    assert cache.live_ids() == {"digimon", "gundam"}
    assert cache.live_labels() == ["Digimon", "Gundam"]
    assert cache.live_choices() == [("digimon", "Digimon"), ("gundam", "Gundam")]


def test_known_is_wider_than_live_and_is_what_validates_an_argument() -> None:
    """A real game with no scenes yet must not be rejected as "Unknown game".

    That window is every new game's bootstrap: the first scene request for a game
    necessarily arrives before that game has a scene.
    """
    cache = _cache(["digimon", "gundam"], _LABELS)
    assert "onepiece" in cache.known_ids()
    assert "onepiece" not in cache.live_ids()
    assert "notagame" not in cache.known_ids()


def test_label_of_nothing_is_the_caller_s_default() -> None:
    cache = _cache(["digimon"], _LABELS)
    assert cache.label(None) == "All games"
    assert cache.label(None, default="") == ""


def test_tagline_names_every_live_game() -> None:
    two = _cache(["digimon", "gundam"], _LABELS)
    assert two.tagline() == "DigiLab — Tournament tracking for Digimon & Gundam"

    three = _cache(["digimon", "gundam", "onepiece"], _LABELS)
    assert three.tagline() == (
        "DigiLab — Tournament tracking for Digimon, Gundam & One Piece"
    )


def test_tagline_with_one_game_reads_like_the_old_hardcoded_line() -> None:
    assert _cache(["digimon"], _LABELS).tagline() == (
        "DigiLab — Digimon Tournament Tracker"
    )


def test_cold_tagline_names_no_game_rather_than_guessing_digimon() -> None:
    """The footer this replaced said "Digimon TCG Tournament Tracker" always."""
    out = GameCache().tagline()
    assert out == "DigiLab — TCG Tournament Tracker"
    assert "Digimon" not in out


# ---------------------------------------------------------------------------
# utils.classify_game_roles
# ---------------------------------------------------------------------------
#
# Game roles are staged AHEAD of the platform on purpose — roles exist in Discord
# for games DigiLab has not launched, and one (naruto) for a game with no `games`
# row at all. That is supported, and it is also the exact shape a typo'd env var
# takes: both parse, both are valid snowflakes, both grant nothing forever in
# silence. These pin that the two stay distinguishable.

# The real config as of 2026-08-24.
_CONFIGURED = ["digimon", "gundam", "onepiece", "fusionworld", "unionarena", "naruto"]
_LIVE = {"digimon", "gundam"}
_KNOWN = {"digimon", "gundam", "onepiece", "fusionworld", "unionarena"}


def test_production_config_classifies_as_expected() -> None:
    out = classify_game_roles(_CONFIGURED, _LIVE, _KNOWN)
    assert out["syncing"] == ["digimon", "gundam"]
    assert out["staged"] == ["fusionworld", "onepiece", "unionarena"]
    assert out["no_row"] == ["naruto"]
    assert out["unconfigured"] == []


def test_staged_and_missing_row_are_never_merged() -> None:
    """A game awaiting launch and a typo both grant nothing — and must not look alike."""
    out = classify_game_roles(["onepiece", "onepeice"], _LIVE, _KNOWN)
    assert out["staged"] == ["onepiece"]
    assert out["no_row"] == ["onepeice"]


def test_a_live_game_with_no_role_is_reported() -> None:
    """Its admins would silently never be granted anything."""
    out = classify_game_roles(["digimon"], _LIVE, _KNOWN)
    assert out["unconfigured"] == ["gundam"]
    assert out["syncing"] == ["digimon"]


def test_a_staged_game_becomes_syncing_when_it_gets_scenes() -> None:
    """No code or config change — coverage alone flips it."""
    before = classify_game_roles(_CONFIGURED, _LIVE, _KNOWN)
    assert "onepiece" in before["staged"]

    after = classify_game_roles(_CONFIGURED, _LIVE | {"onepiece"}, _KNOWN)
    assert "onepiece" in after["syncing"]
    assert "onepiece" not in after["staged"]


def test_no_roles_configured_is_all_live_games_unconfigured() -> None:
    out = classify_game_roles([], _LIVE, _KNOWN)
    assert out["unconfigured"] == ["digimon", "gundam"]
    assert out["syncing"] == out["staged"] == out["no_row"] == []


def test_every_configured_game_lands_in_exactly_one_bucket() -> None:
    out = classify_game_roles(_CONFIGURED, _LIVE, _KNOWN)
    placed = out["syncing"] + out["staged"] + out["no_row"]
    assert sorted(placed) == sorted(_CONFIGURED)
    assert len(placed) == len(set(placed))


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        else:
            print(f"ok   {name}")
    sys.exit(1 if failures else 0)
