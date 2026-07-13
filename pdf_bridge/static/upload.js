(function () {
  "use strict";

  const form = document.querySelector("[data-upload-form]");
  if (!form) return;

  const fileInput = form.querySelector("[data-file-input]");
  const collectionChoices = Array.from(form.querySelectorAll("[data-collection-choice]"));
  const dropHelp = form.querySelector("[data-drop-help]");
  const dropZone = document.getElementById("drop-zone");
  const selection = document.getElementById("upload-selection");
  const list = document.getElementById("upload-list");
  const rowTemplate = document.getElementById("upload-row-template");
  const countLabel = document.getElementById("selected-file-count");
  const summary = document.getElementById("upload-summary");
  const startButton = document.getElementById("start-upload");
  const clearButton = form.querySelector("[data-clear-files]");
  const formError = document.getElementById("upload-form-error");
  const formErrorMessage = form.querySelector("[data-upload-error-message]");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const liveRegion = document.getElementById("app-live-region");
  const maxFiles = Number.parseInt(form.dataset.maxFiles || "20", 10);
  const maxBytes = Number.parseInt(form.dataset.maxBytes || "52428800", 10);
  const preflightUrl = form.dataset.preflightUrl || "/api/v1/uploads/preflight";
  const uploadUrl = form.dataset.uploadUrl || "/api/v1/uploads";
  const items = new Map();
  let uploading = false;

  function selectedCollection() {
    const selected = collectionChoices.find(function (choice) { return choice.checked; });
    if (!selected) return null;
    return {
      key: selected.value,
      name: selected.dataset.collectionName || selected.value
    };
  }

  function updateCollectionControls() {
    const collection = selectedCollection();
    const locked = items.size > 0;
    collectionChoices.forEach(function (choice) { choice.disabled = locked || uploading; });
    fileInput.disabled = !collection || uploading;
    dropZone.classList.toggle("is-disabled", !collection || uploading);
    dropZone.setAttribute("aria-disabled", String(!collection || uploading));
    if (dropHelp) {
      dropHelp.textContent = collection
        ? "PDF files only"
        : "Choose a collection first";
    }
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
      return Array.from(values, function (value) { return value.toString(16).padStart(8, "0"); }).join("-");
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
    return value.toLocaleString(undefined, { maximumFractionDigits: value >= 10 ? 1 : 2 }) + " " + unit;
  }

  function showFormError(message) {
    formErrorMessage.textContent = message || "";
    formError.hidden = !message;
    if (message) announce(message);
  }

  function fileIdentity(file) {
    return [file.name, file.size, file.lastModified].join("::");
  }

  function setItemState(item, status, message) {
    item.status = status;
    item.row.classList.remove("is-ready", "is-error", "is-blocked", "is-complete", "is-uploading");
    const classByStatus = {
      ready: "is-ready",
      retryable: "is-error",
      error: "is-error",
      blocked: "is-blocked",
      complete: "is-complete",
      uploading: "is-uploading"
    };
    if (classByStatus[status]) item.row.classList.add(classByStatus[status]);
    item.statusNode.textContent = message;
    updateControls();
  }

  function eligibleForUpload(item) {
    return item.status === "ready" || item.status === "retryable";
  }

  function updateControls() {
    const allItems = Array.from(items.values());
    selection.hidden = allItems.length === 0;
    countLabel.textContent = allItems.length ? "(" + allItems.length + " of " + maxFiles + ")" : "";

    const counts = allItems.reduce(function (totals, item) {
      if (item.status === "checking") totals.checking += 1;
      else if (item.status === "awaiting-confirmation") totals.confirmation += 1;
      else if (item.status === "complete") totals.complete += 1;
      else if (item.status === "blocked" || item.status === "error") totals.blocked += 1;
      else if (item.status === "retryable") totals.retryable += 1;
      else if (item.status === "ready") totals.ready += 1;
      else if (item.status === "uploading") totals.uploading += 1;
      return totals;
    }, { checking: 0, confirmation: 0, complete: 0, blocked: 0, retryable: 0, ready: 0, uploading: 0 });

    const parts = [];
    if (counts.ready) parts.push(counts.ready + " ready");
    if (counts.retryable) parts.push(counts.retryable + " ready to retry");
    if (counts.checking) parts.push(counts.checking + " checking");
    if (counts.confirmation) parts.push(counts.confirmation + " need confirmation");
    if (counts.complete) parts.push(counts.complete + " queued");
    if (counts.blocked) parts.push(counts.blocked + " blocked");
    summary.textContent = parts.join(" · ");

    const hasEligible = allItems.some(eligibleForUpload);
    const checking = counts.checking > 0;
    startButton.disabled = uploading || checking || !hasEligible;
    startButton.textContent = counts.retryable && !counts.ready ? "Retry failed files" : "Upload ready files";
    clearButton.disabled = uploading;
    allItems.forEach(function (item) {
      item.removeButton.disabled = uploading || item.status === "uploading";
    });
    updateCollectionControls();
  }

  function safeDocumentPath(candidate, documentId) {
    if (candidate) {
      try {
        const url = new URL(candidate, window.location.origin);
        if (url.origin === window.location.origin) return url.pathname + url.search + url.hash;
      } catch (_error) {
        // Use the identifier fallback below.
      }
    }
    return documentId ? "/documents/" + encodeURIComponent(documentId) : "/library";
  }

  function renderMatchList(item, matches) {
    const matchList = item.row.querySelector("[data-duplicate-matches]");
    matchList.replaceChildren();
    matches.slice(0, 5).forEach(function (match) {
      const listItem = document.createElement("li");
      const documentId = match.document_id || match.id || "";
      const filename = match.original_filename || match.filename || "Existing document";
      const link = document.createElement("a");
      link.href = safeDocumentPath(match.document_url || match.detail_url || match.url, documentId);
      link.textContent = filename;
      listItem.append(link);
      const matchStatus = match.status || match.state;
      if (matchStatus) listItem.append(document.createTextNode(" — " + String(matchStatus).replaceAll("_", " ").toLowerCase()));
      const boundary = match.collection_key || "";
      if (boundary) listItem.append(document.createTextNode(" · " + boundary));
      matchList.append(listItem);
    });
  }

  async function runPreflight(item) {
    setItemState(item, "checking", "Checking filename and size…");
    try {
      const headers = {
        Accept: "application/json, application/problem+json",
        "Content-Type": "application/json"
      };
      if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

      const response = await fetch(preflightUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: headers,
        body: JSON.stringify({
          filename: item.file.name,
          size_bytes: item.file.size,
          collection_key: item.collectionKey
        })
      });
      if (!response.ok) throw new Error(await problemMessage(response));

      const payload = await response.json();
      const matches = payload.matches || payload.possible_duplicates || payload.duplicates || [];
      item.possibleDuplicate = Boolean(payload.possible_duplicate || payload.requires_confirmation || matches.length);

      if (item.possibleDuplicate) {
        const warning = item.row.querySelector("[data-duplicate-warning]");
        const warningMessage = item.row.querySelector("[data-duplicate-message]");
        warning.hidden = false;
        if (payload.message) warningMessage.textContent = payload.message;
        renderMatchList(item, matches);
        setItemState(item, "awaiting-confirmation", "Review the possible duplicate before uploading.");
      } else {
        setItemState(item, "ready", "Ready to upload");
      }
    } catch (error) {
      setItemState(item, "error", error.message || "Duplicate check failed. Remove the file and try again.");
    }
  }

  function validateFile(file) {
    if (!file.name.toLowerCase().endsWith(".pdf")) return "Only filenames ending in .pdf are accepted.";
    if (file.size === 0) return "The file is empty.";
    if (file.size > maxBytes) return "The file is " + formatBytes(file.size) + "; the limit is " + formatBytes(maxBytes) + ".";
    return "";
  }

  function addFile(file) {
    const collection = selectedCollection();
    if (!collection) {
      showFormError("Choose a destination collection before selecting files.");
      return;
    }
    const id = createId();
    const fragment = rowTemplate.content.cloneNode(true);
    const row = fragment.querySelector("[data-upload-item]");
    row.dataset.uploadId = id;
    row.querySelector("[data-file-name]").textContent = file.name;
    row.querySelector("[data-file-size]").textContent = formatBytes(file.size);
    list.append(fragment);

    const item = {
      id: id,
      idempotencyKey: createId(),
      identity: fileIdentity(file),
      file: file,
      collectionKey: collection.key,
      collectionName: collection.name,
      row: list.querySelector('[data-upload-id="' + CSS.escape(id) + '"]'),
      status: "checking",
      possibleDuplicate: false,
      confirmed: false
    };
    item.statusNode = item.row.querySelector("[data-file-status]");
    item.progress = item.row.querySelector("[data-file-progress]");
    item.removeButton = item.row.querySelector("[data-remove-file]");
    item.progress.setAttribute("aria-label", "Upload progress for " + file.name);
    item.removeButton.setAttribute("aria-label", "Remove " + file.name);
    items.set(id, item);

    item.removeButton.addEventListener("click", function () {
      if (uploading || item.status === "uploading") return;
      items.delete(id);
      item.row.remove();
      updateControls();
      announce(file.name + " removed from selection");
    });

    item.row.querySelector("[data-duplicate-confirm]").addEventListener("change", function (event) {
      item.confirmed = event.target.checked;
      if (item.confirmed) setItemState(item, "ready", "Possible duplicate confirmed; ready to upload");
      else setItemState(item, "awaiting-confirmation", "Review the possible duplicate before uploading.");
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

    const existingIdentities = new Set(Array.from(items.values(), function (item) { return item.identity; }));
    let duplicateSelections = 0;
    let overLimit = 0;

    incoming.forEach(function (file) {
      if (existingIdentities.has(fileIdentity(file))) {
        duplicateSelections += 1;
        return;
      }
      if (items.size >= maxFiles) {
        overLimit += 1;
        return;
      }
      existingIdentities.add(fileIdentity(file));
      addFile(file);
    });

    if (overLimit) showFormError("Only " + maxFiles + " files can be selected at once. " + overLimit + " additional file" + (overLimit === 1 ? " was" : "s were") + " not added.");
    else if (duplicateSelections) showFormError(duplicateSelections + " file" + (duplicateSelections === 1 ? " was" : "s were") + " already in this selection.");
    updateControls();
  }

  async function problemMessage(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("json")) {
      const problem = await response.json().catch(function () { return null; });
      if (typeof problem?.detail === "string") return problem.detail;
      if (typeof problem?.message === "string") return problem.message;
      if (typeof problem?.title === "string") return problem.title;
    }
    if (response.status === 503) return "The malware scanner or another required service is unavailable. Uploads are blocked.";
    return "The duplicate check failed with status " + response.status + ".";
  }

  function parsePayload(text) {
    if (!text) return {};
    try { return JSON.parse(text); }
    catch (_error) { return {}; }
  }

  function showExactDuplicate(item, problem) {
    const duplicate = problem.duplicate || problem.extensions?.duplicate || {};
    const documentId = problem.existing_document_id || duplicate.document_id || duplicate.id || "";
    const documentUrl = problem.existing_document_url || duplicate.document_url || duplicate.detail_url || duplicate.url || "";
    const panel = item.row.querySelector("[data-exact-duplicate]");
    const link = item.row.querySelector("[data-existing-document-link]");
    panel.hidden = false;
    link.href = safeDocumentPath(documentUrl, documentId);
    setItemState(item, "blocked", "The server found an exact content match and rejected this upload.");
  }

  function showPossibleDuplicateFromUpload(item, problem) {
    const warning = item.row.querySelector("[data-duplicate-warning]");
    const warningMessage = item.row.querySelector("[data-duplicate-message]");
    const checkbox = item.row.querySelector("[data-duplicate-confirm]");
    item.possibleDuplicate = true;
    item.confirmed = false;
    checkbox.disabled = false;
    checkbox.checked = false;
    warning.hidden = false;
    if (problem.detail) warningMessage.textContent = problem.detail;
    renderMatchList(item, problem.possible_duplicates || []);
    setItemState(item, "awaiting-confirmation", "A possible duplicate appeared while this file was uploading. Review it before retrying.");
  }

  function queuedDocumentLink(item, payload) {
    const documentId = payload.document_id || payload.id || payload.document?.id || "";
    const documentUrl = payload.document_url || payload.document?.detail_url || payload.url || "";
    item.statusNode.textContent = "Queued successfully for " + item.collectionName + ". ";
    const link = document.createElement("a");
    link.href = safeDocumentPath(documentUrl, documentId);
    link.textContent = "View document";
    item.statusNode.append(link);
  }

  function uploadFile(item) {
    return new Promise(function (resolve) {
      setItemState(item, "uploading", "Uploading…");
      item.progress.hidden = false;
      item.progress.value = 0;

      const payload = new FormData();
      payload.append("file", item.file, item.file.name);
      payload.append("possible_duplicate_confirmed", String(item.possibleDuplicate && item.confirmed));
      payload.append("idempotency_key", item.idempotencyKey);
      payload.append("collection_key", item.collectionKey);

      const request = new XMLHttpRequest();
      let settled = false;
      function finish() {
        if (settled) return;
        settled = true;
        resolve();
      }

      request.open("POST", uploadUrl, true);
      request.withCredentials = true;
      request.timeout = 10 * 60 * 1000;
      request.setRequestHeader("Accept", "application/json, application/problem+json");
      request.setRequestHeader("Idempotency-Key", item.idempotencyKey);
      if (csrfToken) request.setRequestHeader("X-CSRF-Token", csrfToken);

      request.upload.addEventListener("progress", function (event) {
        if (event.lengthComputable) item.progress.value = Math.round((event.loaded / event.total) * 100);
      });

      request.upload.addEventListener("load", function () {
        item.progress.value = 100;
        item.statusNode.textContent = "Upload received; checking signature, hash, and malware scan…";
      });

      request.addEventListener("load", function () {
        const response = parsePayload(request.responseText);
        if (request.status >= 200 && request.status < 300) {
          item.progress.value = 100;
          item.removeButton.hidden = true;
          item.row.querySelector("[data-duplicate-warning]").hidden = true;
          item.row.querySelector("[data-duplicate-confirm]").disabled = true;
          setItemState(item, "complete", "Queued successfully");
          queuedDocumentLink(item, response);
          finish();
          return;
        }

        item.progress.hidden = true;
        if (request.status === 409 && (response.code === "exact-duplicate" || response.duplicate)) {
          showExactDuplicate(item, response);
        } else if (request.status === 409 && response.code === "possible-duplicate-confirmation-required") {
          showPossibleDuplicateFromUpload(item, response);
        } else if (request.status === 409) {
          setItemState(item, "blocked", response.detail || "The server rejected this upload because its state conflicted with an existing request.");
        } else {
          const message = typeof response.detail === "string" ? response.detail : response.message || "Upload failed with status " + request.status + ".";
          setItemState(item, "retryable", message + " You can retry safely.");
        }
        finish();
      });

      request.addEventListener("error", function () {
        item.progress.hidden = true;
        setItemState(item, "retryable", "The connection was interrupted. You can retry safely.");
        finish();
      });

      request.addEventListener("timeout", function () {
        item.progress.hidden = true;
        setItemState(item, "retryable", "The upload timed out while waiting for the scan. You can retry safely.");
        finish();
      });

      request.addEventListener("abort", function () {
        item.progress.hidden = true;
        setItemState(item, "retryable", "The upload was interrupted. You can retry safely.");
        finish();
      });

      request.send(payload);
    });
  }

  async function uploadReadyFiles() {
    if (uploading) return;
    const candidates = Array.from(items.values()).filter(eligibleForUpload);
    if (!candidates.length) return;

    uploading = true;
    showFormError("");
    updateControls();
    for (const item of candidates) {
      if (items.has(item.id) && eligibleForUpload(item)) await uploadFile(item);
    }
    uploading = false;
    updateControls();

    const completed = candidates.filter(function (item) { return item.status === "complete"; }).length;
    announce(completed + " file" + (completed === 1 ? "" : "s") + " added to the queue.");
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
    if (uploading) return;
    items.clear();
    list.replaceChildren();
    fileInput.value = "";
    showFormError("");
    updateControls();
    announce("File selection cleared");
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
  updateControls();
})();
