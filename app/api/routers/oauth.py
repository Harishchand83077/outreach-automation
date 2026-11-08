"""The OAuth 2.1 endpoints (F14, ADR-058).

Three kinds of route, and they are on three different doors on purpose:

- **Discovery** is public and unauthenticated, because a client has to read it *before* it has
  any credential. Mounted at the root, because RFC 9728 and RFC 8414 specify the paths exactly
  and a client will not look anywhere else.
- **`/oauth/token` is public**, because the caller is a program holding a code rather than a
  person holding a session. PKCE is what proves it is the right program.
- **`/api/v1/oauth/authorize` is authenticated with the dashboard's own Supabase session**,
  because the thing being decided is "does this person grant this". The consent *screen* is a
  page in the dashboard; this is the call that page makes when somebody presses Allow.

**The authorize endpoint is a POST and never a GET.** The browser arrives at the dashboard's
`/oauth/authorize` page by redirect, and that page is a GET that writes nothing. Issuing a code
from a GET would mean any page that could make the browser follow a link could obtain one.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import CurrentUser, CurrentUserDep, SessionDep, SettingsDep, require_role
from app.db.models import API_TOKEN_SCOPES, OAuthClient
from app.db.state import record_event
from app.domain import oauth
from app.services.oauth import server

router = APIRouter(tags=["oauth"])
#: Discovery lives at the root: the paths are fixed by RFC and a client will not look under a
#: prefix for them.
discovery = APIRouter(tags=["oauth"])

#: The scopes a connector may ask for. The extension's door is excluded -- it is a different
#: door (ADR-039), and a scope that opens it has no business being obtainable here.
GRANTABLE: tuple[str, ...] = tuple(name for name in API_TOKEN_SCOPES if name.startswith("mcp:"))


def _issuer(settings: Any) -> str:
    return (getattr(settings, "PUBLIC_API_URL", "") or "").rstrip("/")


def _error(exc: oauth.OAuthError) -> JSONResponse:
    """The spec's error shape. `error` is coarse by design; see `domain/oauth.py`."""
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.error, "error_description": exc.description},
    )


# --------------------------------------------------------------------------- discovery


@discovery.get("/.well-known/oauth-protected-resource")
async def protected_resource(settings: SettingsDep) -> dict[str, Any]:
    """RFC 9728. **Required** by the 2026-07-28 MCP revision.

    It is how a client that has been refused with a 401 finds out who can authorise it. Without
    this document the only route into the connector is a token pasted by hand, which is what
    this slice exists to stop being the answer.
    """
    issuer = _issuer(settings)
    return {
        "resource": issuer,
        "authorization_servers": [issuer],
        "scopes_supported": list(GRANTABLE),
        "bearer_methods_supported": ["header"],
    }


@discovery.get("/.well-known/oauth-authorization-server")
async def authorization_server(settings: SettingsDep) -> dict[str, Any]:
    """RFC 8414.

    `code_challenge_methods_supported` is `["S256"]` and not `["S256", "plain"]`: advertising
    `plain` would invite a client to use it, and a challenge equal to its verifier protects
    against nothing.

    There is no `registration_endpoint`, because Dynamic Client Registration is deprecated in
    the 2026-07-28 revision and was always the wrong shape here -- it would let anything that
    can reach this server become a client, and what a client asks for is somebody's workspace.
    """
    issuer = _issuer(settings)
    dashboard = (getattr(settings, "DASHBOARD_URL", "") or "").rstrip("/")
    return {
        "issuer": issuer,
        # The *dashboard's* page, not an API route: the person arrives in a browser and has to
        # see what they are granting.
        "authorization_endpoint": f"{dashboard}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": list(oauth.GRANT_TYPES),
        "code_challenge_methods_supported": [oauth.CHALLENGE_METHOD],
        "scopes_supported": list(GRANTABLE),
        "token_endpoint_auth_methods_supported": ["none"],
    }


# --------------------------------------------------------------------------- consent


