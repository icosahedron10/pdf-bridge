# OSS review: Playwright and ClamAV

> Point-in-time review: **2026-07-12**. This is engineering due diligence, not legal
> advice. Re-check versions, support status, and terms before an external release or a
> production approval.

## Executive decision

| Component | Project use | Recommendation | Main condition |
| --- | --- | --- | --- |
| Playwright `1.61.0` | Development-only, opt-in Chromium browser tests | **Accept** | Approve and inventory the downloaded browser executable separately from the Apache-2.0 Python package; make the browser job explicit in CI so skips cannot look like coverage. |
| ClamAV `1.5.3` | Runtime malware gate for uploads and historical imports | **Accept for this POC** | Keep it isolated, current, fail-closed, and monitored. Treat it as one control, not proof that a PDF is safe. Own the short non-LTS upgrade window and GPL image-distribution obligations. |

The selected versions are current as of this review. Playwright `1.61.0` was published on
2026-06-29, and ClamAV `1.5.3` was published on 2026-07-01 as a security patch. No immediate
version change is recommended.

One repository-level issue should be resolved before calling this project open source: at review
time the repository has no project `LICENSE`, third-party notice, or SBOM. Publicly visible source
without a license is not automatically open source. Pick the project's license deliberately, then
record the dependencies and redistributed container/browser artifacts under their own terms.

## How the project actually uses them

### Playwright

- `pyproject.toml` pins `playwright==1.61.0` under the `dev` extra. It is not installed in the
  production image because `Dockerfile` installs only the base project dependencies.
- `tests/test_browser.py` uses the synchronous API and a headless default Chromium-family browser
  against a temporary Uvicorn server bound to `127.0.0.1`.
- Five end-to-end tests cover theme/accessibility behavior, upload and mobile navigation,
  duplicate handling, ingestion retry workflow, collection search boundaries, and confirmed
  deletion.
- Browser tests run only when `PDF_BRIDGE_RUN_BROWSER_TESTS=1`; otherwise pytest reports them as
  skipped. The test server uses the normal application with test providers, not the live ClamAV
  service.
- The browser visits only the locally started application. That matches the upstream warning that
  Playwright's test browser should consume trusted content.

### ClamAV

- `clamav.Dockerfile` derives from the official `clamav/clamav:1.5.3` Alpine image and changes
  `StreamMaxLength` to 64 MiB. The application upload ceiling is 50 MiB, and configuration rejects
  an upload ceiling larger than the configured stream ceiling.
- Compose keeps port `3310` internal, persists `/var/lib/clamav`, budgets 4 GiB of memory, and
  waits for `clamdscan --ping` before starting the application.
- `pdf_bridge/services/scanner.py` implements the documented NUL-framed `INSTREAM` protocol. It
  sends bytes rather than a host path, so ClamAV never mounts or walks canonical PDF storage.
- Upload and historical-import paths accept only `CLEAN`. Detections, scanner errors, malformed
  replies, timeouts, and unavailable service do not queue a document. Serving content also
  requires the recorded scan state to be clean.
- Unit tests cover clean/infected replies and protocol failures. The opt-in live test sends a
  small PDF-shaped clean fixture and an EICAR fixture to a real daemon.

These are good integration boundaries. The largest remaining gaps are operational: signature age
is not part of readiness, scanner limits are mostly implicit image defaults, and the ClamAV
container is not yet hardened as aggressively as the application container.

## Playwright review

### License and distribution

The Playwright Python repository is licensed under **Apache License 2.0**, including a patent
grant and conventional notice/redistribution duties. It is a low-friction license for this
development-only use. Preserve its license and notices if the wheel or driver is redistributed
through an internal artifact bundle rather than merely fetched as a dependency.

The browser download is a separate item. Starting with Playwright 1.57 on x64 targets, upstream
changed its default Chromium-family payload to Chrome for Testing/Chrome Headless Shell builds
(Arm64 Linux remained on Chromium). Google describes Chrome for Testing as a Chrome flavor, and
Chrome's executable terms are separate from the licenses covering most of its source. The
download is therefore not accurately represented by saying only "Playwright is Apache-2.0." For
this project, internal automated testing of a trusted local application is the browser's stated
purpose. Still, put **Chrome for Testing/Chrome Headless Shell** on the legal/procurement inventory
as its own component and confirm the applicable terms. If policy requires every executable to be
under an OSI license, resolve that requirement before approval rather than assuming the Playwright
license covers the browser.

### Maintenance and release posture

- `1.61.0` is the current PyPI release and is exactly pinned in this repository.
- Upstream ships frequently and couples each Playwright version to specific browser binaries. An
  upgrade normally requires rerunning `python -m playwright install`.
