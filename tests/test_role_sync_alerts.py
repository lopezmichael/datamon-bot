"""role_sync's reporting: what reaches #bot-log when a run skips, is refused, or is cut short.

No DB, no Discord, no network. The cog runs against a fake guild and fake db
helpers, and `log_to_discord` is a list. Role-sync SEMANTICS (who gets which
role) are not under test here, only that nothing it does or fails to do is
silent any more.

    .venv/bin/python tests/test_role_sync_alerts.py

`config` is stubbed before the import because it fail-fasts on missing env vars
by design.
"""

import asyncio
import logging
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
config = sys.modules.setdefault("config", types.ModuleType("config"))
config.GUILD_ID = 1
config.ROLE_MAP = {"scene_admin": 10}
config.DIGILAB_ROLE_IDS = {10}
config.GAME_ROLES = {}

import discord  # noqa: E402

import cogs.role_sync as rs  # noqa: E402
from utils import IterationTimeout, run_bounded  # noqa: E402

logging.disable(logging.CRITICAL)

POSTED: list[str] = []


async def _capture(message: str) -> None:
    POSTED.append(message)


rs.log_to_discord = _capture
# ProblemSetAlert posts through utils' own global.
import utils  # noqa: E402
utils.log_to_discord = _capture


async def _no_sleep(_seconds) -> None:
    return None


class _Resp:
    status = 403
    reason = "Forbidden"


class _Role:
    def __init__(self, role_id: int, name: str) -> None:
        self.id, self.name = role_id, name


SCENE_ROLE = _Role(10, "Scene Admin")


class _Member:
    def __init__(self, member_id: int, *, refuse: bool = False, hang_after_add: bool = False,
                 name: str | None = None) -> None:
        self.id = member_id
        self.display_name = name or f"user{member_id}"
        self.mention = f"<@{member_id}>"
        self.roles: list = []
        self.bot = False
        self._refuse = refuse
        self._hang = hang_after_add

    async def add_roles(self, role, **_kw) -> None:
        if self._refuse:
            raise discord.Forbidden(_Resp(), "Missing Permissions")
        self.roles.append(role)
        if self._hang:
            await asyncio.sleep(3600)

    async def remove_roles(self, role, **_kw) -> None:
        raise discord.Forbidden(_Resp(), "Missing Permissions")


class _Guild:
    def __init__(self, members) -> None:
        self.members = members

    def get_member(self, member_id):
        return next((m for m in self.members if m.id == member_id), None)

    def get_role(self, role_id):
        return SCENE_ROLE if role_id == 10 else None


class _Bot:
    pool = None

    def __init__(self, guild) -> None:
        self.guild = guild

    def get_guild(self, _guild_id):
        return self.guild

    async def change_presence(self, **_kw) -> None:
        return None


def _admins(*ids: int):
    async def fetch(_pool):
        return [{"discord_user_id": str(i), "role": "scene_admin"} for i in ids]
    return fetch


async def _scene_count(_pool):
    return 3


def _setup(admin_ids) -> None:
    POSTED.clear()
    rs.db.get_active_admins = _admins(*admin_ids)
    rs.db.get_scene_count = _scene_count
    # role_sync's only asyncio use is its 1-second courtesy sleep. Swap the
    # module's NAME, never `asyncio.sleep` itself, which everything shares.
    rs.asyncio = types.SimpleNamespace(sleep=_no_sleep)


def test_a_skipped_run_raises_so_the_alerter_reports_it() -> None:
    _setup([1])
    for guild, needle in ((None, "not in the bot's cache"), (_Guild([_Member(1)]), "GUILD_MEMBERS")):
        cog = rs.RoleSync(_Bot(guild))
        try:
            asyncio.run(cog._run_sync())
        except RuntimeError as exc:
            assert needle in str(exc), exc
        else:
            raise AssertionError("a skipped run must not return quietly")


def test_refusals_post_once_and_absent_admins_never() -> None:
    # Admin 999 is not in the guild: legitimate, and must stay unreported.
    _setup([1, 999])
    cog = rs.RoleSync(_Bot(_Guild([_Member(1, refuse=True), _Member(2)])))
    for _ in range(3):
        asyncio.run(cog._run_sync())
    refusals = [m for m in POSTED if "not allowed" in m]
    assert len(refusals) == 1, POSTED
    assert "user1 (1)" in refusals[0] and "999" not in refusals[0]


def test_refusal_lines_escape_user_chosen_names() -> None:
    line = rs._forbidden_line("add", _Role(10, "**Boss**"), _Member(5, name="@everyone _x_"))
    assert "@everyone" not in line, line
    assert "\\*\\*Boss\\*\\*" in line and "\\_x\\_" in line, line
    assert "(5)" in line


def test_changes_applied_before_a_timeout_are_still_logged() -> None:
    # Member 1's grant lands and is recorded; member 2's grant then hangs. The
    # run is cut short by run_bounded, and member 1's grant must still reach
    # #bot-log (before the fix, the post sat at the end of the run and was lost).
    _setup([1, 2])
    cog = rs.RoleSync(_Bot(_Guild([_Member(1), _Member(2, hang_after_add=True)])))

    async def go():
        try:
            await run_bounded(cog._run_sync(), 0.05, "Role sync")
        except IterationTimeout:
            return
        raise AssertionError("the hung run should have timed out")

    asyncio.run(go())
    logged = [m for m in POSTED if "Role Sync" in m]
    assert len(logged) == 1 and "cut short" in logged[0] and "<@1>" in logged[0], POSTED
    assert "<@2>" not in logged[0], "a grant that never returned is not claimed"


def test_a_clean_run_logs_its_changes_without_the_cut_short_note() -> None:
    _setup([1])
    cog = rs.RoleSync(_Bot(_Guild([_Member(1), _Member(2)])))
    asyncio.run(cog._run_sync())
    logged = [m for m in POSTED if "Role Sync" in m]
    assert len(logged) == 1 and "cut short" not in logged[0] and "Scene Admin" in logged[0], POSTED


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