@router.get("/oauth/consent")
async def describe_consent(
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    client_id: str = "",
    scope: str = "",
    redirect_uri: str = "",
    resource: str = "",
) -> dict[str, Any]:
    """What the consent screen should say. Reads only; grants nothing.

    Every parameter is validated here, before anything is drawn, so a request that could never
    succeed is refused on the screen rather than after somebody presses Allow. **The client's
    name comes from our own row**, never from the request -- a name supplied by the caller is a
    name an attacker chooses, and it is the one string a person uses to decide.
    """
    try:
        client = await server.client_for(session, client_id, org_id=user.org_id)
        oauth.check_redirect_uri(redirect_uri, list(client.redirect_uris or []))
        oauth.check_resource(resource or None, _issuer(settings))
        scopes = oauth.check_scopes(scope, GRANTABLE)
    except oauth.OAuthError as exc:
        return {"ok": False, "error": exc.error, "detail": exc.description}

    return {
        "ok": True,
        "client": {"name": client.name, "client_id": client.client_id},
        "scopes": [{"name": name, "describes": _describe(name)} for name in scopes],
        "workspace": str(user.org_id),
        "note": (
            "Granting this lets that application act as you in this workspace, with exactly "
            "the permissions listed. It can be revoked at any time in Settings, and every call "
            "it makes re-checks that you are still a member here."
        ),
    }


def _describe(scope: str) -> str:
    """One sentence per scope, in the words the tick-boxes in Settings already use."""
    return {
        "mcp:read": "Read this workspace's campaigns, leads, replies, postings and signals.",
        "mcp:profile": (
            "Read your confirmed career facts, and which screening questions you have "
            "answered -- never the answers themselves."
        ),
        "mcp:notes": "Record a note about a company, with the page that says it.",
        "mcp:propose": ("Write a draft into your review queue. It cannot send one or approve one."),
    }.get(scope, scope)


@router.post("/oauth/authorize")
async def authorize(
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    client_id: Annotated[str, Body(embed=True)],
    redirect_uri: Annotated[str, Body(embed=True)],
    code_challenge: Annotated[str, Body(embed=True)],
    scope: Annotated[str, Body(embed=True)] = "",
    resource: Annotated[str, Body(embed=True)] = "",
    state: Annotated[str, Body(embed=True)] = "",
    code_challenge_method: Annotated[str, Body(embed=True)] = oauth.CHALLENGE_METHOD,
) -> Any:
    """Somebody pressed Allow. Mint a code and say where to send the browser."""
    try:
        code, redirect = await server.authorize(
            session,
            client_id=client_id,
            user_id=user.user_id,
            org_id=user.org_id,
            redirect_uri=redirect_uri,
            scope=scope,
            resource=resource or None,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expected_resource=_issuer(settings),
            allowed_scopes=GRANTABLE,
        )
    except oauth.OAuthError as exc:
        await session.rollback()
        return _error(exc)

    await record_event(
        session,
        org_id=user.org_id,
        type="oauth.authorized",
        actor="user",
        actor_id=user.email or user.user_id,
        # The scopes and the client, never the code: this row is read by whoever is asking what
        # was granted, and a code in an audit trail is a code in a log.
        data={"client_id": client_id, "scopes": scope.split()},
    )
    await session.commit()

    separator = "&" if "?" in redirect else "?"
    location = f"{redirect}{separator}code={code}"
    if state:
        location += f"&state={state}"
    return {"redirect_to": location}


# --------------------------------------------------------------------------- the token endpoint


