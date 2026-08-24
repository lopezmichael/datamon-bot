#!/usr/bin/env python3
"""Import every module the bot runs, and fail if any of them cannot be imported.

    python scripts/check_imports.py

## Why this is separate from tests/

Five of the seven cogs — archiver, nudge, reactions, role_sync, thread_watcher —
are imported by no test at all. The suite reaches `cogs.commands` and
`cogs.digest` only because it pulls two pure functions out of them. Everything
else is loaded for the first time by `bot.load_extension` at boot, in production.

So a NameError at module level, a bad import, a decorator applied to the wrong
thing — none of it is caught by `tests/run.py`, and none of it is caught by
`compileall` either, which only checks that the file parses. It surfaces as a
crash loop on Railway after the deploy is already live.

This closes that gap the cheap way: import all of them and see.

## Why it needs a full environment, and tests/ does not

`config.py` fail-fasts on missing env vars by design, and importing any cog pulls
`config` in transitively. The test suite works around that by stubbing `config`
in `sys.modules` — correct there, because those tests exercise pure functions and
must not need credentials. But a stub would defeat THIS check: the point is to
load the real modules the way the bot does.

So CI supplies inert dummy values for all 28 required vars. They are never used —
nothing here opens a socket. `setup()` is not called on any cog, so no loop
starts, no pool is created, and no gateway connection is attempted; importing a
cog module only defines its class.

Deliberately NOT deriving the dummy list from config.py: when someone adds a new
required var, CI failing here is the reminder that Railway needs it too. A
self-updating list would make that silent, which is the same trade this repo
keeps making the other way.
"""

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Top-level modules, then every cog discovered on disk — discovered rather than
# listed so a new cog is covered the moment it exists.
TOP_LEVEL = ["config", "db", "games", "messages", "utils", "bot"]


def _cog_modules() -> list[str]:
    cogs_dir = REPO_ROOT / "cogs"
    return [
        f"cogs.{m.name}"
        for m in pkgutil.iter_modules([str(cogs_dir)])
        if not m.name.startswith("_")
    ]


def main() -> int:
    modules = TOP_LEVEL + _cog_modules()
    failures: list[tuple[str, BaseException]] = []

    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - report anything, including SystemExit
            failures.append((name, exc))
            print(f"FAIL  {name}")
        else:
            print(f"ok    {name}")

    print("─" * 60)
    if failures:
        for name, exc in failures:
            print(f"\n{name}:")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        print(f"\n{len(failures)} of {len(modules)} modules failed to import.")
        return 1

    cog_count = len(_cog_modules())
    print(f"All {len(modules)} modules import cleanly ({cog_count} cogs).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
