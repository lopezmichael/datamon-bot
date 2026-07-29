"""Datamon Bot — Message templates for forum thread responses."""

# ---------------------------------------------------------------------------
# App-created thread messages (thread has a matching admin_requests record)
# ---------------------------------------------------------------------------

# No "scene_coordination" key anywhere in this file: the channel was retired
# after the web app's Phase 2 deploy (store_request / data_error flow through
# the admin UI and the daily #admin-digest) and its legacy threads drained.
_APP_MESSAGES: dict[str, dict[str, str]] = {
    "scene_requests": {
        "scene_request": (
            "\U0001f30d **New Scene Request — Triage Needed**\n"
            "\n"
            "Someone wants to bring DigiLab to a new area! Platform admins, please:\n"
            "1. Check if this area overlaps with an existing scene\n"
            "2. Determine if there's enough local activity to warrant a new scene\n"
            "3. If approved, create the scene and assign an admin\n"
            "4. React \u2705 on the first message when this has been handled\n"
            "\n"
            "If you need more info, reach out to the requester in this thread "
            "\u2014 their Discord is listed above if provided."
        ),
    },
    "bug_reports": {
        "bug_report": (
            "\U0001f41b **Bug Report — Triage Needed**\n"
            "\n"
            "A bug has been reported. Platform admins, please:\n"
            "1. Try to reproduce using the context above\n"
            "2. Prioritize and track in our issue tracker if confirmed\n"
            "3. React \u2705 on the first message when this has been addressed\n"
            "\n"
            "If you need more details, ask the reporter in this thread."
        ),
    },
}


def app_thread_message(channel_type: str, request_type: str) -> str | None:
    """Get the bot's instructions for an app-created thread.

    Returns None if no message is defined for this channel_type + request_type combo.
    """
    channel_messages = _APP_MESSAGES.get(channel_type)
    if not channel_messages:
        return None
    return channel_messages.get(request_type)


# ---------------------------------------------------------------------------
# Manual thread messages (no admin_requests record — user posted directly)
# ---------------------------------------------------------------------------

_MANUAL_MESSAGES: dict[str, str] = {
    "scene_requests": (
        "\U0001f44b **Welcome to Scene Requests!**\n"
        "\n"
        "This channel is for requesting new scenes or communities on DigiLab.\n"
        "\n"
        "**To help us process your request, please include:**\n"
        "\u2022 The city or region you'd like to add\n"
        "\u2022 Any stores or communities running Digimon TCG events there\n"
        "\u2022 Your Discord handle so we can follow up\n"
        "\n"
        "A platform admin has been notified and will review your request here.\n"
        "\n"
        "**Looking to add a store to an existing scene?** Use the store request "
        "form on the DigiLab site instead — it goes straight to the admin queue."
    ),
    "bug_reports": (
        "\U0001f44b **Thanks for reporting a bug!**\n"
        "\n"
        "To help us track this down, please make sure you've included:\n"
        "\u2022 What you were doing when it happened\n"
        "\u2022 What you expected vs what actually happened\n"
        "\u2022 The page/tab you were on and which scene (if applicable)\n"
        "\n"
        "**Tip:** The **Report a Bug** button in the app auto-fills context and "
        "creates a tracked request \u2014 it's the fastest way to get a fix.\n"
        "\n"
        "A platform admin will triage this and follow up here."
    ),
    "feature_requests": (
        "\U0001f44b **Thanks for the feature idea!**\n"
        "\n"
        "To help us evaluate your suggestion, consider including:\n"
        "\u2022 What problem this would solve for you or your community\n"
        "\u2022 How you'd expect it to work\n"
        "\u2022 How important this is relative to other things you'd like to see\n"
        "\n"
        "Platform admins review feature requests regularly. "
        "Community discussion and upvotes (reactions) help us prioritize!"
    ),
}


def manual_thread_message(channel_type: str) -> str | None:
    """Get the bot's welcome message for a manually created thread.

    Returns None if no message is defined for this channel_type.
    """
    return _MANUAL_MESSAGES.get(channel_type)
