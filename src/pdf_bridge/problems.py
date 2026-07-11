"""RFC 9457-style problem responses used by both the UI and JSON API."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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


def problem_response(
    *,
    request: Request,
    status: int,
    code: str,
    title: str,
    detail: str,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://pdf-bridge.invalid/problems/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        "instance": str(request.url.path),
        "request_id": getattr(request.state, "request_id", None),
    }
    if extra:
        body.update(extra)
    response = JSONResponse(body, status_code=status, media_type="application/problem+json")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    request_id = body["request_id"]
    if request_id:
        response.headers["X-Request-ID"] = str(request_id)
    return response


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code_by_status = {
            400: "bad-request",
            401: "not-authenticated",
            403: "forbidden",
            404: "not-found",
            405: "method-not-allowed",
        }
        response = problem_response(
            request=request,
            status=exc.status_code,
            code=code_by_status.get(exc.status_code, "http-error"),
            title="HTTP request was rejected",
            detail=str(exc.detail),
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @app.exception_handler(ProblemError)
    async def handle_problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(
            request=request,
            status=exc.status,
            code=exc.code,
            title=exc.title,
            detail=exc.detail,
            extra=exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return problem_response(
            request=request,
            status=422,
            code="validation-error",
            title="Request validation failed",
            detail="One or more request fields were invalid.",
            extra={"errors": errors},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unexpected request failure",
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        return problem_response(
            request=request,
            status=500,
            code="internal-error",
            title="Unexpected server error",
            detail=(
                "The request could not be completed. Use the request ID when contacting support."
            ),
        )

    default_openapi = app.openapi

    def problem_openapi() -> dict[str, Any]:
        schema = default_openapi()
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if not isinstance(operation, dict):
                    continue
                for response in operation.get("responses", {}).values():
                    content = response.get("content", {})
                    if "application/problem+json" in content:
                        # FastAPI registers the Pydantic model under its default
                        # JSON media type. Runtime failures are RFC problem JSON,
                        # so remove the misleading alternative after registration.
                        content.pop("application/json", None)
        return schema

    app.openapi = problem_openapi
