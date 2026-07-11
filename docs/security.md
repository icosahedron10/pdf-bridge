# Security model and enterprise gates

Uploading a PDF into a parser and retrieval system crosses a serious trust boundary. This POC
implements baseline controls from the
[OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html),
but it is not evidence that arbitrary PDFs are safe. Keep the service on a restricted internal
network until your security team approves the complete path.

## POC controls

- Only `.pdf` display names are accepted, and path separators/control characters are rejected.
- Uploads stream to generated names outside the webroot with byte limits; file bytes are never
  loaded wholesale into application memory.
- The leading PDF signature is checked without invoking a parser.
- A server-calculated SHA-256 identifies exact active duplicates.
- Every upload and historical import is scanned through ClamAV `INSTREAM`; scanner errors fail
  closed and unclean files are not promoted.
- Canonical paths derive only from UUIDs. User filenames are metadata.
- Browser mutations require a signed session, same-origin request, and CSRF token. CORS is absent.
- Trusted-host checks reject unexpected Host headers.
- The container disables Uvicorn's forwarded-header rewriting; trusted-header mode checks the
  direct peer against `TRUSTED_PROXY_CIDRS` before accepting the identity header.
- Jenkins and retrieval use separate bearer credentials supplied through environment/secrets.
- Batch downloads require both a valid job credential and an active batch scope.
- Preview is application-controlled and limited to clean, eligible states with defensive headers.
- Lifecycle events are append-only at the ORM layer and exclude file content and credentials.
- The official container runs the app as a non-root user, drops Linux capabilities, uses a
  read-only root filesystem, and mounts only its data volume writable.

These controls reduce common mistakes; they are not a content-disarm system, sandbox, DLP product,
or authorization model.

## Important residual risks

**Parser compromise.** A clean ClamAV result means only that current signatures did not identify a
threat. A malformed, novel, encrypted, or resource-exhausting PDF may still exploit or stall the
downstream parser. The bridge deliberately never parses PDFs.

**Anonymous access.** `anonymous-poc` distinguishes browser sessions for audit readability but does
not authenticate a person. Anyone who can reach the service can view and mutate the POC library.

**Single bearer token.** The Jenkins token is service-wide. If disclosed, it can claim files and
report lifecycle changes. Scope it at the network and secret-manager layers and rotate it promptly.

**SQLite and one process.** SQLite provides no per-service tenancy and is not a highly available
control plane. A stolen data volume contains the catalog and all canonical PDFs.

**Filename and document sensitivity.** Logs avoid contents and local paths, but filenames, search
queries, snippets, error messages, and audit actors may still be confidential.

## Mandatory enterprise gate

Do not call the deployment enterprise-ready until owners from application security,
infrastructure, identity, data governance, and the retrieval pipeline approve each item:

- [ ] Put the app behind organization-managed TLS; redirect/disable plaintext and validate proxy
      forwarding behavior.
- [ ] Configure enterprise SSO at a reverse proxy and use `PDF_BRIDGE_AUTH_MODE=trusted-header`.
- [ ] Restrict direct app access so only configured proxy CIDRs can reach it or assert identity.
- [ ] Add authorization policy (library audience, uploaders, deleters, administrators) if all SSO
      users should not have identical rights.
- [ ] Store session, Jenkins, and retrieval credentials in the approved secret manager; define
      owners, rotation, revocation, and incident procedures.
- [ ] Complete threat modeling and security review for browser, bridge, Jenkins, storage,
      retrieval API, Qdrant, backups, and administrative import paths.
- [ ] Replace/augment ClamAV according to the organization's malware, content-disarm, encrypted
      document, and signature freshness policies.
- [ ] Run parsing in a least-privilege disposable sandbox with CPU, memory, time, process, and
      network limits. Patch parser libraries through an owned vulnerability process.
- [ ] Decide whether encrypted/password-protected PDFs are rejected before parsing and give users a
      safe failure explanation.
- [ ] Move metadata to managed PostgreSQL before multiple replicas/HA; test migrations, backups,
      point-in-time recovery, locking, and least-privilege database roles.
- [ ] Put PDFs and backups on approved encrypted durable storage with access logging, retention,
      legal-hold, recovery, and verified deletion controls.
- [ ] Establish upload/search/audit retention and privacy rules; prevent sensitive snippets or
      queries from entering broad logs or Jenkins artifacts.
- [ ] Add rate limits, request-body limits at the proxy, capacity alerts, malware-signature age
      alerts, dependency monitoring, and operational ownership.
- [ ] Validate all service egress destinations and certificates. Do not permit a response-provided
      download URL to redirect Jenkins credentials off origin.
- [ ] Run SAST, dependency/container scanning, DAST, and a focused penetration test against the
      approved deployment topology.

The application itself refuses `PDF_BRIDGE_APP_ENV=enterprise` when authentication is anonymous,
the development session secret remains, or no trusted proxy network is configured. That startup
guard is only a backstop; it does not complete the checklist.

## ClamAV operations

Compose builds a minimal layer from the official exact `clamav/clamav:1.5.3` image, sets clamd's
`StreamMaxLength` to 64 MiB so it exceeds the 50 MiB application limit, and persists signatures at
`/var/lib/clamav`. ClamAV recommends a persistent database volume and notes that signature loading
can require more than 2 GiB; this topology budgets 4 GiB. See the
[official ClamAV Docker guide](https://docs.clamav.net/manual/Installing/Docker.html).

- Review release notes and test before changing the pinned feature/patch tag.
- Monitor FreshClam logs, update failures, signature timestamps, and clamd readiness.
- Never publish port 3310 outside the private container/network boundary.
- The app streams bytes to clamd, so the scanner container does not mount canonical storage.
- Treat repeated scanner protocol errors as a security-impacting outage, not an invitation to
  bypass scanning.

## Logging and incident handling

Central logs may include request ID, route, status, actor pseudonym/identity, document/operation
UUID, event type, duration, and bounded pipeline error. They must not include PDF bytes, bearer
tokens, session/CSRF values, full local paths, or retrieval snippets.

On suspected malicious upload or credential compromise:

1. restrict access and stop new claims/uploads without deleting evidence;
2. revoke/rotate affected credentials;
3. record relevant request, document, operation, batch, and pipeline-run IDs;
4. preserve approved forensic copies of logs/catalog/storage under incident policy;
5. investigate downstream parser hosts and indexes, not only the bridge;
6. follow the organization's incident response and disclosure process;
7. restore service only after the bypass/root cause and signature state are understood.

Do not manually rewrite audit rows or lifecycle state during response. Use documented transitions
or a reviewed repair migration that preserves the original evidence.
