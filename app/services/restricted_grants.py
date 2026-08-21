"""
Clearing grants that still carry restricted Google scopes.

Reducing what the app *requests* does not revoke what a user already *granted*.
Roughly a hundred accounts consented under the pre-reduction scope set and their
grants still include ``drive``, ``gmail.readonly`` and ``gmail.compose``. Google's
OAuth reviewers inspect live grants on the project, not just the authorization
request, so a lingering restricted grant can fail verification on its own — and
each restricted scope, if it counts, drags the app back into the mandatory annual
paid CASA assessment.

The only way to clear one is to revoke the grant at Google. The user then
reconnects, and because ``GOOGLE_OAUTH_SCOPE_TIER`` is ``login_only`` the new grant
carries identity scopes only.

**Nothing here runs on its own.** There is no Celery schedule and no startup hook;
the entry point is ``scripts/reconsent_restricted_grants.py``, which reports by
default and acts only under an explicit ``--apply``. Revoking is destructive from
the user's side — every connected automation stops until they reconnect — so the
decision to run it stays with an operator.

Honesty about outcomes matters more here than tidiness. ``revoke_credential`` in
``google_client`` swallows a failed call to Google and marks the row revoked
anyway, which is right for local state but would let this job report "cleared"
while Google still lists the restricted grant — precisely the thing a reviewer
looks at. So this module does its own revoke and separates the two cases:
``REVOKED`` means Google confirmed it, ``LOCAL_ONLY`` means the operator still has
manual work to do.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.google_client import GOOGLE_REVOKE_URL
from app.core.google_scopes import granted_restricted_scopes
from app.core.security import DecryptionError, decrypt_token
from app.models.credential import GoogleCredential

logger = logging.getLogger(__name__)

# Outcome states. Only REVOKED means the grant is actually gone at Google.
FOUND = "found"  # dry run: identified, nothing done
REVOKED = "revoked"  # Google confirmed the grant is gone
ALREADY_GONE = "already_gone"  # Google rejected the token as invalid — same effect
LOCAL_ONLY = "local_only"  # row marked revoked, but Google still holds the grant

#: Outcomes that leave the grant live on Google's side and need an operator to
#: finish the job by hand.
NEEDS_FOLLOWUP = frozenset({LOCAL_ONLY})


@dataclass
class GrantOutcome:
    """What was found on one credential, and what happened to it."""

    credential_id: int
    email: str
    sabah_user_id: Optional[str]
    restricted_scopes: List[str]
    status: str
    detail: str = ""


@dataclass
class ReconsentReport:
    scanned: int = 0
    applied: bool = False
    outcomes: List[GrantOutcome] = field(default_factory=list)

    @property
    def affected(self) -> int:
        return len(self.outcomes)

    @property
    def needs_manual_followup(self) -> List[GrantOutcome]:
        """Grants Google may still consider live despite the local row being revoked."""
        return [o for o in self.outcomes if o.status in NEEDS_FOLLOWUP]

    @property
    def cleared(self) -> List[GrantOutcome]:
        return [o for o in self.outcomes if o.status in (REVOKED, ALREADY_GONE)]


async def find_restricted_grants(session: AsyncSession) -> List[GoogleCredential]:
    """
    Every live credential whose stored scope list still contains a restricted scope.

    Already-revoked rows are skipped: the grant behind them is gone (or was marked
    unusable), so re-revoking would only add noise to the report.
    """
    result = await session.execute(
        select(GoogleCredential).where(GoogleCredential.revoked.is_(False))
    )
    return [
        cred
        for cred in result.scalars().all()
        if granted_restricted_scopes(cred.scopes or [])
    ]


async def _revoke_at_google(refresh_token: str) -> tuple[bool, str]:
    """
    Ask Google to drop the grant.

    Returns ``(gone, detail)``. A 400 with ``invalid_token`` means the token is
    already dead, which is the outcome we wanted, so it counts as success — the
    endpoint is not otherwise idempotent-looking and treating it as a failure would
    strand accounts in the follow-up list forever.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            GOOGLE_REVOKE_URL,
            data={"token": refresh_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code == 200:
        return True, ""
    if response.status_code == 400 and "invalid_token" in response.text:
        return True, "token already invalid at Google"
    return False, f"HTTP {response.status_code}: {response.text[:200]}"


async def clear_restricted_grants(
    session: AsyncSession,
    *,
    apply: bool = False,
    limit: Optional[int] = None,
) -> ReconsentReport:
    """
    Report — and with ``apply=True``, revoke — every grant still carrying a
    restricted scope.

    Commits after each credential rather than once at the end: a batch of a hundred
    network calls will not always finish, and a partial run whose progress survives
    is far easier to resume than one that rolls back everything on the last error.
    """
    candidates = await find_restricted_grants(session)
    report = ReconsentReport(scanned=len(candidates), applied=apply)

    for cred in candidates[:limit] if limit is not None else candidates:
        restricted = granted_restricted_scopes(cred.scopes or [])

        if not apply:
            report.outcomes.append(
                GrantOutcome(
                    credential_id=cred.id,
                    email=cred.google_account_email,
                    sabah_user_id=cred.sabah_user_id,
                    restricted_scopes=restricted,
                    status=FOUND,
                )
            )
            continue

        status, detail = REVOKED, ""
        try:
            gone, detail = await _revoke_at_google(decrypt_token(cred.refresh_token))
            if gone:
                status = ALREADY_GONE if detail else REVOKED
            else:
                status = LOCAL_ONLY
        except DecryptionError:
            # The stored ciphertext no longer opens under the current FERNET_KEY, so
            # there is no token to hand Google. The row is still marked revoked —
            # an unreadable credential is unusable either way — but the grant itself
            # has to be cleared by the user or a Workspace admin.
            status, detail = LOCAL_ONLY, "refresh token could not be decrypted"
        except httpx.HTTPError as exc:
            status, detail = LOCAL_ONLY, f"revoke call failed: {exc}"

        cred.revoked = True
        await session.commit()

        if status in NEEDS_FOLLOWUP:
            logger.warning(
                "Restricted grant for %s marked revoked locally but may still be "
                "live at Google: %s",
                cred.google_account_email,
                detail,
            )

        report.outcomes.append(
            GrantOutcome(
                credential_id=cred.id,
                email=cred.google_account_email,
                sabah_user_id=cred.sabah_user_id,
                restricted_scopes=restricted,
                status=status,
                detail=detail,
            )
        )

    return report