@discovery.post("/oauth/token")
async def token(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    grant_type: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
    redirect_uri: Annotated[str, Form()] = "",
    client_id: Annotated[str, Form()] = "",
    code_verifier: Annotated[str, Form()] = "",
    refresh_token: Annotated[str, Form()] = "",
    resource: Annotated[str, Form()] = "",
) -> Any:
    """Exchange a code, or rotate a refresh token.

    `application/x-www-form-urlencoded`, because that is what the spec says and what every
    client sends. Public: there is no client secret to present, and PKCE is what proves the
    caller is the program that asked for the code.
    """
    try:
        if grant_type == "authorization_code":
            issued = await server.exchange_code(
                session,
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_verifier=code_verifier,
                resource=resource or None,
                expected_resource=_issuer(settings),
            )
        elif grant_type == "refresh_token":
            issued = await server.refresh(
                session,
                refresh_token=refresh_token,
                client_id=client_id,
                expected_resource=_issuer(settings),
            )
        else:
            raise oauth.OAuthError(
                "unsupported_grant_type",
                f"This server supports {' and '.join(oauth.GRANT_TYPES)}.",
            )
    except oauth.OAuthError as exc:
        await session.rollback()
        return _error(exc)

    await session.commit()
    return JSONResponse(
        # The spec requires both, and they matter: a client that cached a token would present a
        # revoked one for an hour.
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        content={
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            "refresh_token": issued.refresh_token,
            "scope": " ".join(issued.scopes),
        },
    )


# --------------------------------------------------------------------------- clients


@router.get("/oauth/clients")
async def list_clients(user: CurrentUserDep, session: SessionDep) -> dict[str, Any]:
    """The clients this deployment will talk to. No secrets, because there are none."""
    from sqlalchemy import select

    rows = (
        await session.scalars(
            select(OAuthClient)
            .where(OAuthClient.org_id == user.org_id)
            .order_by(OAuthClient.created_at)
        )
    ).all()
    return {
        "clients": [
            {
                "id": str(row.id),
                "client_id": row.client_id,
                "name": row.name,
                "redirect_uris": list(row.redirect_uris or []),
                "disabled": row.disabled_at is not None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
        "note": (
            "Clients are added by hand. Dynamic registration is deprecated in the current MCP "
            "revision and would let anything that can reach this server ask your people for "
            "access to their workspace."
        ),
    }


@router.post("/oauth/clients", status_code=status.HTTP_201_CREATED)
async def add_client(
    user: Annotated[CurrentUser, Depends(require_role("owner"))],
    session: SessionDep,
    client_id: Annotated[str, Body(embed=True)],
    name: Annotated[str, Body(embed=True)],
    redirect_uris: Annotated[list[str], Body(embed=True)],
) -> dict[str, Any]:
    """Register a client. **Owner only**, and that is not a formality.

    A client is the thing a consent screen names, and the redirect URIs are where codes go.
    Whoever can add one can put their own address on a screen that says "allow this".
    """
    cleaned = [uri.strip() for uri in redirect_uris if uri.strip()]
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one redirect URI is required, and it is matched exactly.",
        )
    for uri in cleaned:
        if not uri.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"{uri} is not https. A code delivered over http is a code read in "
                    "transit; only localhost is exempt, because it does not leave the machine."
                ),
            )

    row = OAuthClient(
        org_id=user.org_id,
        client_id=client_id.strip(),
        name=name.strip(),
        redirect_uris=cleaned,
        created_by=user.email or user.user_id,
    )
    session.add(row)
    await record_event(
        session,
        org_id=user.org_id,
        type="oauth.client_added",
        actor="user",
        actor_id=user.email or user.user_id,
        data={"client_id": client_id, "redirect_uris": cleaned},
    )
    await session.commit()
    return {"id": str(row.id), "client_id": row.client_id, "name": row.name}


@router.delete("/oauth/clients/{row_id}")
async def disable_client(
    row_id: uuid.UUID,
    user: Annotated[CurrentUser, Depends(require_role("owner"))],
    session: SessionDep,
) -> dict[str, Any]:
    """Stop a client being able to ask. **Disabled rather than deleted.**

    The tokens it already holds carry its `client_id`, and a funnel or an audit row naming a
    client that no longer exists is a question nobody can answer later.
    """
    row = await session.get(OAuthClient, row_id)
    if row is None or row.org_id != user.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such client.")

    from datetime import UTC, datetime

    row.disabled_at = datetime.now(UTC)
    await session.commit()
    return {
        "note": (
            "Disabled. It can no longer ask anybody for access. Tokens it already holds keep "
            "working until they expire -- revoke those in Settings."
        )
    }
