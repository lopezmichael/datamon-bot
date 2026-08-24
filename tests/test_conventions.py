"""Mechanical guards for the CLAUDE.md rules whose violation is SILENT.

This file exists because the two most expensive bugs in this repo were both
valid code. Nothing raised, no loop died, no alert fired — a query simply
answered a narrower question than the caller believed, and the wrong answer
looked exactly like the right one:

* reading `games.is_active` to decide behaviour made the weekly digest cover
  Digimon only and report it as a complete picture, and made `/admins
  game:gundam` answer "Unknown game" about a game with 16 scenes and 11 admins;
* a game name hardcoded in outgoing copy reads as fine until a second game
  exists, and then it is confidently wrong at the people it is routing.

digilab-web ratchets the first of these in `conventions.test.ts` precisely
because a prose rule in its CLAUDE.md had not stopped its badge cron. The bot
had the identical bug and no guard at all. This is that guard, sized to what
this project actually runs: standalone stdlib test files, no pytest, no CI.

    .venv/bin/python tests/test_conventions.py

**A ratchet that cannot fail is worse than no ratchet**, because it reads as
coverage. Every check here has been verified to fail against a deliberate
violation — see the mutation notes on each test. If you change one, re-verify
it the same way rather than trusting that it stayed green for a good reason.
"""

