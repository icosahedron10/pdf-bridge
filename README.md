# PDF Bridge

PDF Bridge is a small, Python-first handoff service for a scheduled PDF retrieval pipeline. It
gives a trusted proof-of-concept user a collection-aware library, upload queue, classification
review workspace, and document history; it gives Jenkins an idempotent way to claim work, verify
files, and report pipeline outcomes. The bridge deliberately does **not** parse PDFs or talk to
Qdrant.

> [!WARNING]
> This is a network-restricted proof of concept, not an internet-facing upload service. ClamAV
> reduces risk but cannot make hostile PDFs safe. Before enterprise use, complete the security
> gates in [docs/security.md](docs/security.md), especially SSO, TLS, an approved malware-control
> design, and sandboxing the downstream PDF parser.

## Supported platform

Linux is the only supported host operating system. The direct-development commands, Jenkins
agent, filesystem paths, and service-management guidance in this repository assume a Linux host
with a POSIX shell. Windows and macOS hosts and Jenkins agents are out of scope.

## Start locally with Docker

Requirements: Docker Engine with Compose on Linux, at least 4 GiB available to ClamAV, and a
location that is not synchronized for any host-side Jenkins staging files.

```text
cp .env.example .env
```

Edit `.env` and replace both values marked `CHANGE_ME`, then run:

```text
docker compose up --build
```

The application is available at <http://localhost:8000>. The first ClamAV start can take several
minutes while signatures download; `docker compose ps` shows readiness. PDF bytes and SQLite live
in the Docker-managed `bridge_data` volume, never in this checkout or a synchronized directory.
Interactive API documentation is available at <http://localhost:8000/api/docs> outside enterprise
mode, with the machine-readable contract at <http://localhost:8000/api/openapi.json>.
Compose binds to loopback by default. For an approved internal-network demo, set
`PDF_BRIDGE_BIND_ADDRESS` to the server's internal interface and add the URL's hostname to
`PDF_BRIDGE_ALLOWED_HOSTS`; do not expose anonymous POC mode to the internet.

Useful checks:

```text
curl http://localhost:8000/api/v1/health/live
curl http://localhost:8000/api/v1/health/ready
curl http://localhost:8000/api/v1/health/dependencies
docker compose logs -f app clamav
```

## Run directly on Linux for development

Python 3.12 is required. Choose an absolute data directory outside this repository and any
synchronized directory; the application refuses unsafe storage paths.

```text
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
export PDF_BRIDGE_STORAGE_ROOT="$HOME/.local/share/pdf-bridge"
export PDF_BRIDGE_SESSION_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export PDF_BRIDGE_JOB_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export PDF_BRIDGE_COLLECTIONS='[
  {"key":"customer","display_name":"Customer Product","description":"Approved customer-facing product content.","audience":"customer"},
  {"key":"internal","display_name":"HR & Internal","description":"Employee-only policies and operations.","audience":"internal"}
]'
export PDF_BRIDGE_CLAMD_HOST=localhost
alembic upgrade head
uvicorn pdf_bridge.app:app --reload --no-access-log --no-proxy-headers
```

Direct development still needs a reachable `clamd` service (configured as `clamav:3310` by
default; set `PDF_BRIDGE_CLAMD_HOST=localhost` for a host-local daemon). Uploads fail closed when
the scanner is unavailable. Do not use Uvicorn
reload against real bridge data: the supported POC runtime is one application process.

## What the bridge owns

- The canonical scanned PDF and its SHA-256 checksum.
- Deployment-configured collection metadata and each document's immutable corpus placement.
- The catalog, language evidence, queue state, Jenkins batches, and append-only lifecycle audit
  trail.
- Human actions to upload, cancel pending work, retry failed work, and request deletion.
- A typed boundary to the external retrieval search service.

Jenkins owns the scheduled handoff. It claims an immutable batch, downloads each ingest PDF,
verifies byte count and SHA-256, atomically promotes the complete batch, then acknowledges staging.
After the external ingestion job finishes, Jenkins reports success, operational failure, or
classification review for every operation. An ingested PDF is not shown as deleted until all
required downstream removals are acknowledged.

