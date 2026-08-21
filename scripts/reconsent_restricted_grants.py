#!/usr/bin/env python
"""
Clear Google grants that still carry restricted scopes.

Why this exists: the app stopped *requesting* ``drive``, ``gmail.readonly`` and
``gmail.compose``, but roughly a hundred accounts consented before that change and
their grants still carry them. Google's OAuth reviewers look at live grants, so
those have to go before submitting for verification. See
``app/services/restricted_grants.py`` for the full reasoning.

Reports by default and changes nothing:

    python scripts/reconsent_restricted_grants.py

Revoke for real — every affected user must reconnect Google afterwards, and any
automation of theirs stops until they do:

    python scripts/reconsent_restricted_grants.py --apply

Add ``--limit 5`` to try a handful first, and ``--yes`` to skip the confirmation
prompt in a non-interactive shell.

Exit codes: 0 clean, 1 grants remain live at Google and need manual follow-up,
2 aborted at the prompt.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.db import async_session_factory  # noqa: E402
from app.core.google_scopes import RESTRICTED_SCOPES  # noqa: E402
from app.services.restricted_grants import (  # noqa: E402
    FOUND,
    clear_restricted_grants,
)


def short(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually revoke; without it nothing is changed",
    )
    parser.add_argument("--limit", type=int, default=None, help="process at most N grants")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()

    async with async_session_factory() as session:
        if args.apply and not args.yes:
            preview = await clear_restricted_grants(session, apply=False, limit=args.limit)
            if not preview.affected:
                print("No grant carries a restricted scope. Nothing to do.")
                return 0
            print(
                f"About to revoke {preview.affected} Google grant(s). Each affected "
                "user must reconnect Google, and their automations stop until they do."
            )
            if input("Type 'revoke' to continue: ").strip() != "revoke":
                print("Aborted — nothing was changed.")
                return 2

        report = await clear_restricted_grants(
            session, apply=args.apply, limit=args.limit
        )

    print(f"Restricted scopes tracked: {', '.join(sorted(short(s) for s in RESTRICTED_SCOPES))}")
    print(f"Live credentials carrying one: {report.scanned}")

    if not report.affected:
        print("\nNothing to do — no grant carries a restricted scope.")
        return 0

    print()
    for outcome in report.outcomes:
        scopes = ", ".join(short(s) for s in outcome.restricted_scopes)
        detail = f"  ({outcome.detail})" if outcome.detail else ""
        print(f"  [{outcome.status:>12}] {outcome.email:<40} {scopes}{detail}")

    if report.outcomes and report.outcomes[0].status == FOUND:
        print(
            f"\nDry run — nothing was changed. Re-run with --apply to revoke "
            f"{report.affected} grant(s)."
        )
        return 0

    print(f"\nCleared at Google: {len(report.cleared)}")

    followup = report.needs_manual_followup
    if followup:
        print(
            f"Still live at Google despite the local row being revoked: {len(followup)}.\n"
            "These must be removed by the user at myaccount.google.com/permissions, "
            "or by a Workspace admin:"
        )
        for outcome in followup:
            print(f"  - {outcome.email}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
