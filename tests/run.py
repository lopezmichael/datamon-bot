#!/usr/bin/env python3
"""Run every test file and exit non-zero if ANY of them failed.

    .venv/bin/python tests/run.py

## Why this exists

The documented way to run these tests was a shell loop:

    for t in tests/*.py; do .venv/bin/python "$t"; done

**That loop cannot fail.** A `for` loop's exit status is the status of its LAST
command, so a failure in any file but the alphabetically-last one returns 0.
Verified by dropping a deliberately-failing `aa_temp_fail.py` into tests/: the
loop printed the failure and exited 0. Every green run of that command was
evidence of nothing, which is the same trap `test_conventions.py` exists to
prevent — a check that reads as coverage and cannot report a problem.

CI needs one command with a truthful exit code, and so does anyone running this
by hand before a commit.

## Subprocess per file, deliberately

Each test file stubs `config` in `sys.modules` before importing the modules under
test, because `config` fail-fasts on missing env vars by design. Running them all
in one interpreter would make that stub shared mutable state between files, so
the result would depend on import order. A subprocess each keeps them honest and
costs a few hundred milliseconds total.

## No environment needed

Every file here runs with no `.env`, no database and no Discord credentials —
verified. If you add a test that needs any of those, it does not belong in this
directory: dev, preview and local all point at production's Neon instance, so a
test that connects is a test that can write to prod.
"""

import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def main() -> int:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if not files:
        print("ERROR: no test_*.py files found — the runner is looking in the wrong place")
        return 1

    failed: list[str] = []
    suspicious: list[str] = []
    total_ok = 0

    for path in files:
        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
        )
        oks = sum(1 for line in proc.stdout.splitlines() if line.startswith("ok "))
        total_ok += oks

        print(f"\n── {path.name}")
        if proc.stdout.strip():
            print(proc.stdout.rstrip())
        if proc.stderr.strip():
            print(proc.stderr.rstrip(), file=sys.stderr)

        if proc.returncode != 0:
            failed.append(path.name)
        elif oks == 0:
            # Exited clean having asserted nothing. Usually an import guard that
            # swallowed everything, or a file whose `__main__` block never ran.
            # Green-with-zero-assertions is the failure mode this runner is for.
            suspicious.append(path.name)

    print("\n" + "─" * 60)
    print(f"{total_ok} passing across {len(files)} files")

    for name in suspicious:
        print(f"WARNING: {name} exited 0 but ran no assertions — is it wired up?")
    for name in failed:
        print(f"FAILED:  {name}")

    if failed:
        print(f"\n{len(failed)} file(s) failed.")
        return 1
    if suspicious:
        print(f"\n{len(suspicious)} file(s) ran nothing. Treating as a failure.")
        return 1

    print("All green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
