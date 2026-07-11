# Configuration reference

Configuration uses environment variables with the `PDF_BRIDGE_` prefix. The application validates
configuration at startup and fails rather than selecting an unsafe storage fallback.

## Supported platform

Linux is the only supported host operating system. Direct installations and Jenkins agents must
use Linux, POSIX paths, and a POSIX shell. Docker Compose is supported on a Linux host with Docker
Engine and Compose. Windows and macOS hosts and agents are not supported deployment targets.

## Required POC settings

| Variable | Purpose | POC value |
|---|---|---|
| `PDF_BRIDGE_STORAGE_ROOT` | Absolute canonical data directory | `/var/lib/pdf-bridge` in Compose |
| `PDF_BRIDGE_SESSION_SECRET` | Signs pseudonymous browser sessions | unique random value, at least 32 characters |
| `PDF_BRIDGE_JOB_TOKEN` | Authenticates Jenkins API calls | separate random value, at least 32 characters |
| `PDF_BRIDGE_ALLOWED_HOSTS` | Accepted HTTP Host values, encoded as JSON | `["localhost","127.0.0.1"]` |
| `PDF_BRIDGE_COLLECTIONS` | Ordered JSON collection registry; there is no default | see example below |

Generate secrets independently and keep them in a secret manager or local untracked `.env` file:

