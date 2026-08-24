"""Pure-function tests for react-to-resolve authorization.

No DB, no Discord, no network. `AdminAccess.covers` decides who may resolve a
request, and the shape it replaced handed out global rights by accident, so it is
worth pinning without a live server.

Run with the venv's interpreter (discord.py + asyncpg must import):

    .venv/bin/python tests/test_admin_access.py

`config` is stubbed before the import because it fail-fasts on missing env vars
by design.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("config", types.ModuleType("config"))

import db  # noqa: E402


def _rows(*scene_ids, game="digimon"):
    return tuple({"scene_id": sid, "game_id": game} for sid in scene_ids)


# ---------------------------------------------------------------------------
# AdminAccess.covers
# ---------------------------------------------------------------------------

def test_no_access_covers_nothing() -> None:
    access = db.AdminAccess(db.ADMIN_ACCESS_NONE)
    assert not access.covers(1)
    assert not access.covers(None)


def test_global_covers_every_scene_and_scene_less_requests() -> None:
    access = db.AdminAccess(db.ADMIN_ACCESS_GLOBAL)
    assert access.covers(1)
    assert access.covers(999)
    assert access.covers(None)


def test_scoped_covers_only_its_own_scenes() -> None:
    access = db.AdminAccess(db.ADMIN_ACCESS_SCOPED, _rows(4, 7))
    assert access.covers(4)
    assert access.covers(7)
    assert not access.covers(5)


def test_scoped_with_no_rows_is_the_opposite_of_global() -> None:
    """The H1 regression: an empty row set used to read as "global admin".

    A Digimon-only scene admin asked about a Gundam request resolves to 'scoped'
    with zero rows. That must cover nothing — not every scene, and not the
    scene-less requests either.
    """
    access = db.AdminAccess(db.ADMIN_ACCESS_SCOPED, ())
    assert not access.covers(1)
    assert not access.covers(None)
    assert access.level != db.ADMIN_ACCESS_GLOBAL


def test_scoped_admin_may_take_scene_less_requests_in_their_game() -> None:
    access = db.AdminAccess(db.ADMIN_ACCESS_SCOPED, _rows(4))
    assert access.covers(None)


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
