"""FastAPI application assembly."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pdf_bridge import __version__
from pdf_bridge.api import router as api_router
from pdf_bridge.config import Settings, get_settings
from pdf_bridge.db import build_engine, build_session_factory
from pdf_bridge.jobs import router as jobs_router
from pdf_bridge.lifecycle import validate_collection_references
from pdf_bridge.logging_config import configure_logging
from pdf_bridge.middleware import RequestContextMiddleware, UploadSizeLimitMiddleware
from pdf_bridge.problems import install_problem_handlers
from pdf_bridge.scanner import Scanner, scanner_from_settings
from pdf_bridge.web import router as web_router


def create_app(
    settings: Settings | None = None,
    *,
    scanner: Scanner | None = None,
    search_http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    configure_logging()
    expose_docs = active_settings.app_env != "enterprise"

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        if active_settings.app_env != "test":
            engine = build_engine(active_settings.database_url)
            try:
                factory = build_session_factory(engine)
                with factory() as session:
                    validate_collection_references(
                        session,
                        {collection.key for collection in active_settings.collections},
                    )
            finally:
                engine.dispose()
        yield

    application = FastAPI(
        title="PDF Bridge API",
        summary="A transparent upload and scheduled-ingestion bridge for PDF documents.",
        version=__version__,
        docs_url="/api/docs" if expose_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if expose_docs else None,
        lifespan=lifespan,
    )
    application.state.settings = active_settings
    application.state.scanner = scanner or scanner_from_settings(active_settings)
    # SQLite is intentionally single-process for this POC. This lock makes the
    # select-transition-commit boundary atomic across FastAPI's worker threads.
    application.state.transition_lock = threading.RLock()
    if search_http_client is not None:
        application.state.search_http_client = search_http_client

    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=active_settings.allowed_hosts,
        www_redirect=False,
    )
    application.add_middleware(
        SessionMiddleware,
        secret_key=active_settings.session_secret.get_secret_value(),
        session_cookie="pdf_bridge_session",
        same_site="strict",
        https_only=active_settings.app_env == "enterprise",
        max_age=8 * 60 * 60,
    )
    application.add_middleware(
        UploadSizeLimitMiddleware,
        max_upload_bytes=active_settings.max_upload_bytes,
    )
    application.add_middleware(RequestContextMiddleware)

    static_root = Path(__file__).with_name("static")
    application.mount("/static", StaticFiles(directory=static_root), name="static")
    application.include_router(api_router)
    application.include_router(jobs_router)
    application.include_router(web_router)
    install_problem_handlers(application)
    return application


app = create_app()
