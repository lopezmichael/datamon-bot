"""Tests for the loop watchdog and the alerts that end role_sync's silent skips.

No DB, no Discord, no network: `log_to_discord` is replaced with a list, and the
loops under watch are REAL `discord.ext.tasks.Loop` objects, so the death modes
asserted here (an escaped exception, a stray CancelledError) are the library's
actual behaviour in the installed version, not a model of it.

    .venv/bin/python tests/test_loop_watchdog.py

`config` is stubbed before the import because it fail-fasts on missing env vars
by design.
"""

import asyncio
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.modules.setdefault("config", types.ModuleType("config"))

from discord.ext import tasks  # noqa: E402

import utils  # noqa: E402

# The watchdog logs every death and check failure to stdout by design; the tests
# provoke dozens. Keep the run readable.
import logging  # noqa: E402
logging.disable(logging.CRITICAL)
from utils import (  # noqa: E402
    CRASH_LOOP_ALERT_AT,
    IterationTimeout,
    LoopFailureAlerter,
    LoopWatchdog,
    ProblemSetAlert,
    TRANSIENT_LOOP_EXCEPTIONS,
    describe_exit,
    format_duration,
    is_stale,
    restart_delay,
    run_bounded,
)

POSTED: list[str] = []


async def _capture(message: str) -> None:
    POSTED.append(message)


utils.log_to_discord = _capture


def _reset() -> None:
    POSTED.clear()


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------

def test_format_duration_picks_a_readable_unit() -> None:
    assert format_duration(45) == "45 s"
    assert format_duration(20 * 60) == "20 min"
    assert format_duration(3.5 * 3600) == "3.5 h"
    assert format_duration(30 * 3600) == "30.0 h"
    assert format_duration(3 * 86400) == "3.0 d"


def test_restart_delay_is_immediate_then_backs_off_to_an_hour() -> None:
    assert restart_delay(1) == 0
    assert [restart_delay(n) for n in (2, 3, 4, 5)] == [60, 120, 240, 480]
    assert restart_delay(8) == 3600
    assert restart_delay(50) == 3600, "the cap must hold, not overflow into days"


def test_is_stale_measures_from_the_later_of_last_run_and_start() -> None:
    # Never completed, but only just started: not stale.
    assert not is_stale(None, since=1000, now=1000 + 60, stale_after=1200)
    # Never completed, started long ago: stale.
    assert is_stale(None, since=1000, now=1000 + 1201, stale_after=1200)
    # Completed recently: not stale.
    assert not is_stale(5000, since=1000, now=5000 + 600, stale_after=1200)
    # Completed long ago: stale.
    assert is_stale(5000, since=1000, now=5000 + 1300, stale_after=1200)
    # A restart after the last completion resets the clock.
    assert not is_stale(1000, since=9000, now=9000 + 60, stale_after=1200)


def test_alerter_stamps_a_heartbeat_on_every_completed_run() -> None:
    _reset()
    a = LoopFailureAlerter("x", threshold=2)
    assert a.last_completed is None
    asyncio.run(a.failed(ValueError("boom")))
    first = a.last_completed
    assert first is not None, "a handled failure is still a completed run"
    asyncio.run(a.recovered())
    assert a.last_completed >= first
    a.beat()
    assert POSTED == [], "below threshold and beat() must post nothing"


def test_problem_set_alert_posts_on_change_and_clear_only() -> None:
    alert = ProblemSetAlert("Thing broken", hint="Fix it.")
    assert alert.transition([]) is None, "nothing wrong and nothing posted: silent"

    first = alert.transition(["b", "a"])
    assert first and "Thing broken" in first and "• a\n• b" in first and "Fix it." in first

    assert alert.transition(["a", "b"]) is None, "same set in a new order is not news"
    assert alert.transition(["b", "a", "a"]) is None, "duplicates are not news either"

    changed = alert.transition(["a", "b", "c"])
    assert changed and "• c" in changed

    cleared = alert.transition([])
    assert cleared and cleared.startswith("✅ Cleared")
    assert alert.transition([]) is None


def test_problem_set_alert_caps_a_long_list() -> None:
    alert = ProblemSetAlert("Many")
    message = alert.transition([f"line {i:03d}" for i in range(40)])
    assert message.count("• ") == ProblemSetAlert.MAX_LINES
    assert "…and 25 more" in message


