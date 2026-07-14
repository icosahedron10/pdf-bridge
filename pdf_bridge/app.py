"""Litestar assembly for the API-v2-only intake service."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager

import httpx
from litestar import Litestar, Router
from litestar.datastructures import State
from litestar.di import Provide
from litestar.middleware import DefineMiddleware
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import JsonRenderPlugin
from litestar.types import ASGIApp, Message, Receive, Scope, Send
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge import __version__
from pdf_bridge.contracts.schemas import ErrorResponse
from pdf_bridge.controllers.api import create_api_routers
from pdf_bridge.core.config import Settings, get_settings
from pdf_bridge.core.logging_config import configure_logging
from pdf_bridge.http.middleware import (
    PortAwareTrustedHostMiddleware,
    RequestContextMiddleware,
    ensure_request_id,
)
from pdf_bridge.http.problems import exception_handlers
from pdf_bridge.managers.worker import AnalysisWorker, WorkerProviders, providers_from_settings
from pdf_bridge.persistence.db import build_engine, build_session_factory
from pdf_bridge.services.scanner import Scanner, scanner_from_settings

DBProvider = Callable[[], Iterator[Session]]
UPLOAD_REQUEST_OVERHEAD_BYTES = 1_048_576


class _OpenApiJsonOnlyMiddleware:
    """Replace OpenAPI-router HTML misses with the strict JSON API error."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        replacement_body: bytes | None = None
        replacement_sent = False

        async def send_json_only(message: Message) -> None:
            nonlocal replacement_body, replacement_sent
            if message["type"] == "http.response.start" and message["status"] in {
                404,
                405,
            }:
                status = message["status"]
                request_id = ensure_request_id(scope)
                error = ErrorResponse.model_validate(
                    {
                        "error": {
                            "code": (
                                "route_not_found"
                                if status == 404
                                else "method_not_allowed"
                            ),
                            "message": (
                                "The requested API route was not found."
                                if status == 404
                                else "The requested method is not allowed for this API route."
                            ),
                            "request_id": request_id,
                            "retryable": False,
                        }
                    }
                )
                replacement_body = error.model_dump_json().encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(replacement_body)).encode("ascii")),
                            (b"cache-control", b"no-store"),
                            (b"x-request-id", request_id.encode("ascii")),
                        ],
                    }
                )
                return
            if message["type"] == "http.response.body" and replacement_body is not None:
                if not replacement_sent:
                    replacement_sent = True
                    await send(
                        {
                            "type": "http.response.body",
                            "body": replacement_body,
                            "more_body": False,
                        }
                    )
                return
            await send(message)

        await self.app(scope, receive, send_json_only)


def _session_key(settings: Settings) -> bytes:
    secret = settings.session_secret.get_secret_value().encode("utf-8")
    return hashlib.sha256(b"pdf-bridge/session/v2\0" + secret).digest()


def _db_from_state(state: State) -> Iterator[Session]:
    factory: sessionmaker[Session] = state.db_session_factory
    with factory() as session:
        yield session


def _openapi_config(settings: Settings) -> OpenAPIConfig | None:
    if settings.app_env == "enterprise":
        return None
    return OpenAPIConfig(
        title="PDF Bridge API",
        summary="Prepared-revision semantic PDF intake and replacement API.",
        version=__version__,
        path="/api",
        openapi_router=Router(
            path="/api",
            route_handlers=[],
            include_in_schema=False,
            middleware=[DefineMiddleware(_OpenApiJsonOnlyMiddleware)],
        ),
        render_plugins=[JsonRenderPlugin(path="/openapi.json")],
    )


def create_app(
    settings: Settings | None = None,
    *,
    scanner: Scanner | None = None,
    search_http_client: httpx.Client | None = None,
    db_provider: DBProvider | None = None,
    worker: AnalysisWorker | None = None,
    worker_providers: WorkerProviders | None = None,
) -> Litestar:
    """Assemble the sole v2 service surface and its lifespan-owned worker."""

    active_settings = settings or get_settings()
    if active_settings.app_env != "test" and (db_provider is not None or worker is not None):
        raise RuntimeError("custom database and worker injection is supported only in test mode")
    configure_logging()

    @asynccontextmanager
    async def lifespan(application: Litestar):
        engine: Engine | None = None
        owned_search_client: httpx.Client | None = None
        owned_provider_client: httpx.Client | None = None
        owned_worker: AnalysisWorker | None = None
        try:
            factory: sessionmaker[Session] | None = None
            if db_provider is None:
                engine = build_engine(active_settings.database_url)
                factory = build_session_factory(engine)
                application.state.db_session_factory = factory
            if search_http_client is None:
                owned_search_client = httpx.Client(
                    timeout=active_settings.search_api_timeout_seconds
                )
                application.state.search_http_client = owned_search_client

            if worker is not None:
                application.state.worker = worker
            elif active_settings.worker_enabled and factory is not None:
                providers = worker_providers
                if providers is None:
                    owned_provider_client = httpx.Client()
                    providers = providers_from_settings(
                        active_settings,
                        http_client=owned_provider_client,
                    )
                application.state.worker_providers = providers
                owned_worker = AnalysisWorker(
                    settings=active_settings,
                    session_factory=factory,
                    providers=providers,
                )
                owned_worker.start()
                application.state.worker = owned_worker
            yield
        finally:
            if owned_worker is not None:
                owned_worker.stop()
            if owned_provider_client is not None:
                owned_provider_client.close()
            if owned_search_client is not None:
                owned_search_client.close()
            if engine is not None:
                engine.dispose()

    state_values: dict[str, object] = {
        "settings": active_settings,
        "scanner": scanner or scanner_from_settings(active_settings),
        "transition_lock": threading.RLock(),
    }
    if search_http_client is not None:
        state_values["search_http_client"] = search_http_client
    if worker is not None:
        state_values["worker"] = worker
    if worker_providers is not None:
        state_values["worker_providers"] = worker_providers

    session_config = CookieBackendConfig(
        secret=_session_key(active_settings),
        key="pdf_bridge_session",
        max_age=8 * 60 * 60,
        path="/",
        secure=active_settings.app_env == "enterprise",
        httponly=True,
        samesite="strict",
    )
    upload_request_limit = active_settings.max_upload_bytes + UPLOAD_REQUEST_OVERHEAD_BYTES
    application = Litestar(
        route_handlers=create_api_routers(upload_request_limit),
        dependencies={"db": Provide(db_provider or _db_from_state)},
        exception_handlers=exception_handlers,
        lifespan=[lifespan],
        middleware=[session_config.middleware],
        openapi_config=_openapi_config(active_settings),
        state=State(state_values),
    )
    application.asgi_handler = RequestContextMiddleware(
        PortAwareTrustedHostMiddleware(
            application.asgi_handler,
            allowed_hosts=active_settings.allowed_hosts,
        )
    )
    return application


app = create_app()
