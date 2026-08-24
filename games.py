"""The bot's shared answer to "what games are there, and what do we call them?".

Four cogs need this now — `/admins`, `/scene`, `/roster` and `/requests` for
their game arguments and labels, thread_watcher for the game name in a request's
instructions, digest for its per-game sections — and before this it existed as a
private dict inside the commands cog that nothing else could reach. So a forum
thread said "New Scene Request" with no game while a slash command three lines
away could have named it.

It lives on the bot (`bot.games`), is refreshed by the same 5-minute loop that
refreshes the scene autocomplete cache, and answers two DIFFERENT questions that
are easy to conflate:

* **`live`** — which games this Discord actually coordinates. Derived from active
  scene coverage (`db.get_live_games`), never from `games.is_active`; see the
  long comment above that function for what reading the flag cost. This is what
  the digest reports on, what autocomplete offers, and what the tagline names.

* **`labels`** / **`known_ids`** — every game that exists, and what to call it. A
  superset of `live`, for two jobs: rendering a name (the admin cascade is not
  games-table-filtered and can return a row for a game with no coverage — it must
  still read "Gundam", not `gundam`), and validating a user-supplied argument.

The split matters in both directions. Using `labels` to decide what to REPORT
would reintroduce the bug this module documents. Using `live` to VALIDATE
reproduces its symptom from the other side: `/admins game:onepiece` answering
"Unknown game" about a game DigiLab has, during the bootstrap window every new
game passes through. digilab-web keeps the same two predicates apart for the same
reason (`activeGameIds` vs `claimableGameIds`).
"""

import logging

import asyncpg

import db

log = logging.getLogger(__name__)


class GameCache:
    """In-memory game registry, refreshed on the scene-cache loop.

    Starts empty and stays usable while stale — the game list changes about once
    a year, so a failed refresh is not worth degrading a command over. Callers
    that need it warm on a cold start call `ensure(pool)`.
    """

    def __init__(self) -> None:
        self.labels: dict[str, str] = {}
        self.live: list[str] = []  # game_ids with scene coverage, most scenes first

    async def refresh(self, pool: asyncpg.Pool) -> None:
        labels = await db.get_game_labels(pool)
        live = await db.get_live_games(pool)
        # Both queries complete before either assignment, and there is no await
        # between the assignments — so no coroutine can observe `live` describing
        # one world and `labels` another.
        #
        # `live` holds ids only. It used to carry (id, label) pairs, which stored
        # every live game's name a second time and gave `label()` a fallback that
        # could never fire (both queries read the same `games` table, so live is
        # always a subset of labels). One name, one place.
        self.labels = {r["game_id"]: r["short_name"] or r["game_id"] for r in labels}
        self.live = [r["game_id"] for r in live]

    async def ensure(self, pool: asyncpg.Pool) -> None:
        """Refresh only if still cold. Cheap to call on a command's slow path."""
        if not self.labels:
            await self.refresh(pool)

    def label(self, game_id: str | None, default: str = "All games") -> str:
        """Display name for a game id.

        Falls back to the id itself rather than to a placeholder: an unlabelled
        `gundam` is ugly but true, and tells whoever sees it exactly which row to
        go look at. `default` covers the "no game / every game" case.
        """
        if not game_id:
            return default
        return self.labels.get(game_id, game_id)

    def live_ids(self) -> set[str]:
        """Games this Discord actually coordinates. The set to REPORT on."""
        return set(self.live)

    def known_ids(self) -> set[str]:
        """Every game that exists at all. The set to VALIDATE a user argument against.

        Deliberately wider than `live_ids`. digilab-web keeps the same two
        predicates apart (`activeGameIds` vs `claimableGameIds`) after a picker
        offered a game its own endpoint then rejected, and the bot has the same
        trap: validating `/admins game:onepiece` against coverage answers "Unknown
        game" for a game that plainly exists, and the bootstrap window is real —
        the first scene request for a new game necessarily arrives before that
        game has any scene. An empty result ("no admins for that game here") is
        both true and more informative than a false rejection.
        """
        return set(self.labels)

    def live_labels(self) -> list[str]:
        return [self.label(game_id) for game_id in self.live]

    def live_choices(self) -> list[tuple[str, str]]:
        """(game_id, label) pairs in coverage order, for slash-command autocomplete."""
        return [(game_id, self.label(game_id)) for game_id in self.live]

    def tagline(self) -> str:
        """The bot's own one-line identity, naming the games it covers.

        Replaces the hardcoded "DigiLab — Digimon TCG Tournament Tracker" that sat
        in `/help` and the `/scene` footer. Those were written when there was one
        game; they now sit under embeds that may be reporting entirely Gundam
        numbers. Built from the live list so game #3 needs no edit, and degrading
        to the bare platform name when the cache is cold — never to a guess.
        """
        names = self.live_labels()
        if not names:
            return "DigiLab — TCG Tournament Tracker"
        if len(names) == 1:
            return f"DigiLab — {names[0]} Tournament Tracker"
        return f"DigiLab — Tournament tracking for {', '.join(names[:-1])} & {names[-1]}"
