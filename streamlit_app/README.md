# PDF Bridge Streamlit workspace

Status: Current

This is the production-canonical operator experience for PDF Bridge API v2. It is a pure HTTP
client: Streamlit never opens the catalog database, reads managed storage paths, loads models, or
connects to Qdrant directly.

## Operator workflow

| Page | Workflow |
|---|---|
| Operations | Process and dependency readiness, collection lifecycle counts, and fixed physical-target status |
| Intake | Collection selection, filename-only advisory, bounded upload, and two-second document/operation polling |
| Review | Revision-bound semantic preflight, candidate evidence, Keep/Replace/Cancel, and exact-phase retry |
| Library | Cursor-paged current documents; source preview/download; Markdown, chunk, lifecycle, operation, and event inspection; high-priority delete; terminal history |
| Search | Optional operator-only proxy to the configured external active-corpus retrieval service, with an explicit unavailable state |

Uploads, decisions, retries, and deletes use stable `Idempotency-Key` values. A delete immediately
blocks content access and queues `HIGH`-priority verified point and storage removal. Deleted,
cancelled, and rejected documents leave content-free tombstones in History.

The client retains the server-owned authentication cookie for one Streamlit session. Before its
first protected request, it performs an authenticated `GET /api/v2/collections` and reads the CSRF
token from the `X-CSRF-Token` response header. It refreshes that session once after an explicit CSRF
failure. The browser never manufactures an identity header or receives an upstream search secret.

## API v2 coverage

The workspace consumes only `/api/v2` and covers:

- liveness and readiness;
- collection list/detail, filename advisory, document list, and upload;
- document detail, source PDF, canonical Markdown, public chunks, and semantic preflight;
- decisions, retry, high-priority delete, audit events, and operation polling;
- terminal history and optional operator search.

Lists use the API's opaque `cursor` and bounded `limit`; no view derives offsets or parses cursor
contents. Protected paths, raw vectors, prompts, model output, credentials, and provider failures
are not rendered.

## Launch

Start the configured Litestar service first. Then, from the repository root:

```bash
python -m pip install -e '.[streamlit]'
streamlit run streamlit_app/app.py
```

For the reference container topology, configure `.env` and run `docker compose up --build` from
the repository root. Compose publishes this workspace on `http://127.0.0.1:8501`, waits for Bridge
readiness, and gives the Streamlit container only the dedicated internal operator network. It
receives no Bridge storage, model cache, ClamAV/Qdrant networks, or provider credentials.

`PDF_BRIDGE_URL` selects the fixed Litestar service root and defaults to
`http://127.0.0.1:8000`. It is deployment-owned, cannot be changed in the workspace, and redirects
from Bridge are refused.

`PDF_BRIDGE_STREAMLIT_MAX_UPLOAD_FILES` caps one Intake selection, defaults to `5`, and accepts
values from 1 through 20. The UI rejects a larger selection before filename advisories or uploads;
the API continues to accept exactly one PDF per request.

For trusted-header authentication, set `PDF_BRIDGE_STREAMLIT_IDENTITY_HEADER` to the header name
injected by the approved Streamlit ingress. Streamlit requires that header on the current request
and forwards its value to Bridge; no form or browser script can supply an alternate identity. Block
direct access to both services and configure Bridge's trusted proxy CIDRs to include Streamlit's
direct peer.