- The reviewed official pages do not publish a multi-year LTS window for Playwright. Treat that as
  a fast-moving toolchain: review release notes and refresh on a regular cadence instead of
  holding a browser build indefinitely.
- Microsoft provides an MSRC security-reporting path and asks that vulnerabilities not be filed
  as public GitHub issues.

The exact pin is appropriate. It gives repeatable browser behavior and prevents an unrelated
developer install from silently changing tests. It also means the team must own the update job.

### Security and update considerations

- Chrome for Testing and the Playwright browser images do not auto-update. Old copies accumulate
  browser vulnerabilities, so the package pin is a security pin as well as a compatibility pin.
- Keep the test target trusted and local. Do not reuse this browser job as a general crawler or
  point it at attacker-controlled URLs. Google and Microsoft both explicitly warn against using
  these test images for untrusted browsing.
- Install only the required browser in CI: `python -m playwright install --with-deps chromium`.
  If CI is strictly headless and the current tests remain compatible, evaluate `--only-shell` to
  reduce download size.
- For a restricted network, mirror approved browser artifacts and use
  `PLAYWRIGHT_DOWNLOAD_HOST`, or use the documented proxy/CA settings. Record hashes or the image
  digest in the build provenance.
- Do not cache browser binaries blindly. Upstream says cache restore time is often comparable to a
  fresh download; if caching is required, key it by the exact Playwright version.

### Deployment and operational tradeoffs

**Strengths**

- Web-first locators and assertions match how the server-rendered UI is used and already exercise
  accessibility names, focus, responsive navigation, browser storage, and JavaScript upload flows.
- Browser contexts are isolated and the library works on the project's Windows/Linux development
  targets.
- It stays out of the runtime image and production attack surface.

**Costs**

- The Python wheel does not contain a ready browser; the browser and Linux system libraries are a
  separate, sizable installation.
- Browser and library versions must match. A stale shared cache can fail before tests begin.
- The opt-in environment flag makes local work convenient but permits a green default pytest run
  with all browser coverage skipped.
- The suite currently validates one browser family. That is adequate for a POC whose deployment
  browser is Chromium-based, but it is not a cross-browser compatibility claim.

### Alternatives

- **Selenium** is the clearest OSS replacement: its project code and documentation are Apache-2.0,
  it has broad WebDriver/Grid support, and it may fit an organization that already owns a Selenium
  platform. For this small Python suite, switching would add driver/grid management without a
  demonstrated benefit.
- A hosted browser grid can remove local browser operations but adds a service, credentials, data
  handling, and cost. It is unnecessary while tests target a local POC.

**Recommendation:** retain Playwright. Add a dedicated required browser job (or a documented
manual release gate for the POC), keep the exact pin, and review the browser payload as a separate
license/security component.

## ClamAV review

### License and distribution

ClamAV is licensed under **GPL version 2**. Upstream also calls out separately licensed bundled
components, including a runtime-loaded UnRAR component with a restricted license. The current
architecture keeps ClamAV in a separate container and communicates over a documented TCP
protocol; it does not link ClamAV code into `pdf-bridge`. Running that separate service does not by
itself relicense the Python application.

Publishing `pdf-bridge-clamav:1.5.3-stream64m` is different from merely running it. It distributes
ClamAV binaries in a modified image, so preserve upstream notices and third-party license files and
make the exact corresponding source, including the image modification, available in a GPLv2-
compliant way. Have counsel confirm the delivery mechanism before publishing that image outside
the organization. An SBOM should identify the ClamAV engine, base distribution packages, signature
database snapshot (when present), and third-party components.

### Maintenance and release posture

- `1.5.3` is current and fixed multiple parser/memory-safety and dependency vulnerabilities that
  affect `1.5.2` and earlier releases. The project's rapid move to this patch is the correct
  posture for a scanner that parses hostile inputs.
- The `1.5` feature line is **not LTS**. Critical patch releases are expected until four months
  after `1.6` or until `1.7`; expected EOL and signature-download cutoff are four months after
  `1.7`. Those are event-based dates, so an owner must watch upstream announcements.
- `1.4` is the current LTS line; its critical-patch support runs through 2027-08-15 and signature
  downloads through 2028-08-15. Its current patch, `1.4.5`, contains the July 2026 security fixes.
  Do not roll back only for the LTS label: upstream's matrix also shows signature false-positive
  testing moved to the newer feature line when `1.5` shipped.

**Recommendation:** stay on `1.5.3` for the POC, subscribe to release/EOL notices, and evaluate
`1.6` promptly when it ships. If a production platform requires a fixed multi-year window, compare
`1.4.5` LTS and the then-current feature line in an acceptance environment and document the
chosen tradeoff.

