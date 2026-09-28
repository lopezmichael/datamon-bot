"""Pure-function tests for the weekly digest: which scenes get named, and how.

No DB, no Discord, no network. The digest is the surface whose output nobody sees
until a Monday, and its whole design rests on one arithmetic claim — a scene lands
in exactly one weekly window per threshold — so that claim is pinned here directly,
by walking a scene through consecutive Mondays.

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

from cogs.digest import (  # noqa: E402
    WINDOW_DAYS,
    classify_scene_health,
    format_game_section,
)

TODAY = datetime.date(2026, 9, 28)


def _scene(scene_id=1, name="Austin", joined_days_ago=400, quiet_days=None, has_admin=True):
    """A scene row. `quiet_days=None` means it has never had a tournament."""
    return {
        "scene_id": scene_id,
        "display_name": name,
        "joined_at": TODAY - datetime.timedelta(days=joined_days_ago),
        "last_tournament": (
            None if quiet_days is None else TODAY - datetime.timedelta(days=quiet_days)
        ),
        "has_admin": has_admin,
    }


def _reasons(scene) -> list[str]:
    items, _ = classify_scene_health([scene], TODAY)
    return items[0]["reasons"] if items else []


# --- classification: the week a threshold is crossed ------------------------------


def test_never_played_is_named_at_30_and_90_days_only() -> None:
    assert _reasons(_scene(joined_days_ago=29, quiet_days=None)) == []
    assert _reasons(_scene(joined_days_ago=30, quiet_days=None)) == ["30 days in, no tournaments yet"]
    assert _reasons(_scene(joined_days_ago=36, quiet_days=None)) == ["30 days in, no tournaments yet"]
    assert _reasons(_scene(joined_days_ago=37, quiet_days=None)) == []
    assert _reasons(_scene(joined_days_ago=60, quiet_days=None)) == []
    assert _reasons(_scene(joined_days_ago=90, quiet_days=None)) == ["90 days in, no tournaments yet"]
    assert _reasons(_scene(joined_days_ago=200, quiet_days=None)) == []


def test_quiet_is_named_the_week_the_last_event_turns_60_days_old() -> None:
    assert _reasons(_scene(quiet_days=59)) == []
    assert _reasons(_scene(quiet_days=60)) == ["no tournaments since Jul 30"]
    assert _reasons(_scene(quiet_days=66)) == ["no tournaments since Jul 24"]
    assert _reasons(_scene(quiet_days=67)) == []
    assert _reasons(_scene(quiet_days=300)) == []


def test_no_admin_is_named_at_14_days() -> None:
    assert _reasons(_scene(joined_days_ago=13, quiet_days=1, has_admin=False)) == []
    assert _reasons(_scene(joined_days_ago=14, quiet_days=1, has_admin=False)) == ["no admin assigned"]
    assert _reasons(_scene(joined_days_ago=21, quiet_days=1, has_admin=False)) == []


def test_a_scene_with_two_reasons_is_one_item() -> None:
    # 30 days in, never played, and nobody assigned: one line, both reasons.
    items, _ = classify_scene_health(
        [_scene(joined_days_ago=30, quiet_days=None, has_admin=False)], TODAY
    )
    assert len(items) == 1
    assert items[0]["reasons"] == ["30 days in, no tournaments yet"]
    # has_admin=False at 30 days is past the 14-day window: counted, not named.
    items, standing = classify_scene_health(
        [_scene(joined_days_ago=14, quiet_days=None, has_admin=False)], TODAY
    )
    assert items[0]["reasons"] == ["no admin assigned"]


def test_each_threshold_names_a_scene_exactly_once_across_mondays() -> None:
    """The load-bearing claim: weekly runs + 7-day windows = one naming per threshold.

    Walks one scene through 30 consecutive weekly runs from every possible offset
    within a week, so an off-by-one in the window (a `<=` for a `<`) shows up as
    a scene named twice or never.
    """
    for offset in range(WINDOW_DAYS):
        base = datetime.date(2026, 1, 1) + datetime.timedelta(days=offset)
        never = {"scene_id": 1, "display_name": "A", "joined_at": base,
                 "last_tournament": None, "has_admin": False}
        quiet = {"scene_id": 2, "display_name": "B", "joined_at": base,
                 "last_tournament": base, "has_admin": True}
        named: list[str] = []
        for week in range(30):
            run = datetime.date(2026, 1, 5) + datetime.timedelta(weeks=week)
            items, _ = classify_scene_health([never, quiet], run)
            named += [r for i in items for r in i["reasons"]]
        assert named.count("30 days in, no tournaments yet") == 1, (offset, named)
        assert named.count("90 days in, no tournaments yet") == 1, (offset, named)
        assert named.count("no admin assigned") == 1, (offset, named)
        assert sum(r.startswith("no tournaments since") for r in named) == 1, (offset, named)


def test_standing_counts_everything_past_a_threshold() -> None:
    scenes = [
        _scene(1, joined_days_ago=10, quiet_days=None),   # too new to count
        _scene(2, joined_days_ago=45, quiet_days=None),   # never played
        _scene(3, joined_days_ago=400, quiet_days=None, has_admin=False),  # both
        _scene(4, quiet_days=61),                         # quiet (named this week)
        _scene(5, quiet_days=200),                        # quiet, long-standing
        _scene(6, quiet_days=3),                          # healthy
    ]
    _, standing = classify_scene_health(scenes, TODAY)
    assert standing == {"never_played": 2, "quiet": 2, "no_admin": 1}


def test_missing_joined_at_does_not_crash() -> None:
    row = _scene(quiet_days=None)
    row["joined_at"] = None
    items, standing = classify_scene_health([row], TODAY)
    assert items == [] and standing["never_played"] == 0


# --- rendering ---------------------------------------------------------------------


def test_standing_keys_are_the_web_health_filters() -> None:
    """Each count's `?health=` key must be one digilab-web's /admin/scenes accepts
    (`SCENE_HEALTH_FILTERS` in src/lib/admin-scene-filters.ts). An unknown value is
    ignored there, so a typo here would silently link to the unfiltered list."""
    from cogs.digest import STANDING_LABELS
    assert [k for k, _ in STANDING_LABELS] == ["never_played", "quiet", "no_admin"]


STANDING = {"never_played": 23, "quiet": 34, "no_admin": 40}
URL = "https://digimon.example/admin/scenes"


def test_standing_alone_is_not_news() -> None:
    assert format_game_section("Digimon", [], [], STANDING, {}, URL) is None


def test_section_names_new_items_with_their_own_mentions() -> None:
    items = [
        {"scene_id": 1, "display_name": "Santa Rosa", "reasons": ["30 days in, no tournaments yet"]},
        {"scene_id": 2, "display_name": "Evansville", "reasons": ["no tournaments since Jul 29", "no admin assigned"]},
    ]
    closures = [{"scene_id": 3, "name": "Andyseous Odyssey", "scene_name": "Austin"}]
    out = format_game_section(
        "Digimon", items, closures, STANDING, {1: ["11"], 2: ["22", "33"], 3: ["44"]}, URL
    )
    assert out == (
        "__**Digimon**__\n"
        "**New this week:**\n"
        "• **Santa Rosa** — 30 days in, no tournaments yet · <@11>\n"
        "• **Evansville** — no tournaments since Jul 29; no admin assigned · <@22> <@33>\n"
        "\n"
        "**Stores closed this week:**\n"
        "• **Andyseous Odyssey** (Austin) · <@44>\n"
        "\n"
        "-# Standing: "
        "[23 never played](<https://digimon.example/admin/scenes?health=never_played>) · "
        "[34 quiet 60+ days](<https://digimon.example/admin/scenes?health=quiet>) · "
        "[40 without an admin](<https://digimon.example/admin/scenes?health=no_admin>)"
    )


def test_unmentioned_lines_and_zero_counts_are_omitted() -> None:
    items = [{"scene_id": 9, "display_name": "Osaka", "reasons": ["no admin assigned"]}]
    out = format_game_section(
        "Gundam", items, [], {"never_played": 0, "quiet": 5, "no_admin": 0}, {},
        "https://gundam.example/admin/scenes",
    )
    assert out == (
        "__**Gundam**__\n"
        "**New this week:**\n"
        "• **Osaka** — no admin assigned\n"
        "\n"
        "-# Standing: [5 quiet 60+ days](<https://gundam.example/admin/scenes?health=quiet>)"
    )


def test_no_standing_line_when_there_is_no_backlog() -> None:
    closures = [{"scene_id": 3, "name": "Shop", "scene_name": "Austin"}]
    out = format_game_section(
        "Digimon", [], closures, {"never_played": 0, "quiet": 0, "no_admin": 0}, {}, URL
    )
    assert out is not None and "Standing" not in out


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
