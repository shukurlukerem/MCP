"""
Google Workspace service catalogue — the single source of truth for OAuth scopes.

**No scope string may be hardcoded anywhere else.** Django and the public landing
page both used to keep their own copies, which drifted; the authorization request,
the consent-screen disclosure and the Google Cloud Console list must agree exactly
or verification fails on the mismatch alone.

What gets requested is chosen by ``GOOGLE_OAUTH_SCOPE_TIER``:

* ``login_only`` (default) — identity only. Non-sensitive, so Google shows no
  "unverified app" screen and the request does not consume the project's 100-user
  cap. This is what keeps sign-in working while verification is pending.
* ``full`` — identity plus every Workspace scope below. Sensitive scopes, so this
  is only safe to enable once Google has approved the app.

Every scope here is non-sensitive or sensitive. **No restricted scope is
requested**, which is what removes the mandatory annual paid CASA security
assessment. ``RESTRICTED_SCOPES`` records the three that were dropped so the
re-consent job can spot grants that still carry them, and so the guard at the
bottom of this module fails the process if one is ever reintroduced.

Each service entry describes:

* ``scopes``    — OAuth scopes requested for it
* ``api_base``  — REST root used by the direct data-pull API (``app/api/google.py``)
* ``mcp_url``   — remote MCP server endpoint, if Google publishes one
* ``optional``  — not requested unless explicitly enabled (admin-only scopes that
                  a regular employee cannot grant)
"""

from typing import Dict, List

# Identity — always requested, in either tier. All three are non-sensitive.
LOGIN_SCOPES: List[str] = [
    "openid",
    "email",
    "profile",
]

SCOPE_TIER_LOGIN_ONLY = "login_only"
SCOPE_TIER_CALENDAR = "calendar"
SCOPE_TIER_FULL = "full"
SCOPE_TIERS = (SCOPE_TIER_LOGIN_ONLY, SCOPE_TIER_CALENDAR, SCOPE_TIER_FULL)

# Restricted scopes, deliberately removed and never to return. Each one obliges an
# annual paid third-party CASA assessment, and none had a demonstrable feature
# behind it: the only consumers were a generic read surface exposed to the LLM's
# discretion, which is not something Google's per-scope review can accept.
#
# `drive`          → replaced by `drive.file` (files the app created or the user
#                    explicitly picked via Google Picker)
# `gmail.readonly` → removed outright, no mailbox reading
# `gmail.compose`  → replaced by `gmail.send` + `gmail.labels`
RESTRICTED_SCOPES = frozenset(
    {
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    }
)


class Service:
    """A single Google service the platform can read."""

    __slots__ = ("key", "label", "scopes", "api_base", "mcp_url", "optional")

    def __init__(
        self,
        key: str,
        label: str,
        scopes: List[str],
        api_base: str = "",
        mcp_url: str = "",
        optional: bool = False,
    ) -> None:
        self.key = key
        self.label = label
        self.scopes = scopes
        self.api_base = api_base
        self.mcp_url = mcp_url
        self.optional = optional


