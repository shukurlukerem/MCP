"""
Google Workspace service catalogue.

This deployment is **corporate and internal** — every user is an employee of the
company, signing in with a company Google account. The consent screen therefore
asks for the full set of Workspace scopes at once, so a user connects a single
time and the platform can read every service they own.

Each entry describes one service:

* ``scopes``    — OAuth scopes requested for it
* ``api_base``  — REST root used by the direct data-pull API (``app/api/google.py``)
* ``mcp_url``   — remote MCP server endpoint, if Google publishes one
* ``optional``  — not requested unless explicitly enabled (admin-only scopes that
                  a regular employee cannot grant)
"""

from typing import Dict, List

# Identity — always requested.
CORE_SCOPES: List[str] = [
    "openid",
    "email",
    "profile",
]


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
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.labels",
            "https://www.googleapis.com/auth/gmail.settings.basic",
        ],
        api_base="https://gmail.googleapis.com/gmail/v1",
        mcp_url="https://gmail.googleapis.com/mcp",
    ),
    "drive": Service(
        key="drive",
        label="Google Drive",
        # Full drive scope — the corporate use case is "read everything the
        # employee can see", which drive.file (per-file consent) cannot do.
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        ],
        api_base="https://www.googleapis.com/drive/v3",
        mcp_url="https://www.googleapis.com/drive/v3/mcp",
    ),
    "calendar": Service(
        key="calendar",
        label="Google Calendar",
        scopes=[
            "https://www.googleapis.com/auth/calendar",
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
    seen: List[str] = list(CORE_SCOPES)
    for key in service_keys:
        service = SERVICES.get(key)
        if not service:
            continue
        for scope in service.scopes:
            if scope not in seen:
                seen.append(scope)
    return seen


def services_from_scopes(granted: List[str]) -> List[str]:
    """Reverse-map granted scopes to the service keys they unlock."""
    granted_set = set(granted or [])
    return [
        key
        for key, service in SERVICES.items()
        if any(scope in granted_set for scope in service.scopes)
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