### Security and update considerations

ClamAV itself parses untrusted bytes and is therefore an attack surface. The July 2026 release is
a concrete reminder: several fixed bugs were crashes or out-of-bounds writes reached by malformed
files. Keep the daemon isolated even though its purpose is security.

The project already gets several important things right:

- no host-published `3310` port;
- `INSTREAM` instead of daemon-visible paths;
- no canonical-storage mount in the scanner container;
- a finite application timeout and a 64 MiB stream ceiling;
- a persistent signature volume and a 4 GiB memory budget; and
- fail-closed behavior from upload through content serving.

Close these gaps before a controlled pilot:

1. **Freshness, not only PING.** The official container runs FreshClam, but its default check
   frequency is once per day. Set `FRESHCLAM_CHECKS` to an approved frequency (for example, four
   to six checks per day), alert on update errors, and expose engine/database version and age to
   operator monitoring. Official signatures are usually updated once or twice daily.
2. **Reject stale databases.** Set `FailIfCvdOlderThan` in `clamd.conf` to the organization's
   maximum age and prove that readiness goes unhealthy when the threshold is crossed. A `PONG`
   alone says nothing about detection currency.
3. **Make limit outcomes explicit.** Review `MaxScanSize`, `MaxFileSize`, `MaxRecursion`,
   `MaxFiles`, `MaxScanTime`, queue/thread limits, and temporary storage. Enable
   `AlertExceedsMax yes` unless policy deliberately accepts a partially inspected container as
   clean. Upstream documents that some over-limit content is otherwise skipped.
4. **Decide encrypted-PDF policy.** `AlertEncryptedDoc` can flag encrypted PDFs, but presenting
   them as malware may be misleading. Prefer an explicit product policy and clear rejection path;
   test the chosen behavior.
5. **Harden the scanner container.** Trial the official unprivileged entrypoint (`clamav` user),
   `no-new-privileges`, dropped capabilities, a read-only root, and bounded writable tmpfs/volume
   mounts. The signature volume must remain writable. Apply CPU/PID and temporary-space limits and
   load-test 50 MiB and pathological PDFs before committing values.
6. **Restrict protocol commands and network reachability.** `clamd` TCP is unauthenticated and
   unencrypted. Keep it on the private Compose network, add network policy in an orchestrated
   deployment, and disable unused administrative commands after confirming FreshClam reloads still
   work.
7. **Track immutable artifacts.** `clamav/clamav:1.5.3` pins the engine patch but is not a fully
   immutable byte-for-byte reference: upstream may refresh the non-base tag's signature database
   and may publish base-image rebuilds for dependency CVEs. Record the resolved digest/SBOM. A
   `<patch>_base` image plus the existing persistent signature volume is worth evaluating, but
   digest pins still need an owned refresh procedure.

### Deployment and operational tradeoffs

**Strengths**

- Mature, open engine with a Cisco Talos-maintained, digitally signed signature feed.
- Straightforward daemon protocol and an official container, with no need to give the scanner
  access to application storage.
- Deterministic allow/deny integration and a useful EICAR acceptance test.

**Costs and limits**

- Roughly 3 GiB minimum/4 GiB preferred memory is material for a POC; database reload can briefly
  hold two engines.
- First start depends on signature initialization and network access and can take minutes.
- Signature detection has false positives and false negatives. `CLEAN` means no configured current
  rule fired; it does not make parser execution safe.
- Parser bugs, decompression bombs, encrypted files, update outages, and signature-age drift all
  require separate controls and monitoring.
- The GPLv2 image and its third-party payload need more distribution diligence than an ordinary
  permissively licensed Python dependency.

### Alternatives and complements

- **YARA-X** is a BSD-3-Clause, production-stable pattern matcher with Python bindings. It can add
  organization-specific rules, but it does not replace ClamAV's maintained antivirus signature
  feed or its file-format scanning behavior.
- A managed multi-engine scanning service or content-disarm-and-reconstruction service may better
  meet enterprise policy, but it adds cost, data-transfer/privacy review, network dependency, and
  vendor lock-in. Evaluate it as a policy decision, not a drop-in OSS library swap.
- Sandboxed downstream PDF parsing remains necessary regardless of scanner choice.

**Recommendation:** retain ClamAV as the POC's first-pass malware gate, implement freshness and
limit policy, and keep the documented enterprise gate that requires an organizational decision on
augmentation or replacement.

## Monday-ready checklist

### Before a public GitHub/OSS claim

- [ ] Select and add the project's own `LICENSE`.
- [ ] Add an SBOM or third-party inventory covering Python dependencies, Playwright's downloaded
      browser, the ClamAV image/base packages, and ClamAV's bundled third-party components.
