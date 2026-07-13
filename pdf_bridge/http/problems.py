"""RFC 9457-style responses for deliberate PDF Bridge failures."""

from __future__ import annotations

import logging
from typing import Any

from litestar import Request, Response
from litestar.openapi.datastructures import ResponseSpec

from pdf_bridge.contracts.schemas import ProblemDetail
from pdf_bridge.http.middleware import ensure_request_id

logger = logging.getLogger(__name__)


class ProblemError(Exception):
    """A deliberate, user-safe API failure."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.extra = extra or {}


def problem_responses() -> dict[int, ResponseSpec]:
    """Build the common OpenAPI declarations for problem-detail responses."""

    descriptions = {
        401: "Authentication failed",
        403: "Request was not authorized",
        404: "Resource was not found",
        409: "State or duplicate conflict",
        413: "Upload is too large",
        422: "Request was deliberately rejected",
        500: "Catalog or storage inconsistency",
        502: "Invalid retrieval service response",
        503: "Required dependency is unavailable",
    }
    return {
        status: ResponseSpec(
            data_container=ProblemDetail,
            description=description,
            media_type="application/problem+json",
            generate_examples=False,
        )
        for status, description in descriptions.items()
    }


def problem_response(
    *,
    request: Request,
    status: int,
    code: str,
    title: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> Response[dict[str, Any]]:
    """Build an RFC 9457-style response with request correlation metadata."""

    request_id = ensure_request_id(request.scope)
    body: dict[str, Any] = {
        "type": f"https://pdf-bridge.invalid/problems/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "instance": str(request.url.path),
        "request_id": request_id,
    }
    if extra:
        body.update(extra)
    return Response(
        content=body,
        status_code=status,
        media_type="application/problem+json",
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "same-origin",
            "X-Content-Type-Options": "nosniff",
            "X-Request-ID": request_id,
        },
    )


def handle_problem(
    request: Request, exc: ProblemError
) -> Response[dict[str, Any]]:
    """Convert a deliberate application failure to a problem response."""

    return problem_response(
        request=request,
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        extra=exc.extra,
    )


def handle_unexpected(
    request: Request, exc: Exception
) -> Response[dict[str, Any]]:
    """Log unexpected failures safely while retaining Litestar's native wire shape."""

    request_id = ensure_request_id(request.scope)
    logger.error(
        "unexpected request failure",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"request_id": request_id},
    )
    return Response(
        content={"status_code": 500, "detail": "Internal Server Error"},
        status_code=500,
        headers={"Cache-Control": "no-store", "X-Request-ID": request_id},
    )


exception_handlers = {
    ProblemError: handle_problem,
    500: handle_unexpected,
}
