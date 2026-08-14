"""Pure-function tests for the weekly digest's per-game rendering.

No DB, no Discord, no network. The bot has no test suite and this is not the start
of building one: `format_game_section` is the one piece of PR 4 that is pure, and
the digest is the surface whose output nobody sees until a Monday, so it is worth
pinning that a single-game render still reads like the pre-PR-4 digest.

Run it with the venv's interpreter (discord.py + asyncpg must import):

    .venv/bin/python tests/test_digest_format.py

`config` is stubbed before the import because it fail-fasts on missing env vars by
design, and a formatting test must not need Discord credentials to run.
"""

import datetime
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("config", types.ModuleType("config"))

from cogs.digest import format_game_section  # noqa: E402


def test_healthy_game_renders_nothing() -> None:
    assert format_game_section("Digimon", [], [], [], {}) is None
    # Mentions alone are not content: they exist to point at the lists above them.
    assert format_game_section("Digimon", [], [], [], {"1": ["Austin"]}) is None


def test_single_game_reads_like_the_old_digest() -> None:
    dormant = [
        {"scene_id": 1, "display_name": "Austin", "last_tournament": datetime.date(2026, 5, 4)},
        {"scene_id": 2, "display_name": "Perth", "last_tournament": None},
    ]
    unassigned = [{"scene_id": 3, "display_name": "Lisbon"}]
    deactivated = [{"scene_id": 1, "name": "Dragon's Lair", "scene_name": "Austin"}]

    out = format_game_section(
        "Digimon", dormant, unassigned, deactivated, {"42": ["Austin", "Perth"]}
    )

    assert out is not None
    lines = out.split("\n")
    # The game header is the only structural addition over the pre-PR-4 digest.
    assert lines[0] == "__**Digimon**__"
    assert "**Scenes with no tournaments in 60+ days:**" in out
    assert "• **Austin** — last tournament May 04" in out
    assert "• **Perth** — no tournaments on record" in out
    assert "**Scenes with no assigned admin:**\n• **Lisbon**" in out
    assert "**Stores deactivated this week:**\n• **Dragon's Lair** (Austin)" in out
    assert out.endswith("<@42> — Austin, Perth")


def test_only_the_sections_with_content_appear() -> None:
    out = format_game_section("Gundam", [], [{"scene_id": 9, "display_name": "Osaka"}], [], {})
    assert out == "__**Gundam**__\n\n**Scenes with no assigned admin:**\n• **Osaka**"


def test_long_scene_lists_are_capped_with_a_counter() -> None:
    scenes = [f"Scene {i}" for i in range(7)]
    out = format_game_section(
        "Digimon", [], [{"scene_id": 1, "display_name": "Scene 0"}], [], {"7": scenes}
    )
    assert out is not None
    assert "<@7> — Scene 0, Scene 1, Scene 2, Scene 3, Scene 4 +2 more" in out
    assert "Scene 5" not in out


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