The collection key is deliberately shared across PDF Bridge configuration, the Qdrant collection
name, and the chatbot manager's `allowed_collections` values. PDF Bridge controls which corpus a
PDF enters; it is not the end-user authorization layer. The chatbot manager must intersect every
retrieval request with the authenticated user's server-side allowlist before querying Qdrant.

## Collection and language visibility

`/library` shows every configured collection, its explicit **Customer-facing** or **Internal only**
audience, available/processing/review counts, and English/French/undetermined breakdown. A root
search asks the retrieval service for an exact document total for every collection, including
zero, and carries the query into the selected collection. Collection pages request hits from only
that collection; a missing group, cross-collection hit, wrong-language hit, inactive catalog ID,
or impossible total rejects the entire response instead of showing partial results.

New uploads are clean-scanned and queued as `und`. The existing downstream PDF parser—not PDF
Bridge, Node, V8, PDF.js, or `franc`—classifies extracted text as `en` or `fr` before indexing. A
safe undetermined outcome enters **Needs review** without BM25 or dense writes. A migrated,
unassigned legacy row can be assigned and sent through detection; a pipeline-undetermined row keeps
its collection and requires a reasoned English/French override or removal.

## Jenkins on Linux

Install an exact released wheel on a controlled Linux Jenkins agent and provide the service token
through the credentials store, never as a command-line argument. Pin the expected hostname
separately from the service URL:

```text
export PDF_BRIDGE_JOB_TOKEN="<injected-by-Jenkins>"
export PDF_BRIDGE_JOB_ALLOWED_HOST=pdf-bridge.internal
pdf-bridge-job pull --base-url https://pdf-bridge.internal --allowed-host "$PDF_BRIDGE_JOB_ALLOWED_HOST" --destination /srv/rag-handoff --request-id nightly-2026-07-10 --result-file pull-result.json

# The ingestion pipeline reads the staged batch manifest and writes report.json.
pdf-bridge-job report report.json --pull-result pull-result.json --base-url https://pdf-bridge.internal --allowed-host "$PDF_BRIDGE_JOB_ALLOWED_HOST"
```

`pull` uses a temporary sibling directory, verifies every file, writes `manifest.json`, and only
then renames the directory to `<destination>/<batch-id>`. Reusing a request ID is safe: a matching
existing directory is verified and re-acknowledged. Any mismatch fails loudly. See
[docs/jenkins.md](docs/jenkins.md) and [Jenkinsfile.example](Jenkinsfile.example).

## Import an existing library

Prepare a version 2 import manifest with an explicit configured collection and attested `en` or
`fr` language for every already-ingested PDF, validate it without changing state, then apply it:

```text
pdf-bridge import-manifest historical.json --source-root /srv/approved-pdfs --dry-run
pdf-bridge import-manifest historical.json --source-root /srv/approved-pdfs --apply
```

Every manifest path must resolve below the explicit source root. Applying the import scans and
copies PDFs into canonical bridge storage before registering them as ingested; source files are
never moved or modified. See [docs/importing.md](docs/importing.md).

## Documentation

- [Architecture and lifecycle](docs/architecture.md)
- [Configuration reference](docs/configuration.md)
- [Jenkins handoff and report contracts](docs/jenkins.md)
- [Historical import and backup](docs/importing.md)
- [Operations and troubleshooting](docs/runbook.md)
- [Security model and enterprise gates](docs/security.md)

## Tests

```text
python -m pytest
python -m ruff check .

# After installing Chromium once:
export PDF_BRIDGE_RUN_BROWSER_TESTS=1
python -m pytest tests/test_browser.py

# With a test clamd reachable at PDF_BRIDGE_CLAMD_HOST/PORT:
export PDF_BRIDGE_RUN_CLAMAV_TESTS=1
python -m pytest tests/test_clamav_integration.py
```

The normal test suite uses temporary storage and fake external providers. The ClamAV smoke test is
separate because it requires Docker and current signatures. Browser tests require
`python -m playwright install chromium` once on the development machine.
