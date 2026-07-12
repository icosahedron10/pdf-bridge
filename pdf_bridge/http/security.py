"""Human/request identity, CSRF, and service-token checks."""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from litestar import Request

from pdf_bridge.http.problems import ProblemError


@dataclass(frozen=True, slots=True)
class Actor:
    identifier: str
    kind: str


def ensure_browser_session(request: Request) -> None:
    session = dict(request.session)
    changed = False
    if "session_id" not in session:
        session["session_id"] = secrets.token_urlsafe(18)
        changed = True
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
        changed = True
    if changed:
        request.set_session(session)


def csrf_token(request: Request) -> str:
    ensure_browser_session(request)
    return str(request.session["csrf_token"])


def _proxy_is_trusted(host: str | None, cidrs: list[str] | tuple[str, ...]) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def get_actor(request: Request) -> Actor:
    settings = request.app.state.settings
    ensure_browser_session(request)
    if settings.auth_mode == "anonymous-poc":
        session_id = str(request.session["session_id"])
        return Actor(identifier=f"anonymous:{session_id[:10]}", kind="anonymous")

    client_host = request.client.host if request.client else None
    if not _proxy_is_trusted(client_host, settings.trusted_proxy_cidrs):
        raise ProblemError(
            status=401,
            code="untrusted-identity-source",
            title="Identity was not accepted",
            detail="The identity header did not come from a configured trusted proxy.",
        )
    identifier = request.headers.get(settings.trusted_identity_header, "").strip()
    if not identifier or len(identifier) > 200 or any(ord(char) < 32 for char in identifier):
        raise ProblemError(
            status=401,
            code="identity-required",
            title="Identity is required",
            detail="The trusted proxy did not provide a valid user identity.",
        )
    return Actor(identifier=identifier, kind="trusted-header")


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    expected = request.headers.get("host", "")
    return parsed.scheme in {"http", "https"} and parsed.netloc.casefold() == expected.casefold()


async def require_csrf(request: Request) -> Actor:
    if not _same_origin(request):
        raise ProblemError(
            status=403,
            code="cross-origin-request",
            title="Cross-origin request rejected",
            detail="Mutating browser requests must originate from PDF Bridge.",
        )
    ensure_browser_session(request)
    supplied = request.headers.get("x-csrf-token", "")
    expected = str(request.session["csrf_token"])
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise ProblemError(
            status=403,
            code="csrf-check-failed",
            title="Request verification failed",
            detail="Refresh the page and retry the action.",
        )
    return get_actor(request)


def require_job_token(request: Request) -> Actor:
    settings = request.app.state.settings
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    expected = settings.job_token.get_secret_value() if settings.job_token else ""
    if scheme.casefold() != "bearer" or not token or not hmac.compare_digest(token, expected):
        raise ProblemError(
            status=401,
            code="job-authentication-failed",
            title="Job authentication failed",
            detail="A valid Jenkins service token is required.",
        )
    return Actor(identifier="jenkins", kind="service")