import ast
import io
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _source_files() -> list[Path]:
    """Every module the bot actually runs. Tests are excluded deliberately.

    Test files legitimately name games (asserting their absence elsewhere needs
    them as literals), so scanning this directory would make the game-name check
    self-defeating.
    """
    return sorted(
        [p for p in REPO_ROOT.glob("*.py")]
        + [p for p in (REPO_ROOT / "cogs").glob("*.py")]
        + [p for p in (REPO_ROOT / "scripts").glob("*.py")]
    )


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _string_literals(path: Path):
    """(lineno, value) for every string literal that is not a docstring."""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                docstrings.add(id(node.body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value


# ---------------------------------------------------------------------------
# 1. `games.is_active` must not decide behaviour
# ---------------------------------------------------------------------------
#
# Two things make the obvious version of this check useless, and both were hit
# while writing it:
#
# 1. Grepping source LINES flags the prose. This rule is argued at length in
#    comments and in games.py's module docstring, all of which name
#    `games.is_active` in order to forbid it — so a line scan reports the
#    documentation as the violation. Scanning non-docstring string literals via
#    the AST leaves only SQL, which is the only place the gate can actually live.
# 2. Grepping for "FROM games ... WHERE ... is_active" flags the CORRECT queries.
#    Every liveness query joins `scene_games sg ... WHERE sg.is_active = TRUE`,
#    which that pattern matches happily — a ratchet that fires on the fix.
#
# So: per SQL literal, work out what `games` is aliased to *in that literal*, and
# look for that alias's is_active. `sg.is_active` on scene_games is untouched.

_GAMES_ALIAS = re.compile(r"\b(?:FROM|JOIN)\s+games\s+(?:AS\s+)?([a-z_][a-z0-9_]*)", re.I)


def test_no_behaviour_reads_games_is_active() -> None:
    """Mutation-verified: adding `AND g.is_active = TRUE` to get_live_games fails this."""
    offenders: list[str] = []
    for path in _source_files():
        for lineno, literal in _string_literals(path):
            aliases = {m.group(1).lower() for m in _GAMES_ALIAS.finditer(literal)}
            aliases.discard("on")  # `JOIN games ON ...` binds no alias
            for alias in {"games"} | aliases:
                if re.search(rf"\b{re.escape(alias)}\.is_active\b", literal, re.I):
                    offenders.append(f"{_rel(path)}:{lineno}: alias `{alias}`")

    assert not offenders, (
        "`games.is_active` is read as a behavioural gate here:\n  "
        + "\n  ".join(offenders)
        + "\n\nThat column is a second, divergent answer to \"is this game live\" and it "
        "has already been wrong in production (Gundam is FALSE with 16 active scenes). "
        "A game is live here if it has active scene coverage — use db.get_live_games. "
        "Reading the column to DISPLAY a name is fine (db.get_game_labels)."
    )


# ---------------------------------------------------------------------------
# 2. No game name may be hardcoded in a string the bot emits
# ---------------------------------------------------------------------------
#
# Walks string LITERALS via the AST rather than scanning source lines, so the
# many comments and docstrings that discuss Digimon and Gundam by name — the
# reasoning this codebase is built on — do not trip it. That precision is what
# lets the check be repo-wide instead of a hand-listed set of template dicts,
# which is the version that went green when a third dict was added.

_GAME_NAMES = re.compile(
    r"(?i)\b(digimon|gundam|one[\s_]?piece|fusion[\s_]?world|union[\s_]?arena|naruto)\b"
)

# Sanctioned occurrences, as (file, exact substring). Keep this list SHORT and
# make each entry argue for itself — an entry here is a promise that the literal
# is a database default, not user-facing copy.
_ALLOWED = {
    # `admin_requests.game_id` is NOT NULL DEFAULT 'digimon' with an FK to games,
    # so these COALESCEs cannot fire. They stay because a NULL reaching a
    # `game_id = $n` predicate would silently empty a mention list instead of
    # failing loudly, and digilab-web's ledger explicitly sanctions 'digimon' as
    # a default. Not copy: never rendered to anyone.
    ("db.py", "COALESCE(game_id, 'digimon')"),
}


def test_no_game_name_in_any_emitted_string() -> None:
    """Mutation-verified: a game name in any template, in any module, fails this."""
    offenders: list[str] = []
    for path in _source_files():
        rel = _rel(path)
        for lineno, value in _string_literals(path):
            if any(rel == f and sub in value for f, sub in _ALLOWED):
                continue
            match = _GAME_NAMES.search(value)
            if match:
                excerpt = value[max(0, match.start() - 25):match.end() + 25]
                offenders.append(f"{rel}:{lineno}: ...{excerpt.strip()!r}...")

    assert not offenders, (
        "game name hardcoded in a string literal:\n  "
        + "\n  ".join(offenders)
        + "\n\nEvery game name in outgoing copy must come from `games.short_name` via "
        "`bot.games` (see games.py), so game #3 is a database row and not a code edit. "
        "If this literal is a sanctioned database default rather than copy, add it to "
        "_ALLOWED above with a reason."
    )


# ---------------------------------------------------------------------------
# 3. Every query goes through the dead-connection retry wrappers
# ---------------------------------------------------------------------------

_DIRECT_POOL_CALL = re.compile(r"\bpool\.(fetch|fetchrow|execute)\s*\(")


def test_no_direct_pool_calls() -> None:
    """Mutation-verified: `await pool.fetch(...)` anywhere fails this.

    `_fetch` / `_fetchrow` / `_run` retry when Neon has dropped the pooled
    connection out from under us — a direct call skips that and surfaces as an
    episodic, unreproducible failure on the 5-minute loops. Passing `pool.fetch`
    as a *reference* to `_run` is the sanctioned form and does not match, since
    it is not a call.
    """
    offenders: list[str] = []
    for path in _source_files():
        for lineno, line in enumerate(io.open(path, encoding="utf-8"), 1):
            if _DIRECT_POOL_CALL.search(line):
                offenders.append(f"{_rel(path)}:{lineno}: {line.strip()[:80]}")

    assert not offenders, (
        "direct pool call, bypassing the dead-connection retry:\n  "
        + "\n  ".join(offenders)
        + "\n\nGo through db._fetch / _fetchrow / _run. Anything routed through _run "
        "must also be safe to run more than once."
    )


# ---------------------------------------------------------------------------
# 4. The scan itself must be looking at something
# ---------------------------------------------------------------------------

def test_the_scanners_actually_see_the_codebase() -> None:
    """A ratchet over zero files passes forever. Pin that it found the real modules."""
    files = {_rel(p) for p in _source_files()}
    for expected in (
        "db.py", "games.py", "messages.py", "utils.py",
        "cogs/commands.py", "scripts/check_imports.py",
    ):
        assert expected in files, f"{expected} missing from the scan set"

    literals = sum(1 for _ in _string_literals(REPO_ROOT / "messages.py"))
    assert literals > 5, f"only {literals} literals found in messages.py — the AST walk is broken"


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