- [ ] Decide whether any built ClamAV image will be published; if yes, implement the reviewed GPLv2
      source-and-notice delivery process.
- [ ] Record legal approval for Chrome for Testing terms, or select an approved browser artifact.

### Before calling the test suite release-gating

- [ ] Install the exact browser for `playwright==1.61.0` and run the five browser tests.
- [ ] Put browser tests in a named CI/release job where zero selected tests or unexpected skips
      fail the job.
- [ ] Capture a trace/screenshot on browser-test failure and retain it as a short-lived artifact
      with sensitive-data handling rules.
- [ ] Assign a monthly Playwright release-note/browser refresh owner.

### Before a controlled ClamAV pilot

- [ ] Pull/build `1.5.3`, record the upstream and derived image digests, and scan the images/SBOM.
- [ ] Run the live clean/EICAR test with current signatures.
- [ ] Prove upload/import fail closed for timeout, daemon error, stale signatures, over-limit scan,
      and encrypted PDF according to the chosen policy.
- [ ] Alert on FreshClam failures, database age, daemon readiness, memory/OOM, queue saturation,
      scan latency/timeouts, and repeated protocol errors.
- [ ] Test non-root/container hardening and private network policy.
- [ ] Assign an owner to review ClamAV security releases immediately and its EOL matrix at least
      monthly.

## Verification performed for this review

- Local metadata and CLI both reported Playwright `1.61.0`.
- `docker compose config --quiet` passed with review-only placeholder secrets and a valid temporary
  collection registry.
- With `PDF_BRIDGE_RUN_BROWSER_TESTS=1`, all five Playwright tests passed locally in 15.55 seconds.
- The live ClamAV integration test was collected and skipped as designed because
  `PDF_BRIDGE_RUN_CLAMAV_TESTS` was not set and no daemon was running.
- Docker Desktop was not running, so this review did **not** pull, inspect, scan, or execute the
  `clamav/clamav:1.5.3` image. Complete the live checklist before deployment approval.

## Official sources

### Playwright and browser payload

- [Playwright Python package and release history](https://pypi.org/project/playwright/)
- [Playwright Python release notes](https://playwright.dev/python/docs/release-notes)
- [Playwright Python Apache-2.0 license](https://github.com/microsoft/playwright-python/blob/main/LICENSE)
- [Playwright security reporting policy](https://github.com/microsoft/playwright-python/blob/main/SECURITY.md)
- [Browser installation, version coupling, artifact mirrors, and disk use](https://playwright.dev/python/docs/browsers)
- [Playwright CI guidance](https://playwright.dev/python/docs/ci)
- [Playwright Docker guidance and untrusted-site warning](https://playwright.dev/python/docs/docker)
- [Chrome for Testing purpose and non-auto-updating design](https://developer.chrome.com/blog/chrome-for-testing)
- [Chrome for Testing security threat model](https://chromium.googlesource.com/chromium/src/+/main/docs/security/faq.md#what-is-the-threat-model-for-chrome-for-testing)
- [Google Chrome and ChromeOS additional terms](https://www.google.com/chrome/terms/)
- [Selenium licensing](https://www.selenium.dev/documentation/about/copyright/)

### ClamAV

- [ClamAV 1.5.3 and 1.4.5 security release announcement](https://blog.clamav.net/2026/07/clamav-153-and-145-security-patch.html)
- [ClamAV EOL policy and support matrix](https://docs.clamav.net/faq/faq-eol.html)
- [ClamAV GPLv2 and third-party licensing overview](https://github.com/Cisco-Talos/clamav#licensing)
- [ClamAV GPLv2 text](https://github.com/Cisco-Talos/clamav/blob/main/COPYING.txt)
- [ClamAV security policy](https://github.com/Cisco-Talos/clamav/blob/main/SECURITY.md)
- [Official Docker images, tags, memory, FreshClam, and non-root operation](https://docs.clamav.net/manual/Installing/Docker.html)
- [`clamd` protocol and TCP warning](https://docs.clamav.net/manual/Usage/ClamdProtocol.html)
- [`1.5.3` `clamd.conf` options and defaults](https://github.com/Cisco-Talos/clamav/blob/clamav-1.5.3/etc/clamd.conf.sample)
- [Signature database update behavior](https://docs.clamav.net/faq/faq-cvd.html)
- [FreshClam configuration guidance](https://docs.clamav.net/manual/Usage/Configuration.html#freshclamconf)
- [YARA-X overview](https://virustotal.github.io/yara-x/)
- [YARA-X BSD-3-Clause repository](https://github.com/VirusTotal/yara-x)
