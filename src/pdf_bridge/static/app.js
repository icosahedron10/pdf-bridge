(function () {
  "use strict";

  const liveRegion = document.getElementById("app-live-region");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  function announce(message) {
    if (!liveRegion) return;
    liveRegion.textContent = "";
    window.setTimeout(function () {
      liveRegion.textContent = message;
    }, 20);
  }

  function setupNavigation() {
    const toggle = document.querySelector(".nav-toggle");
    const navigation = document.getElementById("primary-navigation");
    if (!toggle || !navigation) return;

    function setOpen(isOpen) {
      document.body.classList.toggle("nav-is-open", isOpen);
      toggle.setAttribute("aria-expanded", String(isOpen));
      toggle.querySelector(".visually-hidden").textContent = isOpen ? "Close navigation" : "Open navigation";
    }

    toggle.addEventListener("click", function () {
      setOpen(toggle.getAttribute("aria-expanded") !== "true");
    });

    navigation.addEventListener("click", function (event) {
      if (event.target.closest("a")) setOpen(false);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
        setOpen(false);
        toggle.focus();
      }
    });

    window.matchMedia("(min-width: 761px)").addEventListener("change", function (event) {
      if (event.matches) setOpen(false);
    });
  }

  function setupDismissibleNotices() {
    document.querySelectorAll("[data-dismiss-notice]").forEach(function (button) {
      button.addEventListener("click", function () {
        const notice = button.closest(".notice");
        if (!notice) return;
        notice.remove();
        announce("Notification dismissed");
      });
    });
  }

  function setupCopyButtons() {
    document.querySelectorAll("[data-copy-value]").forEach(function (button) {
      button.addEventListener("click", async function () {
        const value = button.dataset.copyValue || "";
        if (!value) return;

        try {
          await navigator.clipboard.writeText(value);
          const priorLabel = button.textContent;
          button.textContent = "Copied";
          announce("Checksum copied to clipboard");
          window.setTimeout(function () {
            button.textContent = priorLabel;
          }, 1600);
        } catch (_error) {
          announce("Clipboard access was denied. Select the checksum to copy it manually.");
        }
      });
    });
  }

  function requestConfirmation(message, confirmLabel) {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(window.confirm(message));
    }

    const messageNode = document.getElementById("confirm-dialog-message");
    const confirmButton = document.getElementById("confirm-dialog-accept");
    messageNode.textContent = message;
    confirmButton.textContent = confirmLabel || "Confirm";
    dialog.returnValue = "";
    dialog.showModal();

    return new Promise(function (resolve) {
      dialog.addEventListener(
        "close",
        function () {
          resolve(dialog.returnValue === "confirm");
        },
        { once: true }
      );
    });
  }

  async function readProblem(response) {
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("json")) {
      try {
        const problem = await response.json();
        if (typeof problem.detail === "string") return problem.detail;
        if (Array.isArray(problem.detail)) {
          return problem.detail.map(function (entry) { return entry.msg || "Invalid request"; }).join("; ");
        }
        if (typeof problem.message === "string") return problem.message;
        if (typeof problem.title === "string") return problem.title;
      } catch (_error) {
        return "The server returned an unreadable error response.";
      }
    }

    if (response.status === 403) return "This action was rejected. Refresh the page and try again.";
    if (response.status === 409) return "The document changed before this action completed. Refresh and review its current status.";
    return "The request failed with status " + response.status + ".";
  }

  function setFormError(form, message) {
    const error = form.querySelector("[data-form-error]");
    if (!error) {
      announce(message);
      return;
    }
    error.textContent = message;
    error.hidden = !message;
    if (message) announce(message);
  }

  function setSubmitting(form, isSubmitting) {
    const submitter = form.querySelector('button[type="submit"], input[type="submit"]');
    form.dataset.submitting = isSubmitting ? "true" : "false";
    if (!submitter) return;
    submitter.disabled = isSubmitting;
    if (isSubmitting) submitter.setAttribute("aria-busy", "true");
    else submitter.removeAttribute("aria-busy");
  }

  function setupApiForms() {
    document.querySelectorAll("form[data-api-form]").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        if (form.dataset.submitting === "true") return;

        const confirmation = form.dataset.confirm;
        if (confirmation) {
          const approved = await requestConfirmation(confirmation, form.dataset.confirmLabel);
          if (!approved) return;
        }

        setFormError(form, "");
        setSubmitting(form, true);

        try {
          // Attribute access avoids HTML named-property collisions from fields such as
          // the classification discriminator named "action".
          const action = new URL(form.getAttribute("action") || window.location.href, window.location.href);
          if (action.origin !== window.location.origin) throw new Error("Cross-origin form actions are not allowed.");

          const method = (form.dataset.method || form.getAttribute("method") || "POST").toUpperCase();
          const headers = { Accept: "application/json, application/problem+json" };
          if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

          const options = {
            method: method,
            headers: headers,
            credentials: "same-origin",
            redirect: "follow"
          };
          if (method !== "GET" && method !== "HEAD") {
            const requestFields = Array.from(new FormData(form).entries()).filter(function (entry) {
              return entry[0] !== "csrf_token";
            });
            if (requestFields.length) {
              headers["Content-Type"] = "application/json";
              options.body = JSON.stringify(Object.fromEntries(requestFields));
            }
          }

          const response = await fetch(action.toString(), options);
          if (!response.ok) throw new Error(await readProblem(response));

          let destination = form.dataset.successUrl || "";
          const contentType = response.headers.get("content-type") || "";
          if (contentType.includes("json")) {
            const payload = await response.json().catch(function () { return null; });
            destination = payload?.redirect_url || payload?.document_url || destination;
          }

          announce(form.dataset.successMessage || "Action completed");
          if (destination) window.location.assign(destination);
          else window.location.reload();
        } catch (error) {
          setFormError(form, error.message || "The request could not be completed.");
          setSubmitting(form, false);
        }
      });
    });

    document.querySelectorAll("form[data-confirm]:not([data-api-form])").forEach(function (form) {
      form.addEventListener("submit", async function (event) {
        if (form.dataset.confirmed === "true") return;
        event.preventDefault();
        if (await requestConfirmation(form.dataset.confirm, form.dataset.confirmLabel)) {
          form.dataset.confirmed = "true";
          form.requestSubmit();
        }
      });
    });
  }

  setupNavigation();
  setupDismissibleNotices();
  setupCopyButtons();
  setupApiForms();
})();
