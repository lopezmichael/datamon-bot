"""Pure-function tests for the /admins embed size guard.

No DB, no Discord, no network. Discord answers 400 on a description over 4096
characters and the interaction then shows "the application did not respond", so
this is the difference between a long answer and no answer at all.

Run with the venv's interpreter (discord.py must import):

    .venv/bin/python tests/test_embed_fit.py

`config` is stubbed before the import because it fail-fasts on missing env vars
by design.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("config", types.ModuleType("config"))

from cogs.commands import (  # noqa: E402
    EMBED_DESCRIPTION_LIMIT,
    fit_embed_description,
)


# ---------------------------------------------------------------------------
# fit_embed_description
# ---------------------------------------------------------------------------

def test_short_blocks_are_joined_untouched() -> None:
    out = fit_embed_description(["**Digimon**\na\nb", "**Global**\nc"])
    assert out == "**Digimon**\na\nb\n\n**Global**\nc"
    assert "not shown" not in out


def test_oversized_input_is_capped_and_says_what_was_cut() -> None:
    big = "**Digimon**\n" + "\n".join(f"<@{i}> line" for i in range(300))
    blocks = [big, big, big]
    out = fit_embed_description(blocks)
    assert len(out) <= EMBED_DESCRIPTION_LIMIT
    assert "more not shown" in out
    # 300 entries per dropped block, and at least one block was dropped.
    assert "+600 more not shown" in out


def test_a_single_block_too_large_is_truncated_not_dropped() -> None:
    huge = "**Digimon**\n" + "\n".join(f"<@{i}>" for i in range(2000))
    out = fit_embed_description([huge])
    assert len(out) <= EMBED_DESCRIPTION_LIMIT
    assert out.startswith("**Digimon**")
    assert "more not shown" in out


def test_empty_input_is_empty_not_a_crash() -> None:
    assert fit_embed_description([]) == ""


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
