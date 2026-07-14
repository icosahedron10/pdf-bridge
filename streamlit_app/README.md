# PDF Bridge Streamlit workspace

A professional Streamlit front end covering the full PDF Bridge intake
lifecycle for the POC. It is a **pure HTTP client** of a running PDF Bridge
service: it bootstraps the same anonymous cookie session and CSRF token the
built-in browser UI uses, sends `Idempotency-Key` headers on every mutation,
and never opens the SQLite catalog directly (the supported topology is exactly
one application process).

## Pages

| Page | Coverage |
|---|---|
| Overview | Dependency health, collection counts, recent intake |
| Upload | Preflight filename advisories, scanned multipart upload, live tracking |
| Review queue | Durable open work, operation phases, candidate evidence with LLM findings and excerpt comparison, Keep / Replace / Cancel decisions, retry, cancel |
| Library | Catalog filters (scope, state, collection), document detail with audit ledger, operations, decisions, PDF download/preview, verified deletion |
| Search | Keyword / semantic / hybrid retrieval; ranked hits with snippets for one collection, totals across several |

## Running

1. Start PDF Bridge itself (`docker compose up --build`, or `uvicorn
   pdf_bridge.app:app` with a configured environment).
2. Install the UI dependency group and launch from the repository root so the
   bundled theme in `.streamlit/config.toml` applies:

   ```bash
   python -m pip install -e '.[streamlit]'
   streamlit run streamlit_app/app.py
   ```

3. The app targets `http://127.0.0.1:8000` by default. Override with the
   `PDF_BRIDGE_URL` environment variable or the sidebar setting.

## Authentication notes

The client supports the `anonymous-poc` mode. Each Streamlit browser session
holds its own bridge session, so audit events record distinct anonymous actor
identifiers. Behind a trusted-header deployment the app would need to run
behind the same identity-injecting proxy as the service; that is outside the
POC scope.

Uploads are limited server-side by `PDF_BRIDGE_MAX_UPLOAD_BYTES` (50 MiB by
default); the Streamlit uploader allows up to 100 MB and relies on the service
to reject oversized envelopes with a typed 413 problem.
