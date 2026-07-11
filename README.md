# PDF Bridge

PDF Bridge is a small, Python-first handoff service for a scheduled PDF retrieval pipeline. It
gives a trusted proof-of-concept user a transparent library, upload queue, and document history;
it gives Jenkins an idempotent way to claim work, verify files, and report pipeline outcomes.
The bridge deliberately does **not** parse PDFs or talk to Qdrant.

> [!WARNING]
> This is a network-restricted proof of concept, not an internet-facing upload service. ClamAV
> reduces risk but cannot make hostile PDFs safe. Before enterprise use, complete the security
> gates in [docs/security.md](docs/security.md), especially SSO, TLS, an approved malware-control
> design, and sandboxing the downstream PDF parser.

## Start locally with Docker

Requirements: Docker Desktop or Docker Engine with Compose, at least 4 GiB available to ClamAV,
and a location that is **not** synchronized by OneDrive for any host-side Jenkins staging files.

```text
copy .env.example .env       # Windows Command Prompt
# or: cp .env.example .env   # PowerShell, macOS, or Linux
```

Edit `.env` and replace both values marked `CHANGE_ME`, then run:

```text
docker compose up --build
```

The application is available at <http://localhost:8000>. The first ClamAV start can take several
minutes while signatures download; `docker compose ps` shows readiness. PDF bytes and SQLite live
in the Docker-managed `bridge_data` volume, never in this checkout or its OneDrive directory.
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

## Run directly for development

Python 3.12 is required. Choose an absolute data directory outside this repository and outside
OneDrive; the application refuses unsafe storage paths.

```text
python -m venv .venv
.venv\Scripts\activate
python -m pip install -e ".[dev]"
$env:PDF_BRIDGE_STORAGE_ROOT = "$env:LOCALAPPDATA\pdf-bridge-data"
$env:PDF_BRIDGE_SESSION_SECRET = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:PDF_BRIDGE_JOB_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(48))"
$env:PDF_BRIDGE_CLAMD_HOST = "localhost"
alembic upgrade head
uvicorn pdf_bridge.app:app --reload --no-access-log --no-proxy-headers
```

Direct development still needs a reachable `clamd` service (configured as `clamav:3310` by
default; set `PDF_BRIDGE_CLAMD_HOST=localhost` for a host-local daemon). Uploads fail closed when
the scanner is unavailable. Do not use Uvicorn
reload against real bridge data: the supported POC runtime is one application process.

## What the bridge owns

- The canonical scanned PDF and its SHA-256 checksum.
- The catalog, queue state, Jenkins batches, and append-only lifecycle audit trail.
- Human actions to upload, cancel pending work, retry failed work, and request deletion.
- A typed boundary to the external retrieval search service.

Jenkins owns the scheduled handoff. It claims an immutable batch, downloads each ingest PDF,
verifies byte count and SHA-256, atomically promotes the complete batch, then acknowledges staging.
After the external ingestion job finishes, Jenkins reports success or failure for every operation.
An ingested PDF is not shown as deleted until all required downstream removals are acknowledged.

## Jenkins in two commands

Install an exact released wheel on a controlled Jenkins agent and provide the service token through
the credentials store, never as a command-line argument. Pin the expected hostname separately from
the service URL:

```text
$env:PDF_BRIDGE_JOB_TOKEN = "<injected-by-Jenkins>"
$env:PDF_BRIDGE_JOB_ALLOWED_HOST = "pdf-bridge.internal"
pdf-bridge-job pull --base-url https://pdf-bridge.internal --allowed-host pdf-bridge.internal --destination D:\rag-handoff --request-id nightly-2026-07-10 --result-file pull-result.json

# The ingestion pipeline reads the staged batch manifest and writes report.json.
pdf-bridge-job report report.json --pull-result pull-result.json --base-url https://pdf-bridge.internal --allowed-host pdf-bridge.internal
```

`pull` uses a temporary sibling directory, verifies every file, writes `manifest.json`, and only
then renames the directory to `<destination>/<batch-id>`. Reusing a request ID is safe: a matching
existing directory is verified and re-acknowledged. Any mismatch fails loudly. See
[docs/jenkins.md](docs/jenkins.md) and [Jenkinsfile.example](Jenkinsfile.example).

## Import an existing library

Prepare a version 1 import manifest, validate it without changing state, then apply it:

```text
pdf-bridge import-manifest historical.json --source-root D:\approved-pdfs --dry-run
pdf-bridge import-manifest historical.json --source-root D:\approved-pdfs --apply
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
$env:PDF_BRIDGE_RUN_BROWSER_TESTS = "1"
python -m pytest tests/test_browser.py

# With a test clamd reachable at PDF_BRIDGE_CLAMD_HOST/PORT:
$env:PDF_BRIDGE_RUN_CLAMAV_TESTS = "1"
python -m pytest tests/test_clamav_integration.py
```

The normal test suite uses temporary storage and fake external providers. The ClamAV smoke test is
separate because it requires Docker and current signatures. Browser tests require
`python -m playwright install chromium` once on the development machine.
