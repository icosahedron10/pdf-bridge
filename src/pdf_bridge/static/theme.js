(function () {
  "use strict";

  const storageKey = "pdf-bridge:theme";
  const validModes = new Set(["light", "dark"]);
  const root = document.documentElement;
  const configuredDefault = ["system", "light", "dark"].includes(root.dataset.themeDefault)
    ? root.dataset.themeDefault
    : "system";
  const systemPreference = window.matchMedia("(prefers-color-scheme: dark)");
  let override = readOverride();
  let toggle = null;

  function readOverride() {
    try {
      const stored = window.localStorage.getItem(storageKey);
      return validModes.has(stored) ? stored : null;
    } catch (_error) {
      return null;
    }
  }

  function writeOverride(mode) {
    try {
      window.localStorage.setItem(storageKey, mode);
    } catch (_error) {
      // The selected mode still applies for this page when storage is unavailable.
    }
  }

  function systemMode() {
    return systemPreference.matches ? "dark" : "light";
  }

  function preferredMode() {
    if (override) return override;
    return configuredDefault === "system" ? systemMode() : configuredDefault;
  }

  function updateToggle(mode) {
    if (!toggle) return;
    const isDark = mode === "dark";
    const title = isDark ? "Switch to light mode" : "Switch to dark mode";
    toggle.setAttribute("aria-pressed", String(isDark));
    toggle.setAttribute("aria-label", "Dark mode");
    toggle.setAttribute("title", title);
  }

  function applyMode(mode) {
    root.dataset.theme = mode;
    updateToggle(mode);
  }

  function handleSystemChange() {
    if (!override && configuredDefault === "system") applyMode(systemMode());
  }

  function setupToggle() {
    toggle = document.querySelector("[data-theme-toggle]");
    if (!toggle) return;
    updateToggle(root.dataset.theme);
    toggle.addEventListener("click", function () {
      override = root.dataset.theme === "dark" ? "light" : "dark";
      writeOverride(override);
      applyMode(override);
    });
  }

  applyMode(preferredMode());

  if (typeof systemPreference.addEventListener === "function") {
    systemPreference.addEventListener("change", handleSystemChange);
  } else {
    systemPreference.addListener(handleSystemChange);
  }

  window.addEventListener("storage", function (event) {
    if (event.key !== storageKey) return;
    override = validModes.has(event.newValue) ? event.newValue : null;
    applyMode(preferredMode());
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setupToggle, { once: true });
  } else {
    setupToggle();
  }
})();