SERVICES: Dict[str, Service] = {
    "gmail": Service(
        key="gmail",
        label="Gmail",
        # Send-only. `gmail.readonly` and `gmail.compose` are restricted and gone;
        # `gmail.settings.basic` is dropped too because nothing reads or writes
        # mail settings. Note that users.drafts.create is NOT available under
        # gmail.send — approval happens in-app, then we send directly.
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.labels",
        ],
        api_base="https://gmail.googleapis.com/gmail/v1",
        mcp_url="https://gmail.googleapis.com/mcp",
    ),
    "drive": Service(
        key="drive",
        label="Google Drive",
        # `drive.file` (non-sensitive) covers files this app created plus files the
        # user explicitly picked in Google Picker — the user chooses the blast
        # radius, which also removes the prompt-injection path that full `drive`
        # opened up. `drive.metadata.readonly` keeps listing/browsing working
        # without granting content access.
        scopes=[
            "https://www.googleapis.com/auth/drive.file",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ],
        api_base="https://www.googleapis.com/drive/v3",
        mcp_url="https://www.googleapis.com/drive/v3/mcp",
    ),
    "calendar": Service(
        key="calendar",
        label="Google Calendar",
        # Events only. Full `calendar` additionally grants creating, sharing and
        # permanently deleting whole calendars plus their ACLs, and nothing here
        # does any of that: every call this app makes is under
        # `/calendars/{id}/events`. The one exception was an uncalled
        # `calendarList` helper, which is not worth the wider grant. Dropping it
        # shrinks the consent text from "permanently delete all the calendars you
        # can access" to editing events, which is both truthful and a smaller
        # surface for review.
        scopes=[
            "https://www.googleapis.com/auth/calendar.events",
        ],
        api_base="https://www.googleapis.com/calendar/v3",
        mcp_url="https://www.googleapis.com/calendar/v3/mcp",
    ),
    "docs": Service(
        key="docs",
        label="Google Docs",
        scopes=["https://www.googleapis.com/auth/documents"],
        api_base="https://docs.googleapis.com/v1",
    ),
    "sheets": Service(
        key="sheets",
        label="Google Sheets",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
        api_base="https://sheets.googleapis.com/v4",
    ),
    "slides": Service(
        key="slides",
        label="Google Slides",
        scopes=["https://www.googleapis.com/auth/presentations"],
        api_base="https://slides.googleapis.com/v1",
    ),
    "contacts": Service(
        key="contacts",
        label="Contacts & Directory",
        scopes=[
            "https://www.googleapis.com/auth/contacts.readonly",
            "https://www.googleapis.com/auth/directory.readonly",
        ],
        api_base="https://people.googleapis.com/v1",
    ),
    "tasks": Service(
        key="tasks",
        label="Google Tasks",
        scopes=["https://www.googleapis.com/auth/tasks"],
        api_base="https://tasks.googleapis.com/tasks/v1",
    ),
    "forms": Service(
        key="forms",
        label="Google Forms",
        scopes=[
            "https://www.googleapis.com/auth/forms.body.readonly",
            "https://www.googleapis.com/auth/forms.responses.readonly",
        ],
        api_base="https://forms.googleapis.com/v1",
    ),
    "chat": Service(
        key="chat",
        label="Google Chat",
        scopes=[
            "https://www.googleapis.com/auth/chat.spaces.readonly",
            "https://www.googleapis.com/auth/chat.messages.readonly",
        ],
        api_base="https://chat.googleapis.com/v1",
    ),
    "meet": Service(
        key="meet",
        label="Google Meet",
        scopes=["https://www.googleapis.com/auth/meetings.space.readonly"],
        api_base="https://meet.googleapis.com/v2",
    ),
    # ── Admin-only. Requesting these for a non-admin employee produces a
    # consent screen they cannot satisfy, so they stay off unless enabled.
    "admin_directory": Service(
        key="admin_directory",
        label="Admin Directory",
        scopes=[
            "https://www.googleapis.com/auth/admin.directory.user.readonly",
            "https://www.googleapis.com/auth/admin.directory.group.readonly",
        ],
        api_base="https://admin.googleapis.com/admin/directory/v1",
        optional=True,
    ),
    "admin_reports": Service(
        key="admin_reports",
        label="Admin Reports (audit)",
        scopes=["https://www.googleapis.com/auth/admin.reports.audit.readonly"],
        api_base="https://admin.googleapis.com/admin/reports/v1",
        optional=True,
    ),
}

# Services requested by default: everything an ordinary employee can grant.
DEFAULT_SERVICES: List[str] = [k for k, s in SERVICES.items() if not s.optional]

# Hosts the generic Google passthrough proxy may call.
ALLOWED_GOOGLE_HOSTS = frozenset(
    {
        "www.googleapis.com",
        "gmail.googleapis.com",
        "docs.googleapis.com",
        "sheets.googleapis.com",
        "slides.googleapis.com",
        "people.googleapis.com",
        "tasks.googleapis.com",
        "forms.googleapis.com",
        "chat.googleapis.com",
        "meet.googleapis.com",
        "admin.googleapis.com",
        "calendar.googleapis.com",
        "drive.googleapis.com",
        "oauth2.googleapis.com",
    }
)


def scopes_for(service_keys: List[str]) -> List[str]:
    """Return the deduplicated scope list for the given services, plus identity."""
    seen: List[str] = list(LOGIN_SCOPES)
    for key in service_keys:
        service = SERVICES.get(key)
        if not service:
            continue
        for scope in service.scopes:
            if scope not in seen:
                seen.append(scope)
    return seen


