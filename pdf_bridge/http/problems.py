"""Sanitized API v2 error responses."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from litestar import Request, Response
from litestar.exceptions import (
    MethodNotAllowedException,
    NotFoundException,
    ValidationException,
)
from litestar.openapi.datastructures import ResponseSpec

from pdf_bridge.contracts.schemas import ErrorResponse
from pdf_bridge.http.middleware import ensure_request_id

logger = logging.getLogger(__name__)

_FAILURE_CODE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


class ProblemError(Exception):
    """A deliberate transport-safe failure.

    Only the exact-duplicate document UUID is accepted as bounded conflict
    context. Arbitrary service details are never serialized.
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        detail: str,
        retryable: bool = False,
        existing_document_id: str | uuid.UUID | None = None,
        title: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        del title, extra
        if not _FAILURE_CODE.fullmatch(code):
            raise ValueError("problem codes must satisfy the API v2 failure-code contract")
        message = " ".join(detail.split())
        if not message or len(message) > 500:
            raise ValueError("problem messages must contain 1 through 500 characters")
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = message
        self.retryable = retryable
        try:
            self.existing_document_id = (
                uuid.UUID(str(existing_document_id))
                if existing_document_id is not None
                else None
            )
        except ValueError as exc:
            raise ValueError("existing_document_id must be a UUID") from exc


def problem_responses() -> dict[int, ResponseSpec]:
    """Declare the one error envelope used by every v2 endpoint."""

    descriptions = {
        400: "Malformed request",
        401: "Authentication failed",
        403: "Request was not authorized",
        404: "Resource was not found",
        409: "State, duplicate, or idempotency conflict",
        410: "Content was intentionally purged",
        413: "Upload is too large",
        415: "Media type is not supported",
        422: "Request validation failed",
        500: "Internal service failure",
        502: "Dependency returned an invalid response",
        503: "Required dependency is unavailable",
    }
    return {
        status: ResponseSpec(
            data_container=ErrorResponse,
            description=description,
            media_type="application/json",
            generate_examples=False,
        )
        for status, description in descriptions.items()
    }


def _request_uuid(request: Request) -> uuid.UUID:
    request_id = ensure_request_id(request.scope)
    try:
        return uuid.UUID(request_id)
    except ValueError as exc:  # pragma: no cover - middleware owns this invariant
        raise RuntimeError("request middleware produced a non-UUID request ID") from exc


def problem_response(
    *,
    request: Request,
    status: int,
    code: str,
    detail: str,
    retryable: bool = False,
    existing_document_id: uuid.UUID | None = None,
) -> Response[dict[str, Any]]:
    """Build the strict nested v2 envelope."""

    request_id = _request_uuid(request)
    body = ErrorResponse.model_validate(
        {
            "error": {
                "code": code,
                "message": detail,
                "request_id": request_id,
                "retryable": retryable,
                "existing_document_id": existing_document_id,
            }
        }
    )
    return Response(
        content=body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type="application/json",
        headers={"Cache-Control": "no-store", "X-Request-ID": str(request_id)},
    )


def handle_problem(request: Request, exc: ProblemError) -> Response[dict[str, Any]]:
    """Convert a deliberate application failure to the v2 envelope."""

    return problem_response(
        request=request,
        status=exc.status,
        code=exc.code,
        detail=exc.detail,
        retryable=exc.retryable,
        existing_document_id=exc.existing_document_id,
    )


def handle_unexpected(request: Request, exc: Exception) -> Response[dict[str, Any]]:
    """Log internal detail and disclose only a stable failure."""

    request_id = ensure_request_id(request.scope)
    logger.error(
        "unexpected request failure",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={"request_id": request_id},
    )
    return problem_response(
        request=request,
        status=500,
        code="internal_error",
        detail="The service could not complete the request.",
        retryable=False,
    )


def handle_validation(
    request: Request, exc: ValidationException
) -> Response[dict[str, Any]]:
    """Replace framework validation detail with the strict safe v2 envelope."""

    del exc
    media_type = request.headers.get("content-type", "").partition(";")[0].casefold()
    json_body = media_type == "application/json"
    return problem_response(
        request=request,
        status=422 if json_body else 400,
        code="request_validation_failed" if json_body else "invalid_request",
        detail=(
            "The JSON request body does not match the API contract."
            if json_body
            else "The request parameters or multipart body are invalid."
        ),
    )


def handle_not_found(
    request: Request, exc: NotFoundException
) -> Response[dict[str, Any]]:
    """Return the same v2 envelope for unknown routes."""

    del exc
    return problem_response(
        request=request,
        status=404,
        code="route_not_found",
        detail="The requested API route was not found.",
    )


def handle_method_not_allowed(
    request: Request, exc: MethodNotAllowedException
) -> Response[dict[str, Any]]:
    """Return the same v2 envelope for unsupported route methods."""

    del exc
    return problem_response(
        request=request,
        status=405,
        code="method_not_allowed",
        detail="The requested method is not allowed for this API route.",
    )


exception_handlers = {
    ProblemError: handle_problem,
    ValidationException: handle_validation,
    NotFoundException: handle_not_found,
    MethodNotAllowedException: handle_method_not_allowed,
    500: handle_unexpected,
}