# ---------------------------------------------------------------------------
# run_bounded
# ---------------------------------------------------------------------------

def test_run_bounded_turns_only_its_own_deadline_into_iteration_timeout() -> None:
    async def hangs():
        await asyncio.sleep(10)

    async def inner_timeout():
        raise TimeoutError("the HTTP client's own timeout")

    async def fine():
        return 42

    async def go():
        assert await run_bounded(fine(), 1, "X") == 42

        try:
            await run_bounded(hangs(), 0.02, "X")
        except IterationTimeout as exc:
            assert "X run exceeded" in str(exc)
            # The point of the type: the loop bodies' transient clause must NOT
            # swallow it into discord.ext.tasks' silent retry.
            assert not isinstance(exc, TRANSIENT_LOOP_EXCEPTIONS)
        else:
            raise AssertionError("a hung run must raise IterationTimeout")

        try:
            await run_bounded(inner_timeout(), 1, "X")
        except IterationTimeout:
            raise AssertionError("an inner TimeoutError is not our deadline")
        except TimeoutError:
            pass

    asyncio.run(go())


# ---------------------------------------------------------------------------
# The watchdog, against real tasks.Loop objects
# ---------------------------------------------------------------------------

class _Cog:
    """A loop whose behaviour each test sets via `mode`."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.alerter = LoopFailureAlerter("Test loop", threshold=1)
        self.runs = 0

    @tasks.loop(seconds=0.01)
    async def lp(self) -> None:
        self.runs += 1
        if self.mode == "raise":
            raise ValueError("boom")
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        if self.mode == "hang":
            await asyncio.sleep(3600)
        await self.alerter.recovered()

    @lp.error
    async def _quiet(self, exc) -> None:
        # The library's default handler logs a traceback; keep the test output clean.
        pass


async def _settle() -> None:
    await asyncio.sleep(0.05)


def test_describe_exit_names_each_way_a_task_ends() -> None:
    async def go():
        async def boom():
            raise ValueError("bad")

        async def ok():
            return None

        t1 = asyncio.create_task(boom())
        t2 = asyncio.create_task(ok())
        t3 = asyncio.create_task(asyncio.sleep(10))
        await asyncio.sleep(0)
        t3.cancel()
        await asyncio.gather(t1, t2, t3, return_exceptions=True)
        assert describe_exit(t1) == "ValueError: bad"
        assert describe_exit(t2) == "returned without an error"
        assert "CancelledError" in describe_exit(t3)
        assert describe_exit(None) == "never started"

    asyncio.run(go())


def test_a_loop_killed_by_an_exception_is_reported_and_restarted() -> None:
    _reset()

    async def go():
        cog = _Cog("raise")
        dog = LoopWatchdog()
        dog.watch(cog.lp, cog.alerter, stale_after=600)
        await _settle()
        assert not cog.lp.is_running(), "precondition: discord.ext.tasks stopped the loop"

        cog.mode = "ok"
        await dog.check(now=0)
        assert len(POSTED) == 1 and "ValueError: boom" in POSTED[0] and "Test loop" in POSTED[0]
        assert cog.lp.is_running(), "first death restarts immediately"

        await _settle()
        await dog.check(now=1)
        assert "running again" in POSTED[-1], POSTED
        dog.unwatch(cog.lp)
        await _settle()

    asyncio.run(go())


def test_a_stray_cancelled_error_is_caught_although_dot_error_never_fires() -> None:
    _reset()

    async def go():
        cog = _Cog("cancel")
        dog = LoopWatchdog()
        dog.watch(cog.lp, cog.alerter, stale_after=600)
        await _settle()
        assert not cog.lp.is_running()
        assert not cog.lp.failed(), "the library does not even mark this as failed"

        cog.mode = "ok"
        await dog.check(now=0)
        assert len(POSTED) == 1 and "CancelledError" in POSTED[0]
        assert cog.lp.is_running()
        dog.unwatch(cog.lp)
        await _settle()

    asyncio.run(go())


def test_a_crash_loop_backs_off_and_posts_once_more_only() -> None:
    _reset()

    async def go():
        cog = _Cog("raise")
        dog = LoopWatchdog()
        dog.watch(cog.lp, cog.alerter, stale_after=600)
        now = 0.0
        for death in range(1, 6):
            await _settle()
            assert not cog.lp.is_running()
            await dog.check(now=now)
            if death == 1:
                assert cog.lp.is_running(), "first restart is immediate"
            else:
                assert not cog.lp.is_running(), f"death {death} must wait out the backoff"
                await dog.check(now=now + restart_delay(death) - 1)
                assert not cog.lp.is_running(), "still inside the backoff window"
                await dog.check(now=now + restart_delay(death))
                assert cog.lp.is_running(), "restarted once the backoff elapsed"
            now += restart_delay(death) + 1

        deaths_posted = [m for m in POSTED if "💀" in m]
        assert len(deaths_posted) == 2, deaths_posted
        assert f"died {CRASH_LOOP_ALERT_AT} times" in deaths_posted[1]
        dog.unwatch(cog.lp)
        await _settle()

    asyncio.run(go())


def test_a_hung_loop_is_reported_once_and_its_recovery_announced() -> None:
    _reset()

    async def go():
        cog = _Cog("hang")
        dog = LoopWatchdog()
        dog.watch(cog.lp, cog.alerter, stale_after=600)
        await _settle()
        assert cog.lp.is_running(), "a hung loop looks alive"
        since = dog._watched[id(cog.lp)].since

        await dog.check(now=since + 599)
        assert POSTED == []
        await dog.check(now=since + 601)
        assert len(POSTED) == 1 and "has not completed a run" in POSTED[0]
        await dog.check(now=since + 900)
        assert len(POSTED) == 1, "stale is posted once, not every check"

        cog.alerter.beat()
        await dog.check(now=cog.alerter.last_completed + 1)
        assert "completed a run again" in POSTED[-1]
        assert cog.lp.is_running(), "stale loops are not restarted"
        dog.unwatch(cog.lp)
        await _settle()

    asyncio.run(go())


def test_unwatch_stops_the_loop_without_a_death_alert() -> None:
    _reset()

    async def go():
        cog = _Cog("ok")
        dog = LoopWatchdog()
        dog.watch(cog.lp, cog.alerter, stale_after=600)
        await _settle()
        dog.unwatch(cog.lp)
        await _settle()
        assert not cog.lp.is_running()
        await dog.check(now=10_000)
        assert POSTED == []

    asyncio.run(go())


def test_the_watchdog_survives_its_own_failures_and_stops_only_on_stop() -> None:
    _reset()
    original = utils.WATCHDOG_CHECK_SECONDS
    utils.WATCHDOG_CHECK_SECONDS = 0.005

    async def go():
        dog = LoopWatchdog()
        calls = {"n": 0}

        async def flaky_check(now):
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncio.CancelledError()  # a stray, not a cancel of this task
            if calls["n"] <= 4:
                raise RuntimeError("check bug")

        dog.check = flaky_check
        dog.start()
        await asyncio.sleep(0.2)
        assert dog._task and not dog._task.done(), "a stray cancel or a bug must not end it"
        assert calls["n"] > 4
        assert any("Loop watchdog" in m and "check bug" in m for m in POSTED), (
            "three consecutive check failures must reach #bot-log"
        )

        task = dog._task
        dog.stop()
        await asyncio.sleep(0.02)
        assert task.cancelled() and dog._task is task, "stop() must not trigger a self-restart"

    try:
        asyncio.run(go())
    finally:
        utils.WATCHDOG_CHECK_SECONDS = original


def test_the_watchdog_restarts_itself_if_its_task_ends() -> None:
    _reset()
    original = utils.WATCHDOG_CHECK_SECONDS
    utils.WATCHDOG_CHECK_SECONDS = 0.005

    async def go():
        dog = LoopWatchdog()

        async def dies():
            # _run is `while True`; ending at all is the bug this guards.
            return None

        first_run = dog._run
        dog._run = dies
        dog.start()
        first = dog._task
        await asyncio.sleep(0.02)
        dog._run = first_run
        # The done-callback restarted it with the real _run.
        assert first.done() and dog._task is not first and not dog._task.done()
        await asyncio.sleep(0.02)
        assert any("ended unexpectedly" in m for m in POSTED), POSTED
        dog.stop()
        await asyncio.sleep(0.01)

    try:
        asyncio.run(go())
    finally:
        utils.WATCHDOG_CHECK_SECONDS = original


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