```text
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Do not reuse the session secret as the Jenkins token. Changing the session secret signs users out;
changing the job token requires updating Jenkins at the same time.

## Collection registry

Define every corpus boundary explicitly in one JSON array:

```text
PDF_BRIDGE_COLLECTIONS=[{"key":"customer","display_name":"Customer Product","description":"Approved product and support material available to customer-facing assistants.","audience":"customer"},{"key":"internal","display_name":"HR & Internal","description":"Employee-only policy and operations material.","audience":"internal"}]
```

The array must contain 1–50 collections and keys must be unique. Each object has exactly these fields:

| Field | Rule |
|---|---|
| `key` | 1–63 lowercase ASCII letters, digits, hyphens, or underscores; starts with a letter or digit |
| `display_name` | nonblank operator-facing name, maximum 255 characters |
| `description` | nonblank explanation of what belongs in the corpus, maximum 2,000 characters |
| `audience` | exactly `customer` or `internal` |

The key is a cross-system identifier, not merely a label: it is the PDF Bridge collection key, the
Qdrant collection name, and the chatbot manager's `allowed_collections` value. Treat renaming as a
controlled corpus migration. Uploads have no default destination, and a document's collection is
immutable once queued; move it only through downstream-confirmed deletion and re-upload.

The audience makes **Customer-facing** versus **Internal only** visible in PDF Bridge, but it is not
chatbot authorization. The chatbot manager must derive its allowlist from authenticated server-side
policy and intersect every requested collection list before retrieval. PDF Bridge refuses startup
when an active catalog row references an unconfigured key or is unassigned outside the Needs Review
state.

## Application and identity

| Variable | Default | Notes |
|---|---:|---|
| `PDF_BRIDGE_APP_ENV` | `development` | `development`, `test`, or `enterprise` |
| `PDF_BRIDGE_AUTH_MODE` | `anonymous-poc` | `anonymous-poc` or `trusted-header` |
| `PDF_BRIDGE_TRUSTED_PROXY_CIDRS` | `[]` | JSON list of proxies allowed to assert identity |
| `PDF_BRIDGE_TRUSTED_IDENTITY_HEADER` | `X-Forwarded-User` | header inserted by the trusted SSO proxy |

`enterprise` refuses to start with anonymous access, the development session secret, or no trusted
proxy CIDRs. Do not expose trusted-header mode directly to clients: the named identity header is
authoritative only when the immediate peer address belongs to a configured CIDR.

## Files and database

| Variable | Default | Notes |
|---|---:|---|
| `PDF_BRIDGE_DATABASE_URL` | SQLite under storage root | SQLAlchemy URL; leave unset for POC |
| `PDF_BRIDGE_MAX_UPLOAD_BYTES` | `52428800` | 50 MiB per file |
| `PDF_BRIDGE_MAX_UPLOAD_FILES` | `20` | files per browser selection, maximum 100 |
| `PDF_BRIDGE_UPLOAD_CHUNK_BYTES` | `1048576` | streaming read size |

The storage root must be absolute in deployment, outside the source tree, outside a webroot, and
outside any path whose component begins with `OneDrive`. It needs durable capacity for canonical
PDFs plus SQLite and temporary headroom for concurrent uploads. Do not share one SQLite catalog
between multiple app containers. If an explicit SQLite URL is supplied, its file path must be
absolute and resolve beneath the storage root; in-memory SQLite is accepted only in test mode.

To use PostgreSQL later, set a PostgreSQL SQLAlchemy URL, install the approved driver, run Alembic
migrations, and test transaction/locking behavior before cutover. Merely changing the URL is not a
production migration plan.

Canonical bridge objects remain under UUID-derived `objects/` keys regardless of collection or
language. The version 2 Jenkins handoff and downstream RAG store—not the canonical store—use
`pdfs/{language}/{collection_key}/{document_id}.pdf`.

## Malware scanner

| Variable | Default | Notes |
|---|---:|---|
| `PDF_BRIDGE_CLAMD_HOST` | `clamav` | TCP host reachable from the app |
| `PDF_BRIDGE_CLAMD_PORT` | `3310` | clamd port |
| `PDF_BRIDGE_CLAMD_TIMEOUT` | `30` | connection and scan timeout in seconds |
| `PDF_BRIDGE_CLAMD_STREAM_MAX_BYTES` | `67108864` | must match/exceed clamd `StreamMaxLength` and the upload limit |

The app uses clamd `INSTREAM`; ClamAV does not need access to the bridge storage volume. Scanner
timeouts, protocol errors, and malware findings all prevent canonical promotion.

## Jenkins and retrieval

| Variable | Default | Notes |
|---|---:|---|
| `PDF_BRIDGE_CLAIM_LEASE_MINUTES` | `30` | 1–1440; exceed worst-case download duration |
| `PDF_BRIDGE_SEARCH_API_URL` | unset | typed retrieval endpoint base URL |
| `PDF_BRIDGE_SEARCH_API_TOKEN` | unset | required with the search URL; distinct, at least 32 characters |
| `PDF_BRIDGE_SEARCH_API_TIMEOUT` | `10` | retrieval request timeout in seconds |

The configured service receives `POST {PDF_BRIDGE_SEARCH_API_URL}/search`. Count-only requests may
span configured collections; hit-producing requests contain exactly one collection and may filter
to `en` or `fr`. Responses must echo the query/mode/language and return exactly one group, with an
exact total, for every requested key—including zero totals. PDF Bridge rejects malformed,
missing/extra-group, cross-collection, wrong-language, inactive-document, or impossible-total
responses in full. It never falls back to catalog filename search.

There is no bridge-side language detector setting. Clean uploads begin as `und` and the existing
downstream PDF parser classifies extracted content asynchronously as `en`, `fr`, or
`review_required`. Do not install Node, V8, PDF.js, or `franc` in PDF Bridge for this purpose.

The Jenkins client separately reads:

- `PDF_BRIDGE_URL` for the bridge URL;
- `PDF_BRIDGE_JOB_ALLOWED_HOST` for the exact hostname permitted to receive the bearer token;
- `PDF_BRIDGE_JOB_TOKEN` for the bearer token, or `--token-file` for file credentials.

The allowed host is mandatory and must be configured independently of the URL; it accepts no
scheme, port, path, or credentials. Use HTTPS outside loopback. A private CA can be supplied to the
CLI with `--ca-bundle`. The client refuses non-loopback HTTP unless `--allow-http` is explicit and
visible in the job definition. Jenkins should keep both URL and host pin as SCM-reviewed constants,
never trigger-supplied parameters.

## Compose-specific settings

`PDF_BRIDGE_BIND_ADDRESS` defaults Compose to loopback-only publication; set it only to an
approved internal interface when another machine must reach the POC. `PDF_BRIDGE_PORT` selects
the host port and is not read by the Python application. Compose fixes
the in-container storage root and clamd address so they cannot accidentally point into the source
checkout. Its single-process entrypoint runs `alembic upgrade head` before Uvicorn and fails startup
if migration fails. Its container health check uses the readiness endpoint (database, storage, and
ClamAV), and `/tmp` is an in-memory 256 MiB filesystem rather than persistent application storage.
List values in `.env` must remain valid JSON arrays.
