"""
Whether a Google Workspace call is permitted right now.

While the app's OAuth verification is pending, the Workspace surface is paused.
This module is the one place that decides it, so a new call site cannot forget:
``google_request`` — the single chokepoint every Google API call passes through —
asks here first, and its ``capability`` argument defaults to the *restrictive*
value. Adding a Google call without thinking about the pause therefore fails
closed rather than quietly reaching Google.

Failing closed means a clear, user-facing refusal. Not a stack trace, and not a
silent no-op: an automation that appears to succeed while doing nothing is worse
than one that says why it stopped.

Nothing here deletes or invalidates a stored token. The pause is about what the
running app may *do*; the grants stay intact so that flipping the flag back on
restores service without asking every employee to re-consent.
"""

from app.core.config import settings

# Capability names understood by `ensure_allowed`.
CAPABILITY_WORKSPACE = "workspace"
CAPABILITY_CALENDAR_CONFERENCE = "calendar_conference"
CAPABILITY_CALENDAR_SYNC = "calendar_sync"

WORKSPACE_PAUSED_MESSAGE = (
    "Google integrations are temporarily paused pending Google verification"
)

CALENDAR_CONFERENCE_PAUSED_MESSAGE = (
    "Google Meet link creation is temporarily unavailable pending Google "
    "verification. The event was still saved in SABAH.OS."
)

CALENDAR_SYNC_PAUSED_MESSAGE = (
    "Google Calendar synchronisation is temporarily switched off on this server."
)


class WorkspaceDisabledError(Exception):
    """
    A Google Workspace capability is switched off.

    Carries a message written for the person who triggered the action, so callers
    can surface it verbatim.
    """


def workspace_enabled() -> bool:
    """True when the broad Workspace read/write surface is available."""
    return bool(settings.GOOGLE_WORKSPACE_INTEGRATIONS_ENABLED)


def calendar_conference_enabled() -> bool:
    """
    True when minting a Google Meet link is available.

    Independent of the master switch on purpose: it is a narrow, user-initiated
    write to the organiser's own calendar under a sensitive (not restricted) scope,
    so it can stay on while the broad read surface is paused.
    """
    return bool(settings.GOOGLE_CALENDAR_CONFERENCE_ENABLED)


def calendar_sync_enabled() -> bool:
    """True when the hourly pull of a user's own calendar is available."""
    return bool(settings.GOOGLE_CALENDAR_SYNC_ENABLED)


def capability_enabled(capability: str) -> bool:
    if capability == CAPABILITY_CALENDAR_CONFERENCE:
        return calendar_conference_enabled()
    if capability == CAPABILITY_CALENDAR_SYNC:
        return calendar_sync_enabled()
    # Unknown capabilities are treated as part of the broad surface, so a typo
    # cannot accidentally grant an exemption.
    return workspace_enabled()


def ensure_allowed(capability: str = CAPABILITY_WORKSPACE) -> None:
    """Raise ``WorkspaceDisabledError`` unless *capability* is currently permitted."""
    if capability_enabled(capability):
        return
    message = {
        CAPABILITY_CALENDAR_CONFERENCE: CALENDAR_CONFERENCE_PAUSED_MESSAGE,
        CAPABILITY_CALENDAR_SYNC: CALENDAR_SYNC_PAUSED_MESSAGE,
    }.get(capability, WORKSPACE_PAUSED_MESSAGE)
    raise WorkspaceDisabledError(message)
