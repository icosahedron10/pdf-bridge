"""Litestar application assembly."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from litestar import Litestar, MediaType, Response, Router
from litestar.datastructures import State
from litestar.di import Provide
from litestar.middleware.session.client_side import CookieBackendConfig
from litestar.openapi.config import OpenAPIConfig
from litestar.openapi.plugins import JsonRenderPlugin, SwaggerRenderPlugin
from litestar.plugins.jinja import JinjaTemplateEngine
from litestar.static_files import create_static_files_router
from litestar.template.config import TemplateConfig
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pdf_bridge import __version__
from pdf_bridge.controllers.api import create_api_routers
from pdf_bridge.controllers.jobs import jobs_router
from pdf_bridge.controllers.web import TEMPLATE_ROOT, web_router
from pdf_bridge.core.config import Settings, get_settings
from pdf_bridge.core.logging_config import configure_logging
from pdf_bridge.http.middleware import PortAwareTrustedHostMiddleware, RequestContextMiddleware
from pdf_bridge.http.problems import exception_handlers
from pdf_bridge.persistence.db import build_engine, build_session_factory
from pdf_bridge.services.lifecycle import validate_collection_references
from pdf_bridge.services.scanner import Scanner, scanner_from_settings

DBProvider = Callable[[], Iterator[Session]]
UPLOAD_REQUEST_OVERHEAD_BYTES = 1_048_576


def _session_key(settings: Settings) -> bytes:
    secret = settings.session_secret.get_secret_value().encode("utf-8")
    return hashlib.sha256(b"pdf-bridge/session/v1\0" + secret).digest()


def _db_from_state(state: State) -> Iterator[Session]:
    """Yield a request session from the lifespan-owned session factory."""

    factory: sessionmaker[Session] = state.db_session_factory
    with factory() as session:
        yield session


def _normalize_openapi_not_found(response: Response) -> Response:
    """Keep unknown development schema routes on Litestar's JSON error contract."""

    if response.status_code == 404:
        return Response(
            content={"status_code": 404, "detail": "Not Found"},
            status_code=404,
            media_type=MediaType.JSON,
        )
    return response


def _openapi_config(settings: Settings) -> OpenAPIConfig | None:
    if settings.app_env == "enterprise":
        return None
    return OpenAPIConfig(
        title="PDF Bridge API",
        summary="A transparent upload and scheduled-ingestion bridge for PDF documents.",
        version=__version__,
        path="/api",
        openapi_router=Router(
            path="/api",
            route_handlers=[],
            after_request=_normalize_openapi_not_found,
            include_in_schema=False,
        ),
        render_plugins=[
            SwaggerRenderPlugin(
                path="/docs",
                favicon=(
                    '<link rel="icon" type="image/svg+xml" '
                    'href="/static/favicon.svg">'
                ),
            ),
            JsonRenderPlugin(path="/openapi.json", media_type=MediaType.JSON),
        ],
    )


def create_app(
    settings: Settings | None = None,
    *,
    scanner: Scanner | None = None,
    search_http_client: httpx.Client | None = None,
    db_provider: DBProvider | None = None,
) -> Litestar:
    """Assemble and configure the PDF Bridge Litestar application."""

    active_settings = settings or get_settings()
    if db_provider is not None and active_settings.app_env != "test":
        raise RuntimeError("custom database providers are supported only in test mode")
    configure_logging()

    @asynccontextmanager
    async def lifespan(application: Litestar):
        """Own the engine, session factory, and retrieval client for the app.

        Owned resources are built from the settings given to ``create_app`` so
        the validated database is the served database, and they are always
        released — including when startup validation fails. Injected resources
        remain caller-owned and are never closed here.
        """

        engine: Engine | None = None
        owned_search_client: httpx.Client | None = None
        try:
            if db_provider is None:
                engine = build_engine(active_settings.database_url)
                factory = build_session_factory(engine)
                with factory() as session:
                    validate_collection_references(
                        session,
                        {collection.key for collection in active_settings.collections},
                    )
                application.state.db_session_factory = factory
            if search_http_client is None:
                owned_search_client = httpx.Client(timeout=active_settings.search_api_timeout)
                application.state.search_http_client = owned_search_client
            yield
        finally:
            if owned_search_client is not None:
                owned_search_client.close()
            if engine is not None:
                engine.dispose()

    state_values: dict[str, object] = {
        "settings": active_settings,
        "scanner": scanner or scanner_from_settings(active_settings),
        # SQLite remains deliberately single-process. Litestar runs blocking
        # handlers in worker threads, so transition boundaries still share a lock.
        "transition_lock": threading.RLock(),
    }
    if search_http_client is not None:
        state_values["search_http_client"] = search_http_client

    session_config = CookieBackendConfig(
        secret=_session_key(active_settings),
        key="pdf_bridge_session",
        max_age=8 * 60 * 60,
        path="/",
        secure=active_settings.app_env == "enterprise",
        httponly=True,
        samesite="strict",
    )

    static_root = Path(__file__).with_name("static")
    upload_request_limit = (
        active_settings.max_upload_bytes + UPLOAD_REQUEST_OVERHEAD_BYTES
    )
    application = Litestar(
        route_handlers=[
            *create_api_routers(upload_request_limit),
            jobs_router,
            web_router,
            create_static_files_router(path="/static", directories=[static_root]),
        ],
        dependencies={
            # Litestar manages generator dependency cleanup itself; its
            # sync_to_thread flag intentionally has no effect on generators.
            "db": Provide(db_provider or _db_from_state),
        },
        exception_handlers=exception_handlers,
        lifespan=[lifespan],
        middleware=[session_config.middleware],
        openapi_config=_openapi_config(active_settings),
        state=State(state_values),
        template_config=TemplateConfig(
            directory=TEMPLATE_ROOT,
            engine=JinjaTemplateEngine,
        ),
    )

    # Litestar attaches ordinary middleware after route resolution. Wrap the
    # completed handler so host checks, request IDs, and security headers also
    # cover framework-generated 404 and 405 responses.
    application.asgi_handler = RequestContextMiddleware(
        PortAwareTrustedHostMiddleware(
            application.asgi_handler,
            allowed_hosts=active_settings.allowed_hosts,
        )
    )
    return application


app = create_app()
