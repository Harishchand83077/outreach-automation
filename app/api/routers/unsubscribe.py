"""The public one-click unsubscribe endpoint (ADR-033, RFC 8058).

Mounted at the root, outside `/api/v1`, because a recipient must be able to act without signing
in and the URL goes in a header that receiving mail servers POST to automatically. It is the
only unauthenticated write in this system, and everything below is about keeping that narrow.

**POST acts, GET does not.** RFC 8058's one-click is a POST, and that split is a safety property
rather than pedantry: mail clients, security scanners and link-preview crawlers fetch URLs they
find in a message. If GET unsubscribed, a scanner opening the mail would opt the recipient out
of a campaign they had not read, and we would record it as their choice. So GET returns a page
with a button, and the button POSTs.

The token proves which lead of which workspace, so the route needs no `org_id` from the caller
and cannot be pointed at another workspace's data (ADR-004 holds here as everywhere).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status
from fastapi.responses import HTMLResponse

from app.api.deps import SessionDep
from app.core.settings import config
from app.db import repo
from app.db.models import Lead
from app.db.repo import OrgContext
from app.services import unsubscribe

logger = logging.getLogger(__name__)

router = APIRouter(tags=["unsubscribe"])

PATH = "/u/{token}"

#: Deliberately plain, deliberately tiny, and deliberately not branded. Somebody reading it is
#: leaving; the job of this page is to be unambiguous, not to persuade.
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unsubscribe</title>
<style>
 body{{font:16px/1.5 system-ui,sans-serif;margin:0;padding:2rem;color:#111;background:#fff}}
 main{{max-width:28rem;margin:4rem auto}}
 button{{font:inherit;padding:.6rem 1rem;border:1px solid #111;background:#111;color:#fff;
  border-radius:.4rem;cursor:pointer}}
 p{{margin:0 0 1rem}}
 @media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}
  button{{background:#eee;color:#111;border-color:#eee}}}}
</style></head>
<body><main>
<p>{message}</p>
{form}
</main></body></html>
"""

_CONFIRM_FORM = """<form method="post"><button type="submit">Unsubscribe me</button></form>"""


def _page(message: str, *, form: bool, code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        _PAGE.format(message=message, form=_CONFIRM_FORM if form else ""), status_code=code
    )


@router.get(PATH, response_class=HTMLResponse, include_in_schema=False)
async def confirm(token: str, session: SessionDep) -> HTMLResponse:
    """Show a confirmation. **Writes nothing** — see the module docstring."""
    resolved = unsubscribe.verify(token, secret=unsubscribe.signing_key(config.CREDENTIAL_KEY))
    if resolved is None:
        await session.rollback()
        return _page(
            "This unsubscribe link is not valid. It may have been altered by your mail client. "
            "Replying to the message with the word unsubscribe works just as well.",
            form=False,
            code=status.HTTP_404_NOT_FOUND,
        )
    return _page("Unsubscribe from these emails?", form=True)


@router.post(PATH, include_in_schema=False)
async def act(token: str, session: SessionDep) -> Response:
    """Suppress the address. This is what a receiving mail server POSTs to.

    **Answers 200 for an unknown token as well as a known one**, after doing nothing. A public
    endpoint that distinguishes the two is an oracle for which tokens exist; and from the
    recipient's side the outcome is identical either way — they will not be emailed again.
    """
    secret = unsubscribe.signing_key(config.CREDENTIAL_KEY)
    resolved = unsubscribe.verify(token, secret=secret)
    if resolved is None:
        await session.rollback()
        return Response(status_code=status.HTTP_200_OK)

    org_id, lead_id = resolved
    lead = await session.get(Lead, lead_id)
    if lead is None or lead.org_id != org_id:
        # The lead was erased, or the token names a pairing that never existed. Nothing to do,
        # and nothing to say about it.
        await session.rollback()
        return Response(status_code=status.HTTP_200_OK)

    await repo.add_suppression(
        OrgContext(org_id=org_id, actor_id="unsubscribe"),
        session,
        value=lead.email_norm,
        reason="one-click unsubscribe",
    )
    await session.commit()
    logger.info("one-click unsubscribe honoured", extra={"org_id": str(org_id)})
    return Response(status_code=status.HTTP_200_OK)
