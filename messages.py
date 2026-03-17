"""Datamon Bot — Message templates for forum thread responses."""

# ---------------------------------------------------------------------------
# App-created thread messages (thread has a matching admin_requests record)
# ---------------------------------------------------------------------------

_APP_MESSAGES: dict[str, dict[str, str]] = {
    "scene_coordination": {
        "store_request": (
            "\U0001f4cb **Store Request — Action Needed**\n"
            "\n"
            "A new store has been requested for this scene. Tagged admins, please:\n"
            "1. Verify the store exists and is running Digimon TCG events\n"
            "2. Check if this store is already listed under a different name\n"
            "3. React \u2705 on the first message when this has been handled\n"
            "\n"
            "If you need more info from the requester, reply in this thread."
        ),
        "data_error": (
            "\U0001f50d **Data Error — Review Needed**\n"
            "\n"
            "A data error has been reported for this scene. Tagged admins, please:\n"
            "1. Review the error details above\n"
            "2. Make the correction in the admin panel if confirmed\n"
            "3. React \u2705 on the first message when this has been fixed\n"
            "\n"
            "If you can't reproduce the issue, ask the reporter for more details in this thread."
        ),
    },
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
        "data_error": (
            "\U0001f50d **Data Error — Review Needed**\n"
            "\n"
            "A data error was reported but couldn't be routed to a specific scene. "
            "Platform admins, please:\n"
            "1. Identify which scene this belongs to\n"
            "2. Review and correct the error if confirmed\n"
            "3. React \u2705 on the first message when this has been fixed"
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
    "scene_coordination": (
        "\U0001f44b **Welcome to Scene Coordination!**\n"
        "\n"
        "This channel is for scene admins to discuss anything related to managing "
        "their scenes \u2014 data corrections, reorganizing scenes, general questions, "
        "or anything else.\n"
        "\n"
        "**Tips:**\n"
        "\u2022 Tag the relevant scene admins if you need their attention \u2014 "
        "check `/admins <scene>` to find them\n"
        "\u2022 For data errors, the fastest route is the **Report Error** button "
        "in the app \u2014 it creates a tracked request and notifies the right admins "
        "automatically\n"
        "\u2022 When your question or issue is resolved, react \u2705 on the first "
        "message to mark it done"
    ),
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
        "**Looking to add a store to an existing scene?** Head over to "
        "#scene-coordination instead."
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
