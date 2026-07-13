(function () {
  "use strict";

  const form = document.querySelector("[data-upload-form]");
  if (!form) return;

  const fileInput = form.querySelector("[data-file-input]");
  const dropZone = document.getElementById("drop-zone");
  const dropHelp = form.querySelector("[data-drop-help]");
  const collectionChoices = Array.from(form.querySelectorAll("[data-collection-choice]"));
  const selection = document.getElementById("upload-selection");
  const list = document.getElementById("upload-list");
  const rowTemplate = document.getElementById("upload-row-template");
  const countLabel = document.getElementById("selected-file-count");
  const summary = document.getElementById("upload-summary");
  const startButton = document.getElementById("start-upload");
  const clearButton = form.querySelector("[data-clear-files]");
  const restoreStatus = form.querySelector("[data-restore-status]");
  const formError = document.getElementById("upload-form-error");
  const formErrorMessage = formError.querySelector("[data-upload-error-message]");
  const liveRegion = document.getElementById("live-region");
  const csrfToken = form.querySelector('input[name="csrf_token"]')?.value || "";
  const preflightUrl = form.dataset.preflightUrl;
  const uploadUrl = form.dataset.uploadUrl;
  const openUploadsUrl = form.dataset.openUploadsUrl;
  const maxFiles = Number.parseInt(form.dataset.maxFiles || "20", 10);
  const maxBytes = Number.parseInt(form.dataset.maxBytes || "52428800", 10);
  const pollInterval = Number.parseInt(form.dataset.pollInterval || "1500", 10);
  const configuredRequestTimeout = Number.parseInt(form.dataset.requestTimeout || "30000", 10);
  const requestTimeout = Number.isFinite(configuredRequestTimeout) && configuredRequestTimeout > 0
    ? configuredRequestTimeout
    : 30000;
  const requestPoolSize = 3;
  const requestQueue = [];
  const items = new Map();
  const itemsByUploadId = new Map();
  const temporarilyFocused = new WeakSet();
  let activeRequestCount = 0;
  let uploading = false;
  let polling = false;
  let pollTimer = null;

  const failedStates = new Set([
    "INGEST_FAILED",
    "REPLACE_FAILED",
    "DELETE_FAILED",
    "CLEANUP_FAILED"
  ]);
  const terminalStates = new Set(["INGESTED", "REJECTED", "CANCELLED", "DELETED"]);

  function selectedCollection() {
    const selected = collectionChoices.find(function (choice) { return choice.checked; });
    if (!selected) return null;
    return {
      key: selected.value,
      name: selected.dataset.collectionName || selected.value
    };
  }

  function announce(message) {
    if (!liveRegion) return;
    liveRegion.textContent = "";
    window.setTimeout(function () { liveRegion.textContent = message; }, 20);
  }

  function createId() {
    if (window.crypto?.randomUUID) return window.crypto.randomUUID();
    if (window.crypto?.getRandomValues) {
      const values = new Uint32Array(4);
      window.crypto.getRandomValues(values);
      return Array.from(values, function (value) {
        return value.toString(16).padStart(8, "0");
      }).join("-");
    }
    return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes)) return "Size unknown";
    if (bytes < 1024) return bytes + (bytes === 1 ? " byte" : " bytes");
    const units = ["KiB", "MiB", "GiB"];
    let value = bytes / 1024;
    let unit = units[0];
    for (let index = 1; index < units.length && value >= 1024; index += 1) {
      value /= 1024;
      unit = units[index];
    }
    return value.toLocaleString(undefined, {
      maximumFractionDigits: value >= 10 ? 1 : 2
    }) + " " + unit;
  }

  function displayValue(value) {
    return String(value || "unknown").replaceAll("_", " ").toLowerCase();
  }

  function titleValue(value) {
    return displayValue(value).replace(/\b\w/g, function (letter) {
      return letter.toUpperCase();
    });
  }

  function setStatusLabel(node, value) {
    const normalized = String(value || "UNKNOWN").toUpperCase();
    node.className = "status status--" + normalized.toLowerCase().replaceAll("_", "-");
    node.textContent = titleValue(normalized);
    node.hidden = false;
  }

  function showFormError(message) {
    formErrorMessage.textContent = message || "";
    formError.hidden = !message;
    if (message) announce(message);
  }

  function requestHeaders(options) {
    const settings = options || {};
    const headers = { Accept: "application/json, application/problem+json" };
    if (settings.json) headers["Content-Type"] = "application/json";
    if (settings.idempotencyKey) headers["Idempotency-Key"] = settings.idempotencyKey;
    if (csrfToken) headers["X-CSRF-Token"] = csrfToken;
    return headers;
  }

  function drainRequestQueue() {
    while (activeRequestCount < requestPoolSize && requestQueue.length > 0) {
      const request = requestQueue.shift();
      activeRequestCount += 1;
      Promise.resolve()
        .then(request.task)
        .then(request.resolve, request.reject)
        .finally(function () {
          activeRequestCount -= 1;
          drainRequestQueue();
        });
    }
  }

  function scheduleRequest(task) {
    return new Promise(function (resolve, reject) {
      requestQueue.push({ task: task, resolve: resolve, reject: reject });
      drainRequestQueue();
    });
  }

  function fetchWithTimeout(url, options) {
    return scheduleRequest(async function () {
      const controller = new AbortController();
      let timedOut = false;
      const timer = window.setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, requestTimeout);
      try {
        return await fetch(url, {
          ...(options || {}),
          signal: controller.signal
        });
      } catch (error) {
        if (timedOut) {
          throw new Error("The request timed out. Try again.");
        }
        throw error;
      } finally {
        window.clearTimeout(timer);
      }
    });
  }

  async function readProblem(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("json")) {
      const problem = await response.json().catch(function () { return null; });
      if (typeof problem?.detail === "string") return problem.detail;
      if (typeof problem?.message === "string") return problem.message;
      if (typeof problem?.title === "string") return problem.title;
    }
    if (response.status === 503) {
      return "A required analysis or storage service is unavailable. The durable work can be retried.";
    }
    return "The request failed with status " + response.status + ".";
  }

  async function fetchJson(url, options) {
    const response = await fetchWithTimeout(url, options);
    if (!response.ok) throw new Error(await readProblem(response));
    try {
      return await response.json();
    } catch (_error) {
      throw new Error("The server returned an invalid JSON response.");
    }
  }

  function parsePayload(text) {
    if (!text) return {};
    try { return JSON.parse(text); }
    catch (_error) { return {}; }
  }

  function safeDocumentPath(candidate, documentId) {
    if (candidate) {
      try {
        const url = new URL(candidate, window.location.origin);
        if (url.origin === window.location.origin) {
          return url.pathname + url.search + url.hash;
        }
      } catch (_error) {
        // Fall back to the catalog identifier below.
      }
    }
    return documentId ? "/documents/" + encodeURIComponent(documentId) : "/library";
  }

  function fileIdentity(file) {
    return [file.name, file.size, file.lastModified].join("::");
  }

  function isDismissible(item) {
    return item.status === "complete" || item.status === "blocked";
  }

  function eligibleForUpload(item) {
    return Boolean(item.file) && (item.status === "ready" || item.status === "upload-failed");
  }

  function updateCollectionControls() {
    const collection = selectedCollection();
    const localSelectionLocked = Array.from(items.values()).some(function (item) {
      return Boolean(item.file) && !item.uploadId;
    });
    collectionChoices.forEach(function (choice) {
      choice.disabled = localSelectionLocked || uploading;
    });
    fileInput.disabled = !collection || uploading;
    dropZone.classList.toggle("is-disabled", !collection || uploading);
    dropZone.setAttribute("aria-disabled", String(!collection || uploading));
    if (dropHelp) dropHelp.textContent = collection ? "PDF files only" : "Choose a collection first";
  }

  function updateControls() {
    const allItems = Array.from(items.values());
    selection.hidden = allItems.length === 0;
    countLabel.textContent = allItems.length ? "(" + allItems.length + ")" : "";

    const counts = allItems.reduce(function (totals, item) {
      if (item.status === "checking") totals.checking += 1;
      else if (item.status === "ready") totals.ready += 1;
      else if (item.status === "uploading") totals.uploading += 1;
      else if (item.status === "active") totals.active += 1;
      else if (item.status === "review") totals.review += 1;
      else if (item.status === "failed" || item.status === "upload-failed") totals.failed += 1;
      else if (item.status === "complete") totals.complete += 1;
      else if (item.status === "blocked") totals.blocked += 1;
      return totals;
    }, {
      checking: 0,
      ready: 0,
      uploading: 0,
      active: 0,
      review: 0,
      failed: 0,
      complete: 0,
      blocked: 0
    });

    const parts = [];
    if (counts.ready) parts.push(counts.ready + " ready to upload");
    if (counts.checking) parts.push(counts.checking + " checking");
    if (counts.uploading) parts.push(counts.uploading + " uploading");
    if (counts.active) parts.push(counts.active + " processing");
    if (counts.review) parts.push(counts.review + " need a decision");
    if (counts.failed) parts.push(counts.failed + " failed");
    if (counts.complete) parts.push(counts.complete + " finished");
    if (counts.blocked) parts.push(counts.blocked + " blocked");
    summary.textContent = parts.join(" · ");

    const eligible = allItems.filter(eligibleForUpload);
    startButton.disabled = uploading || eligible.length === 0;
    startButton.textContent = eligible.some(function (item) { return item.status === "upload-failed"; })
      ? "Retry ready files"
      : "Upload ready files";
    clearButton.disabled = uploading || !allItems.some(isDismissible);
    allItems.forEach(function (item) {
      item.removeButton.disabled = uploading || item.status === "uploading";
    });
    updateCollectionControls();
  }

  function setItemState(item, status, message) {
    item.status = status;
    item.row.classList.remove(
      "is-ready",
      "is-error",
      "is-blocked",
      "is-complete",
      "is-uploading",
      "is-review"
    );
    const classByStatus = {
      ready: "is-ready",
      failed: "is-error",
      "upload-failed": "is-error",
      blocked: "is-blocked",
      complete: "is-complete",
      uploading: "is-uploading",
      active: "is-uploading",
      review: "is-review"
    };
    if (classByStatus[status]) item.row.classList.add(classByStatus[status]);
    item.statusNode.textContent = message;
    updateControls();
  }

  function createRow(options) {
    const localId = options.localId || createId();
    const fragment = rowTemplate.content.cloneNode(true);
    const row = fragment.querySelector("[data-upload-item]");
    row.dataset.localUploadId = localId;
    row.querySelector("[data-file-name]").textContent = options.filename;
    row.querySelector("[data-file-size]").textContent = formatBytes(options.sizeBytes);
    list.append(row);

    const item = {
      id: localId,
      idempotencyKey: options.idempotencyKey || createId(),
      identity: options.file ? fileIdentity(options.file) : "",
      file: options.file || null,
      collectionKey: options.collectionKey,
      collectionName: options.collectionName || options.collectionKey,
      row: row,
      status: options.file ? "checking" : "active",
      uploadId: null,
      statusUrl: null,
      analysisUrl: null,
      analysisRevision: null,
      analysisLoading: false,
      focusWhenReviewReady: false,
      pollingEnabled: false,
      decisionKey: null
    };
    item.statusNode = row.querySelector("[data-file-status]");
    item.phaseNode = row.querySelector("[data-file-phase]");
    item.progress = row.querySelector("[data-file-progress]");
    item.removeButton = row.querySelector("[data-remove-file]");
    item.retryButton = row.querySelector("[data-retry-upload]");
    item.cancelButton = row.querySelector("[data-cancel-upload]");
    item.documentLink = row.querySelector("[data-document-link]");
    item.rowActions = row.querySelector("[data-row-actions]");
    item.review = row.querySelector("[data-analysis-review]");
    item.decisionForm = row.querySelector("[data-decision-form]");
    item.replacementFields = row.querySelector("[data-replacement-fields]");
    item.replacementTarget = row.querySelector("[data-replacement-target]");
    item.replacementConfirm = row.querySelector("[data-replacement-confirm]");
    item.decisionSubmit = row.querySelector("[data-submit-decision]");
    item.decisionError = row.querySelector("[data-decision-error]");
    item.progress.setAttribute("aria-label", "Upload progress for " + options.filename);
    item.removeButton.setAttribute("aria-label", "Remove " + options.filename);
    items.set(localId, item);

    item.removeButton.addEventListener("click", function () {
      if (uploading || item.status === "uploading" || item.uploadId) return;
      removeItem(item);
      announce(options.filename + " removed from the workspace");
    });
    item.retryButton.addEventListener("click", function () {
      if (item.uploadId) retryWork(item);
      else runPreflight(item);
    });
    item.cancelButton.addEventListener("click", function () { cancelWork(item); });
    item.decisionForm.addEventListener("change", function () { updateDecisionControls(item); });
    item.decisionForm.addEventListener("submit", function (event) {
      event.preventDefault();
      submitDecision(item);
    });
    return item;
  }

  function removeItem(item) {
    items.delete(item.id);
    if (item.uploadId) itemsByUploadId.delete(item.uploadId);
    item.row.remove();
    updateControls();
  }

  function renderFilenameWarnings(item, warnings) {
    const panel = item.row.querySelector("[data-filename-warning]");
    const warningList = item.row.querySelector("[data-filename-warning-list]");
    warningList.replaceChildren();
    warnings.forEach(function (warning) {
      const listItem = document.createElement("li");
      const match = warning.matched || {};
      const link = document.createElement("a");
      link.href = safeDocumentPath(match.detail_url, match.document_id);
      link.textContent = match.filename || "Existing document";
      listItem.append(link);
      const percent = Number.isFinite(warning.similarity)
        ? " · " + Math.round(warning.similarity * 100) + "% " + displayValue(warning.kind)
        : "";
      listItem.append(document.createTextNode(percent));
      if (warning.shared_tokens?.length) {
        listItem.append(document.createTextNode(" · " + warning.shared_tokens.join(", ")));
      }
      warningList.append(listItem);
    });
    panel.hidden = warnings.length === 0;
  }

  async function runPreflight(item) {
    item.retryButton.hidden = true;
    item.rowActions.hidden = true;
    setItemState(item, "checking", "Checking filename and size…");
    try {
      const response = await fetchWithTimeout(preflightUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: requestHeaders({ json: true }),
        body: JSON.stringify({
          filename: item.file.name,
          size_bytes: item.file.size,
          collection_key: item.collectionKey
        })
      });
      if (!response.ok) throw new Error(await readProblem(response));
      const payload = await response.json();
      const warnings = Array.isArray(payload.warnings) ? payload.warnings : [];
      renderFilenameWarnings(item, warnings);
      setItemState(
        item,
        "ready",
        warnings.length
          ? "Filename advisory found; ready to upload and analyze"
          : "Ready to upload and analyze"
      );
    } catch (error) {
      item.retryButton.textContent = "Retry check";
      item.retryButton.hidden = false;
      item.rowActions.hidden = false;
      setItemState(item, "failed", error.message || "The filename check failed.");
    }
  }

  function validateFile(file) {
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      return "Only filenames ending in .pdf are accepted.";
    }
    if (file.size === 0) return "The file is empty.";
    if (file.size > maxBytes) {
      return "The file is " + formatBytes(file.size) + "; the limit is " + formatBytes(maxBytes) + ".";
    }
    return "";
  }

  function addFile(file) {
    const collection = selectedCollection();
    if (!collection) {
      showFormError("Choose a destination collection before selecting files.");
      return;
    }
    const item = createRow({
      file: file,
      filename: file.name,
      sizeBytes: file.size,
      collectionKey: collection.key,
      collectionName: collection.name
    });
    const validationError = validateFile(file);
    if (validationError) setItemState(item, "blocked", validationError);
    else runPreflight(item);
  }

  function addFiles(fileList) {
    showFormError("");
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    if (!selectedCollection()) {
      showFormError("Choose a destination collection before selecting files.");
      return;
    }

    const localItems = Array.from(items.values()).filter(function (item) { return item.file; });
    const identities = new Set(localItems.map(function (item) { return item.identity; }));
    let selectedCount = localItems.length;
    let duplicateSelections = 0;
    let overLimit = 0;
    incoming.forEach(function (file) {
      if (identities.has(fileIdentity(file))) {
        duplicateSelections += 1;
        return;
      }
      if (selectedCount >= maxFiles) {
        overLimit += 1;
        return;
      }
      identities.add(fileIdentity(file));
      selectedCount += 1;
      addFile(file);
    });

    if (overLimit) {
      showFormError("Only " + maxFiles + " files can be selected at once. " + overLimit + " additional file" + (overLimit === 1 ? " was" : "s were") + " not added.");
    } else if (duplicateSelections) {
      showFormError(duplicateSelections + " file" + (duplicateSelections === 1 ? " was" : "s were") + " already in this selection.");
    }
    updateControls();
  }

  function showExactDuplicate(item, problem) {
    const duplicate = problem.duplicate || {};
    const panel = item.row.querySelector("[data-exact-duplicate]");
    const link = item.row.querySelector("[data-existing-document-link]");
    panel.hidden = false;
    link.href = safeDocumentPath(duplicate.detail_url, duplicate.document_id);
    item.progress.hidden = true;
    setItemState(item, "blocked", "The server found the same PDF in this collection and rejected this upload.");
  }

  function uploadFile(item) {
    setItemState(item, "uploading", "Waiting for a request slot…");
    return scheduleRequest(function () {
      return new Promise(function (resolve) {
        setItemState(item, "uploading", "Uploading…");
        item.progress.hidden = false;
        item.progress.value = 0;

        const payload = new FormData();
        payload.append("file", item.file, item.file.name);
        payload.append("idempotency_key", item.idempotencyKey);
        payload.append("collection_key", item.collectionKey);

        const request = new XMLHttpRequest();
        request.open("POST", uploadUrl, true);
        request.withCredentials = true;
        request.timeout = 10 * 60 * 1000;
        request.setRequestHeader("Accept", "application/json, application/problem+json");
        request.setRequestHeader("Idempotency-Key", item.idempotencyKey);
        if (csrfToken) request.setRequestHeader("X-CSRF-Token", csrfToken);

        request.upload.addEventListener("progress", function (event) {
          if (event.lengthComputable) {
            item.progress.value = Math.round((event.loaded / event.total) * 100);
          }
        });
        request.upload.addEventListener("load", function () {
          item.progress.value = 100;
          item.statusNode.textContent = "Upload received; scanning and registering…";
        });
        request.addEventListener("load", function () {
          const response = parsePayload(request.responseText);
          if (request.status >= 200 && request.status < 300 && response.upload) {
            item.progress.value = 100;
            item.removeButton.hidden = true;
            item.file = null;
            applyUploadResource(item, response.upload);
            resolve();
            return;
          }
          item.progress.hidden = true;
          if (request.status === 409 && (response.code === "exact-duplicate" || response.duplicate)) {
            showExactDuplicate(item, response);
          } else if (request.status === 409) {
            setItemState(item, "blocked", response.detail || "The server rejected this upload because its state conflicted with an existing request.");
          } else {
            const message = response.detail || response.message || "Upload failed with status " + request.status + ".";
            setItemState(item, "upload-failed", message + " You can retry safely.");
          }
          resolve();
        });
        request.addEventListener("error", function () {
          item.progress.hidden = true;
          setItemState(item, "upload-failed", "The connection was interrupted. You can retry safely with the same request key.");
          resolve();
        });
        request.addEventListener("timeout", function () {
          item.progress.hidden = true;
          setItemState(item, "upload-failed", "The upload timed out while waiting for the scan. You can retry safely.");
          resolve();
        });
        request.addEventListener("abort", function () {
          item.progress.hidden = true;
          setItemState(item, "upload-failed", "The upload was interrupted. You can retry safely.");
          resolve();
        });
        request.send(payload);
      });
    });
  }

  async function uploadReadyFiles() {
    if (uploading) return;
    const candidates = Array.from(items.values()).filter(eligibleForUpload);
    if (!candidates.length) return;
    uploading = true;
    showFormError("");
    updateControls();
    const results = await Promise.allSettled(candidates.map(uploadFile));
    uploading = false;
    updateControls();
    const accepted = candidates.filter(function (item) { return item.uploadId; }).length;
    announce(accepted + " file" + (accepted === 1 ? "" : "s") + " accepted for analysis.");
    if (results.some(function (result) { return result.status === "rejected"; })) {
      showFormError("One or more upload requests could not be started. Retry those files.");
    }
    schedulePoll(0);
  }

  function phaseMessage(upload) {
    const state = String(upload.document?.state || "UNKNOWN").toUpperCase();
    const phase = String(upload.operation?.phase || "QUEUED").toUpperCase();
    const phaseMessages = {
      QUEUED: "Waiting for an internal worker slot",
      EXTRACTING: "Extracting page-mapped text",
      COMPARING: "Comparing against active and pending documents",
      AWAITING_DECISION: "Analysis is ready for an operator decision",
      DELETING_EXISTING: "Deleting and verifying the selected active document",
      INGESTING: "Publishing verified active index points",
      CLEANING_UP: "Purging retained content and private screening points",
      COMPLETE: "Work completed"
    };
    const stateMessages = {
      REVIEW_REQUIRED: "Review the analysis evidence and choose Keep, Replace, or Cancel",
      INGEST_FAILED: "Ingestion failed; retained work can be retried",
      REPLACE_FAILED: "Replacement failed; the current durable stage can be retried",
      DELETE_FAILED: "Deletion failed; the active document has not been fully removed",
      CLEANUP_FAILED: "Cleanup failed; private content still requires removal",
      INGESTED: "Analysis complete and available to retrieval",
      REJECTED: "The PDF was rejected and retained content was removed",
      CANCELLED: "Upload cancelled and retained content removed",
      DELETED: "Document deleted and retained content removed"
    };
    return stateMessages[state] || phaseMessages[phase] || titleValue(state);
  }

  function applyUploadResource(item, upload) {
    const documentResource = upload.document || {};
    const uploadId = String(upload.upload_id || documentResource.id || "");
    const state = String(documentResource.state || "UNKNOWN").toUpperCase();
    const phase = String(upload.operation?.phase || "QUEUED").toUpperCase();
    if (!uploadId) return;

    if (item.uploadId && item.uploadId !== uploadId) itemsByUploadId.delete(item.uploadId);
    item.uploadId = uploadId;
    item.statusUrl = upload.status_url || "/api/v1/uploads/" + encodeURIComponent(uploadId);
    item.analysisUrl = upload.analysis_url || null;
    item.upload = upload;
    itemsByUploadId.set(uploadId, item);
    item.row.id = "upload-" + uploadId;
    item.row.dataset.uploadId = uploadId;
    item.removeButton.hidden = true;
    item.progress.hidden = true;
    item.documentLink.href = safeDocumentPath(documentResource.detail_url, documentResource.id);
    item.documentLink.hidden = false;
    item.rowActions.hidden = false;
    setStatusLabel(item.phaseNode, phase);

    item.retryButton.hidden = true;
    item.cancelButton.hidden = true;
    item.decisionForm.hidden = true;
    item.pollingEnabled = false;
    if (state === "REVIEW_REQUIRED") {
      setItemState(item, "review", phaseMessage(upload));
      if (item.analysisUrl) loadAnalysis(item);
    } else if (failedStates.has(state)) {
      setItemState(item, "failed", upload.operation?.error || phaseMessage(upload));
      item.retryButton.textContent = "Retry work";
      item.retryButton.hidden = upload.operation?.retryable === false;
      item.cancelButton.hidden = !["INGEST_FAILED", "REPLACE_FAILED"].includes(state);
    } else if (terminalStates.has(state)) {
      setItemState(item, state === "REJECTED" ? "blocked" : "complete", phaseMessage(upload));
    } else {
      setItemState(item, "active", phaseMessage(upload));
      item.pollingEnabled = Boolean(upload.open);
      item.cancelButton.hidden = !["ANALYZING", "INGESTING", "REPLACING"].includes(state);
    }
    updateControls();
    if (item.pollingEnabled) schedulePoll();
  }

  function createRestoredItem(upload) {
    const documentResource = upload.document || {};
    const uploadId = String(upload.upload_id || documentResource.id || "");
    const existing = itemsByUploadId.get(uploadId);
    if (existing) {
      applyUploadResource(existing, upload);
      return existing;
    }
    const item = createRow({
      filename: documentResource.original_filename || "Restored upload",
      sizeBytes: documentResource.size_bytes,
      collectionKey: documentResource.collection_key || "unknown",
      collectionName: documentResource.collection_key || "unknown"
    });
    applyUploadResource(item, upload);
    return item;
  }

  function temporarilyFocus(node) {
    if (!node) return;
    if (!temporarilyFocused.has(node)) {
      const previousTabIndex = node.getAttribute("tabindex");
      temporarilyFocused.add(node);
      node.setAttribute("tabindex", "-1");
      node.addEventListener("blur", function restoreTabIndex() {
        if (previousTabIndex === null) node.removeAttribute("tabindex");
        else node.setAttribute("tabindex", previousTabIndex);
        temporarilyFocused.delete(node);
      }, { once: true });
    }
    node.focus({ preventScroll: true });
    node.scrollIntoView({ block: "center" });
  }

  function focusDeepLinkedItem(item) {
    if (!item) return;
    if (item.upload?.review_required && item.review.hidden) {
      item.focusWhenReviewReady = true;
      return;
    }
    item.focusWhenReviewReady = false;
    const target = item.upload?.review_required
      ? item.review.querySelector("h4")
      : item.row;
    window.requestAnimationFrame(function () { temporarilyFocus(target); });
  }

  function focusRestoredHash() {
    const hash = window.location.hash;
    if (!hash) return;
    let targetId = "";
    try {
      targetId = decodeURIComponent(hash.slice(1));
    } catch (_error) {
      return;
    }
    const target = document.getElementById(targetId);
    const uploadId = target?.dataset.uploadId;
    if (uploadId) focusDeepLinkedItem(itemsByUploadId.get(uploadId));
  }

  async function fetchOpenUploadPages() {
    const firstUrl = new URL(openUploadsUrl, window.location.origin);
    firstUrl.searchParams.set("page", "1");
    const options = {
      credentials: "same-origin",
      headers: requestHeaders()
    };
    const first = await fetchJson(firstUrl, options);
    if (!Array.isArray(first.items)) {
      throw new Error("The open-upload response did not contain an item list.");
    }
    const pages = Number(first.pages || 0);
    if (!Number.isInteger(pages) || pages < 0) {
      throw new Error("The open-upload response contained invalid pagination metadata.");
    }
    if (pages <= 1) return [first];

    const pageRequests = [];
    for (let page = 2; page <= pages; page += 1) {
      const pageUrl = new URL(firstUrl);
      pageUrl.searchParams.set("page", String(page));
      pageRequests.push(fetchJson(pageUrl, options));
    }
    const remaining = await Promise.all(pageRequests);
    remaining.forEach(function (payload) {
      if (!Array.isArray(payload.items)) {
        throw new Error("An open-upload page did not contain an item list.");
      }
    });
    return [first, ...remaining];
  }

  async function restoreOpenUploads() {
    selection.hidden = false;
    restoreStatus.textContent = "Restoring open uploads…";
    try {
      const payloads = await fetchOpenUploadPages();
      const restoredIds = new Set();
      payloads.forEach(function (payload) {
        payload.items.forEach(function (upload) {
          const item = createRestoredItem(upload);
          if (item.uploadId) restoredIds.add(item.uploadId);
        });
      });
      restoreStatus.textContent = restoredIds.size
        ? "Restored " + restoredIds.size + " open upload" + (restoredIds.size === 1 ? "." : "s.")
        : "";
      updateControls();
      schedulePoll();
      focusRestoredHash();
    } catch (error) {
      restoreStatus.textContent = "";
      showFormError(error.message || "Open uploads could not be restored.");
      updateControls();
    }
  }

  async function refreshUpload(item) {
    if (!item.statusUrl) return;
    const upload = await fetchJson(item.statusUrl, {
      credentials: "same-origin",
      headers: requestHeaders()
    });
    applyUploadResource(item, upload);
  }

  function schedulePoll(delay) {
    if (pollTimer) window.clearTimeout(pollTimer);
    const hasPollingItems = Array.from(items.values()).some(function (item) {
      return item.pollingEnabled;
    });
    if (!hasPollingItems || polling) return;
    pollTimer = window.setTimeout(pollAllActive, delay === undefined ? pollInterval : delay);
  }

  async function pollAllActive() {
    if (polling) return;
    polling = true;
    pollTimer = null;
    const active = Array.from(items.values()).filter(function (item) {
      return item.pollingEnabled && item.statusUrl;
    });
    await Promise.all(active.map(async function (item) {
      try {
        await refreshUpload(item);
      } catch (error) {
        item.statusNode.textContent = (error.message || "Status refresh failed") + "; retrying automatically.";
      }
    }));
    polling = false;
    schedulePoll();
  }

  function addTextLine(parent, label, value) {
    const paragraph = document.createElement("p");
    const strong = document.createElement("strong");
    strong.textContent = label + ": ";
    paragraph.append(strong, document.createTextNode(value));
    parent.append(paragraph);
  }

  function renderExcerpts(parent, label, excerpts) {
    if (!Array.isArray(excerpts) || excerpts.length === 0) return;
    const details = document.createElement("details");
    details.className = "candidate-excerpts";
    const summaryNode = document.createElement("summary");
    summaryNode.textContent = label + " (" + excerpts.length + ")";
    details.append(summaryNode);
    excerpts.forEach(function (excerpt) {
      const block = document.createElement("blockquote");
      const pages = excerpt.page_start === excerpt.page_end
        ? "Page " + excerpt.page_start
        : "Pages " + excerpt.page_start + "–" + excerpt.page_end;
      const heading = document.createElement("strong");
      heading.textContent = pages;
      const text = document.createElement("p");
      text.textContent = excerpt.text || "";
      block.append(heading, text);
      details.append(block);
    });
    parent.append(details);
  }

  function renderFinding(parent, finding) {
    const section = document.createElement("div");
    section.className = "candidate-finding";
    const heading = document.createElement("h6");
    heading.textContent = titleValue(finding.role) + (finding.label ? " · " + titleValue(finding.label) : "");
    section.append(heading);
    if (finding.summary) {
      const summaryNode = document.createElement("p");
      summaryNode.textContent = finding.summary;
      section.append(summaryNode);
    }
    if (!finding.valid && finding.error) {
      const error = document.createElement("p");
      error.className = "candidate-finding__error";
      error.textContent = finding.error;
      section.append(error);
    }
    if (Array.isArray(finding.evidence) && finding.evidence.length) {
      const evidenceList = document.createElement("ul");
      finding.evidence.forEach(function (evidence) {
        const item = document.createElement("li");
        const reference = evidence.chunk_reference ? evidence.chunk_reference + ": " : "";
        item.textContent = reference + (evidence.quote || "Validated source reference");
        evidenceList.append(item);
      });
      section.append(evidenceList);
    }
    parent.append(section);
  }

  function renderCandidate(candidate, index) {
    const details = document.createElement("details");
    details.className = "candidate-evidence";
    details.open = index === 0;
    const summaryNode = document.createElement("summary");
    const rank = document.createElement("span");
    rank.className = "candidate-evidence__rank";
    rank.textContent = "#" + candidate.rank;
    const link = document.createElement("a");
    link.href = safeDocumentPath(candidate.document?.detail_url, candidate.document?.document_id);
    link.textContent = candidate.document?.filename || "Candidate document";
    link.addEventListener("click", function (event) { event.stopPropagation(); });
    const source = document.createElement("span");
    source.className = "candidate-evidence__source";
    source.textContent = candidate.source === "screening" ? "Pending upload" : "Active document";
    summaryNode.append(rank, link, source);
    details.append(summaryNode);

    const body = document.createElement("div");
    body.className = "candidate-evidence__body";
    addTextLine(body, "Deterministic signals", (candidate.reasons || []).map(titleValue).join(", ") || "Candidate threshold met");
    addTextLine(body, "Similarity", Math.round((candidate.max_cosine || 0) * 100) + "% maximum cosine · " + (candidate.moderate_cosine_chunks || 0) + " matching chunks · " + (candidate.bm25_strong_placements || 0) + " strong BM25 placements");
    if (candidate.overflow) addTextLine(body, "Classification", "Outside the top classified candidates; deterministic evidence still requires review");
    (candidate.findings || []).forEach(function (finding) { renderFinding(body, finding); });
    renderExcerpts(body, "Incoming excerpts", candidate.incoming_excerpts);
    renderExcerpts(body, "Candidate excerpts", candidate.candidate_excerpts);
    details.append(body);
    return details;
  }

  function updateDecisionControls(item) {
    const selected = item.decisionForm.querySelector('[data-decision-action]:checked')?.value || "";
    const replacing = selected === "replace";
    item.replacementFields.hidden = !replacing;
    if (!replacing) item.replacementConfirm.checked = false;
    const validReplacement = !replacing || (
      Boolean(item.replacementTarget.value) && item.replacementConfirm.checked
    );
    item.decisionSubmit.disabled = !selected || !validReplacement;
  }

  function renderAnalysis(item, analysis, candidates) {
    item.review.hidden = false;
    item.decisionForm.hidden = false;
    item.analysisRevision = analysis.revision;
    const summaryNode = item.row.querySelector("[data-analysis-summary]");
    const completenessNode = item.row.querySelector("[data-analysis-completeness]");
    const notice = item.row.querySelector("[data-analysis-notice]");
    const noticeMessage = item.row.querySelector("[data-analysis-notice-message]");
    const candidateList = item.row.querySelector("[data-candidate-list]");
    summaryNode.textContent = candidates.length + " qualifying candidate" + (candidates.length === 1 ? "" : "s") + " · revision " + analysis.revision;
    const complete = analysis.semantic_complete && analysis.classification_complete;
    setStatusLabel(completenessNode, complete ? "COMPLETE" : "INCOMPLETE");
    const incompleteReasons = Array.isArray(analysis.incomplete_reasons)
      ? analysis.incomplete_reasons
      : [];
    notice.hidden = complete;
    noticeMessage.textContent = incompleteReasons.length
      ? incompleteReasons.join(" · ") + ". These findings are advisory; Keep remains available."
      : "One or more semantic checks were inconclusive. These findings are advisory; Keep remains available.";

    candidateList.replaceChildren();
    if (candidates.length === 0) {
      const empty = document.createElement("p");
      empty.className = "candidate-list__empty";
      empty.textContent = "No deterministic candidate qualified. Review is required because analysis was incomplete.";
      candidateList.append(empty);
    } else {
      candidates.forEach(function (candidate, index) {
        candidateList.append(renderCandidate(candidate, index));
      });
    }

    item.replacementTarget.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Choose an eligible document";
    item.replacementTarget.append(placeholder);
    const eligible = candidates.filter(function (candidate) {
      return candidate.replacement_eligible;
    });
    eligible.forEach(function (candidate) {
      const option = document.createElement("option");
      option.value = candidate.document.document_id;
      option.textContent = candidate.document.filename;
      item.replacementTarget.append(option);
    });
    const replaceAction = item.decisionForm.querySelector('[data-decision-action][value="replace"]');
    replaceAction.disabled = eligible.length === 0;
    replaceAction.closest("label").classList.toggle("is-disabled", eligible.length === 0);
    updateDecisionControls(item);
    if (item.focusWhenReviewReady) focusDeepLinkedItem(item);
  }

  function sameAnalysisPage(payload, first) {
    return Boolean(
      payload?.analysis
      && first?.analysis
      && String(payload.upload_id || "") === String(first.upload_id || "")
      && String(payload.analysis.id || "") === String(first.analysis.id || "")
      && Number(payload.analysis.revision) === Number(first.analysis.revision)
      && Number(payload.pages || 0) === Number(first.pages || 0)
      && Number(payload.total_candidates || 0) === Number(first.total_candidates || 0)
    );
  }

  async function fetchAnalysisSnapshot(item) {
    const firstUrl = new URL(item.analysisUrl, window.location.origin);
    firstUrl.searchParams.set("page", "1");
    firstUrl.searchParams.set("page_size", "100");
    const options = {
      credentials: "same-origin",
      headers: requestHeaders()
    };
    const first = await fetchJson(firstUrl, options);
    if (!first.analysis || !Array.isArray(first.candidates)) {
      throw new Error("The analysis response was incomplete.");
    }
    const pages = Number(first.pages || 0);
    if (!Number.isInteger(pages) || pages < 0) {
      throw new Error("The analysis response contained invalid pagination metadata.");
    }
    if (pages <= 1) {
      return { analysis: first.analysis, candidates: first.candidates.slice() };
    }

    const pageRequests = [];
    for (let page = 2; page <= pages; page += 1) {
      const pageUrl = new URL(firstUrl);
      pageUrl.searchParams.set("page", String(page));
      pageRequests.push(fetchJson(pageUrl, options));
    }
    const payloads = await Promise.all(pageRequests);
    if (payloads.some(function (payload) {
      return !Array.isArray(payload.candidates) || !sameAnalysisPage(payload, first);
    })) {
      return null;
    }
    const candidates = first.candidates.slice();
    payloads.forEach(function (payload) {
      candidates.push(...payload.candidates);
    });
    return { analysis: first.analysis, candidates: candidates };
  }

  async function loadAnalysis(item) {
    const revision = item.upload?.analysis?.revision;
    if (item.analysisLoading || (item.analysisRevision === revision && !item.review.hidden)) return;
    item.analysisLoading = true;
    try {
      let rendered = false;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        const requestedRevision = Number(item.upload?.analysis?.revision || 0);
        const snapshot = await fetchAnalysisSnapshot(item);
        const currentRevision = Number(item.upload?.analysis?.revision || 0);
        if (
          snapshot
          && requestedRevision > 0
          && requestedRevision === currentRevision
          && Number(snapshot.analysis.revision) === requestedRevision
        ) {
          renderAnalysis(item, snapshot.analysis, snapshot.candidates);
          rendered = true;
          break;
        }
        await refreshUpload(item);
      }
      if (!rendered) {
        throw new Error("The analysis changed while its evidence was loading. Try again.");
      }
    } catch (error) {
      item.review.hidden = false;
      item.decisionForm.hidden = true;
      item.row.querySelector("[data-analysis-summary]").textContent = error.message || "Analysis evidence could not be loaded.";
      if (item.focusWhenReviewReady) focusDeepLinkedItem(item);
    } finally {
      item.analysisLoading = false;
    }
  }

  async function submitDecision(item) {
    if (!item.uploadId || !item.analysisRevision || item.decisionSubmit.disabled) return;
    const action = item.decisionForm.querySelector('[data-decision-action]:checked')?.value;
    const target = action === "replace" ? item.replacementTarget.value : null;
    if (action === "replace" && !window.confirm(
      "Replace the selected active document? The old points will be deleted and verified before this PDF is published, creating a possible availability gap."
    )) return;
    if (action === "cancel" && !window.confirm(
      "Cancel this upload and remove its PDF, analysis artifacts, and private screening points?"
    )) return;

    item.decisionError.hidden = true;
    item.decisionSubmit.disabled = true;
    item.decisionSubmit.setAttribute("aria-busy", "true");
    item.decisionKey = item.decisionKey || createId();
    try {
      const body = {
        analysis_revision: item.analysisRevision,
        action: action
      };
      if (target) body.target_document_id = target;
      const response = await fetchWithTimeout(item.statusUrl + "/decision", {
        method: "POST",
        credentials: "same-origin",
        headers: requestHeaders({ json: true, idempotencyKey: item.decisionKey }),
        body: JSON.stringify(body)
      });
      if (!response.ok) throw new Error(await readProblem(response));
      item.decisionKey = null;
      item.review.hidden = true;
      setItemState(item, "active", action === "cancel" ? "Cancellation queued" : action === "replace" ? "Safe replacement queued" : "Ingestion queued");
      await refreshUpload(item);
      item.pollingEnabled = true;
      schedulePoll(0);
      announce(titleValue(action) + " decision recorded for " + item.row.querySelector("[data-file-name]").textContent);
    } catch (error) {
      item.decisionError.textContent = error.message || "The decision could not be recorded.";
      item.decisionError.hidden = false;
      updateDecisionControls(item);
    } finally {
      item.decisionSubmit.removeAttribute("aria-busy");
    }
  }

  async function retryWork(item) {
    if (!item.uploadId) return;
    item.retryButton.disabled = true;
    try {
      const response = await fetchWithTimeout(item.statusUrl + "/retry", {
        method: "POST",
        credentials: "same-origin",
        headers: requestHeaders()
      });
      if (!response.ok) throw new Error(await readProblem(response));
      setItemState(item, "active", "Retry queued");
      await refreshUpload(item);
      item.pollingEnabled = true;
      schedulePoll(0);
    } catch (error) {
      setItemState(item, "failed", error.message || "The work could not be retried.");
    } finally {
      item.retryButton.disabled = false;
    }
  }

  async function cancelWork(item) {
    if (!item.uploadId || !window.confirm(
      "Cancel this upload and remove its PDF, full analysis, and private screening points?"
    )) return;
    item.cancelButton.disabled = true;
    try {
      const response = await fetchWithTimeout(item.statusUrl, {
        method: "DELETE",
        credentials: "same-origin",
        headers: requestHeaders()
      });
      if (!response.ok) throw new Error(await readProblem(response));
      item.review.hidden = true;
      setItemState(item, "active", "Cancellation queued; cleaning up retained content");
      await refreshUpload(item);
      item.pollingEnabled = true;
      schedulePoll(0);
    } catch (error) {
      setItemState(item, "failed", error.message || "The upload could not be cancelled.");
    } finally {
      item.cancelButton.disabled = false;
    }
  }

  fileInput.addEventListener("change", function () {
    addFiles(fileInput.files);
    fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach(function (eventName) {
    dropZone.addEventListener(eventName, function (event) {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
      dropZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach(function (eventName) {
    dropZone.addEventListener(eventName, function (event) {
      event.preventDefault();
      dropZone.classList.remove("is-dragging");
    });
  });
  dropZone.addEventListener("drop", function (event) {
    addFiles(event.dataTransfer?.files);
  });
  clearButton.addEventListener("click", function () {
    Array.from(items.values()).filter(isDismissible).forEach(removeItem);
    showFormError("");
    announce("Finished items cleared from the upload workspace");
  });
  collectionChoices.forEach(function (choice) {
    choice.addEventListener("change", function () {
      showFormError("");
      updateCollectionControls();
      const collection = selectedCollection();
      if (collection) announce("Destination set to " + collection.name);
    });
  });
  startButton.addEventListener("click", uploadReadyFiles);
  form.addEventListener("submit", function (event) { event.preventDefault(); });
  window.addEventListener("hashchange", focusRestoredHash);
  updateControls();
  restoreOpenUploads();
})();
