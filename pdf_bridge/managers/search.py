"""Thin orchestration for retrieval and catalog correlation."""

from __future__ import annotations

from collections.abc import Sequence

import httpx
from sqlalchemy.orm import Session

from pdf_bridge.contracts.schemas import SearchRequest, SearchResponse
from pdf_bridge.core.config import CollectionDefinition, Settings
from pdf_bridge.services import catalog
from pdf_bridge.services.search import search_retrieval


async def search_documents(
    session: Session,
    *,
    settings: Settings,
    definitions: Sequence[CollectionDefinition],
    request: SearchRequest,
    client: httpx.AsyncClient | None,
) -> SearchResponse:
    catalog.validate_configured_collections(definitions, request.collections)
    response = await search_retrieval(settings, request, client=client)
    catalog.validate_search_response(session, request, response)
    return response