# Every Workspace scope an ordinary employee can grant — the `full` tier, and the
# exact list to paste into Google Cloud Console → Data Access.
WORKSPACE_SCOPES: List[str] = scopes_for(DEFAULT_SERVICES)


# Identity plus Calendar, and nothing else — the tier that turns the calendar page
# on without asking for Gmail, Drive or anything else the feature does not read.
# `calendar.events` is requested rather than `calendar.readonly`, because minting a
# Google Meet link is a write and is already a shipped feature; a read-only grant
# would quietly break the Meet button for everyone who consents
# after the change. Calendar scopes are sensitive, so this tier still shows the
# unverified-app screen until Google approves the app — but it is one scope family
# instead of all of them, and no restricted scope, so still no CASA assessment.
CALENDAR_TIER_SCOPES: List[str] = scopes_for(["calendar"])


def scopes_for_tier(tier: str) -> List[str]:
    """
    Resolve a scope tier to the list to put in the authorization request.

    An unrecognised tier falls back to identity-only rather than raising: failing
    open here would mean silently asking for sensitive scopes on a typo, which is
    precisely the mistake that put this app behind the unverified-app screen.
    """
    if tier == SCOPE_TIER_FULL:
        return list(WORKSPACE_SCOPES)
    if tier == SCOPE_TIER_CALENDAR:
        return list(CALENDAR_TIER_SCOPES)
    return list(LOGIN_SCOPES)


def granted_restricted_scopes(granted: List[str]) -> List[str]:
    """
    Restricted scopes a stored grant still carries.

    Reducing what we *request* does not revoke what was *granted*. Google's
    reviewers look at live grants, so the re-consent job uses this to find tokens
    that must be re-authorized with the minimal set.
    """
    return sorted(RESTRICTED_SCOPES.intersection(granted or []))


# Fail at import if a restricted scope ever creeps back into the catalogue. A test
# would catch it too, but this also stops a running deployment from quietly asking
# for CASA-triggering access.
_leaked = RESTRICTED_SCOPES.intersection(WORKSPACE_SCOPES)
if _leaked:
    raise RuntimeError(
        f"Restricted Google scopes must never be requested: {sorted(_leaked)}. "
        "Each one obliges an annual paid CASA security assessment."
    )


# Scopes this app no longer requests but which older grants still carry. Used only
# when reading a stored grant back — never when building a request. Without it, an
# employee who consented before the scope reduction would reverse-map to *fewer*
# services than they actually granted, and the platform would report their Drive or
# Gmail access as absent while Google still considers it live.
LEGACY_SERVICE_SCOPES: Dict[str, str] = {
    "https://www.googleapis.com/auth/drive": "drive",
    # Dropped in favour of `calendar.events`; grants made before that still carry
    # it, and they can still do everything the calendar feature needs.
    "https://www.googleapis.com/auth/calendar": "calendar",
    "https://www.googleapis.com/auth/gmail.readonly": "gmail",
    "https://www.googleapis.com/auth/gmail.compose": "gmail",
    "https://www.googleapis.com/auth/gmail.settings.basic": "gmail",
}


def services_from_scopes(granted: List[str]) -> List[str]:
    """
    Reverse-map granted scopes to the service keys they unlock.

    Recognises legacy scopes as well as current ones, so this stays truthful about
    what an existing grant can reach.
    """
    granted_set = set(granted or [])
    legacy = {
        key for scope, key in LEGACY_SERVICE_SCOPES.items() if scope in granted_set
    }
    return [
        key
        for key, service in SERVICES.items()
        if key in legacy or any(scope in granted_set for scope in service.scopes)
    ]


def describe_services(service_keys: List[str]) -> List[dict]:
    """Serialisable description of services, for the /google/services endpoint."""
    out = []
    for key in service_keys:
        service = SERVICES.get(key)
        if not service:
            continue
        out.append(
            {
                "key": service.key,
                "label": service.label,
                "scopes": service.scopes,
                "api_base": service.api_base,
                "mcp_url": service.mcp_url or None,
            }
        )
    return out
