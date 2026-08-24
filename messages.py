"""Datamon Bot — Message templates for forum thread responses.

# Naming games in copy

DigiLab went multi-game, and this module is where that stops being a data
concern and becomes something a human reads. Two rules hold everything here
together:

**Never hardcode a game.** Not "Digimon TCG events", not "the Digimon team".
Every game name in an outgoing message arrives as an argument, sourced from
`games.short_name` in the database, and the templates below name none of them.
A fourth game must not require editing a string in this file — the same reason
`db.get_live_games` derives liveness from scene coverage instead of a flag.

**Say which game, whenever we know.** An app-created thread carries
`admin_requests.game_id`, so its instructions can and should name the game — the
admins reading #scene-requests now cover two games with different teams, and
"New Scene Request" alone makes them open the thread to find out whose it is.
A manually created thread has no request row and therefore no game — nobody has
told us which one it is. That copy asks the poster to put the game **in the
thread title**, which is deliberate: the forums are shared across games and a
per-game forum tag would burn one of Discord's five tag slots per channel, which
the status tags already need. A title is free, sorts in the channel list, and is
searchable.

The distinction matters more than it looks: a wrong game name is worse than no
game name, because it routes attention to the wrong people confidently.
"""


def _suffix(game_label: str | None) -> str:
    """Render a game name as a heading suffix, or nothing when we have no game.

    Parenthetical, not a second em dash: these headings already end in
    "— Triage Needed", and "New Scene Request — Gundam — Triage Needed" reads as
    two half-thoughts. "New Scene Request (Gundam) — Triage Needed" keeps the
    original sentence intact with the scope tucked inside it.
    """
    return f" ({game_label})" if game_label else ""


def _game_hint(game_labels: list[str] | None) -> str:
    """Parenthetical list of game names for neutral copy, or nothing.

    Returns the empty string when there are no labels — a cold cache or a failed
    games query — so the sentence degrades to "Which game this is for" rather than
    to a filler noun. Asking someone to pick from an unstated list is confusing;
    asking them which game is still a perfectly good question on its own.
    """
    labels = [g for g in (game_labels or []) if g]
    if not labels:
        return ""
    if len(labels) == 1:
        return f" ({labels[0]})"
    return f" ({', '.join(labels[:-1])} or {labels[-1]})"


# ---------------------------------------------------------------------------
# App-created thread messages (thread has a matching admin_requests record)
# ---------------------------------------------------------------------------

# No "scene_coordination" key anywhere in this file: the channel was retired
# after the web app's Phase 2 deploy (store_request / data_error flow through
# the admin UI and the daily #admin-digest) and its legacy threads drained.
#
# Templates take `{game}`, pre-rendered by `app_thread_message` — a heading
# suffix that is empty when the request somehow carries no game, so the message
# degrades to exactly its pre-multi-game wording instead of to "New Scene
# Request — None".
_APP_MESSAGES: dict[str, dict[str, str]] = {
    "scene_requests": {
        "scene_request": (
            "\U0001f30d **New Scene Request{game} — Triage Needed**\n"
            "\n"
            "Someone wants to bring DigiLab to a new area! Platform admins, please:\n"
            "1. Check if this area overlaps with an existing scene\n"
            "2. Determine if there's enough local activity to warrant a new scene\n"
            "3. If approved, create the scene and assign an admin\n"
            "4. React ✅ on the first message when this has been handled\n"
            "\n"
            "If you need more info, reach out to the requester in this thread "
            "— their Discord is listed above if provided."
        ),
    },
    "bug_reports": {
        "bug_report": (
            "\U0001f41b **Bug Report{game} — Triage Needed**\n"
            "\n"
            "A bug has been reported. Platform admins, please:\n"
            "1. Try to reproduce using the context above\n"
            "2. Prioritize and track in our issue tracker if confirmed\n"
            "3. React ✅ on the first message when this has been addressed\n"
            "\n"
            "If you need more details, ask the reporter in this thread."
        ),
    },
}


def app_thread_message(
    channel_type: str, request_type: str, game_label: str | None = None
) -> str | None:
    """The bot's instructions for an app-created thread, naming its game.

    `game_label` is a display name ("Gundam"), not a `game_id` ("gundam") — the
    id is a database key and reads as a typo in a sentence. Callers resolve it
    through the games cache, which falls back to the id rather than dropping the
    thread, so a game missing from the cache still says something true.

    Returns None if no message is defined for this channel_type + request_type.
    """
    channel_messages = _APP_MESSAGES.get(channel_type)
    if not channel_messages:
        return None
    template = channel_messages.get(request_type)
    if template is None:
        return None
    return template.format(game=_suffix(game_label))


# ---------------------------------------------------------------------------
# Manual thread messages (no admin_requests record — user posted directly)
# ---------------------------------------------------------------------------

# `{games}` is the list of games DigiLab currently covers, rendered by
# `_game_hint` (empty when we have no list). These threads have no request row and so no game at all: the
# person posting has not told us which one they mean, and several of these
# channels are genuinely cross-game. Asking them to say is the point.
_MANUAL_MESSAGES: dict[str, str] = {
    "scene_requests": (
        "\U0001f44b **Welcome to Scene Requests!**\n"
        "\n"
        "This channel is for requesting new scenes or communities on DigiLab.\n"
        "\n"
        "**Please put the game in your thread title{games}** — it's how admins "
        "find the threads that are theirs.\n"
        "\n"
        "**And in the post, please include:**\n"
        "• The city or region you'd like to add\n"
        "• Any stores or communities running events there\n"
        "• Your Discord handle so we can follow up\n"
        "\n"
        "A platform admin has been notified and will review your request here.\n"
        "\n"
        "**Looking to add a store to an existing scene?** Use the store request "
        "form on the DigiLab site instead — it goes straight to the admin queue, "
        "already tagged with the right game."
    ),
    "bug_reports": (
        "\U0001f44b **Thanks for reporting a bug!**\n"
        "\n"
        "**Please put the game in your thread title{games}** — it's how admins "
        "find the reports that are theirs.\n"
        "\n"
        "**And to help us track this down, please include:**\n"
        "• What you were doing when it happened\n"
        "• What you expected vs what actually happened\n"
        "• The page/tab you were on, and which scene (if applicable)\n"
        "\n"
        "**Tip:** The **Report a Bug** button in the app auto-fills context and "
        "creates a tracked request — it's the fastest way to get a fix, and it "
        "records which game you were looking at so the right people see it.\n"
        "\n"
        "A platform admin will triage this and follow up here."
    ),
    "feature_requests": (
        "\U0001f44b **Thanks for the feature idea!**\n"
        "\n"
        "**If this is for one game{games}, put it in your thread title** — if it's "
        "for all of them, say so in the post.\n"
        "\n"
        "**To help us evaluate your suggestion, consider including:**\n"
        "• What problem this would solve for you or your community\n"
        "• How you'd expect it to work\n"
        "• How important this is relative to other things you'd like to see\n"
        "\n"
        "Platform admins review feature requests regularly. "
        "Community discussion and upvotes (reactions) help us prioritize!"
    ),
}


def manual_thread_message(
    channel_type: str, game_labels: list[str] | None = None
) -> str | None:
    """The bot's welcome message for a manually created thread.

    Returns None if no message is defined for this channel_type.
    """
    template = _MANUAL_MESSAGES.get(channel_type)
    if template is None:
        return None
    return template.format(games=_game_hint(game_labels))
