(() => {
  const SUPPORTED = new Set(["en", "ru", "zh_CN"]);
  let preference = "auto";
  let locale = "en";
  let messages = {};

  function normalizeBrowserLocale(rawValue) {
    const raw = String(rawValue || "en")
      .trim()
      .toLowerCase()
      .replace(/_/g, "-");
    if (raw === "ru" || raw.startsWith("ru-")) return "ru";
    if (raw === "zh" || raw.startsWith("zh-")) return "zh_CN";
    return "en";
  }

  function detectBrowserLocale() {
    const raw =
      (typeof chrome.i18n.getUILanguage === "function"
        ? chrome.i18n.getUILanguage()
        : chrome.i18n.getMessage("@@ui_locale")) || "en";
    return normalizeBrowserLocale(raw);
  }

  function resolveLocale(value) {
    return SUPPORTED.has(value) ? value : detectBrowserLocale();
  }

  async function load(localeCode) {
    try {
      const response = await fetch(
        chrome.runtime.getURL(`_locales/${localeCode}/messages.json`),
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      messages = await response.json();
    } catch (_) {
      messages = {};
    }
  }

  function normalizeSubstitutions(substitutions) {
    if (substitutions === undefined || substitutions === null) return [];
    return Array.isArray(substitutions)
      ? substitutions.map(String)
      : [String(substitutions)];
  }

  function formatEntry(entry, substitutions) {
    if (!entry?.message) return "";
    const values = normalizeSubstitutions(substitutions);
    let output = String(entry.message);
    const placeholders = entry.placeholders || {};

    for (const [name, descriptor] of Object.entries(placeholders)) {
      const content = String(descriptor?.content || "");
      const match = /^\$(\d+)$/.exec(content);
      const replacement = match
        ? (values[Number(match[1]) - 1] ?? "")
        : content;
      output = output.replace(new RegExp(`\\$${name}\\$`, "gi"), replacement);
    }
    output = output.replace(
      /\$(\d+)/g,
      (_match, number) => values[Number(number) - 1] ?? "",
    );
    return output.replace(/\$\$/g, "$");
  }

  function t(key, substitutions) {
    const local = formatEntry(messages[key], substitutions);
    if (local) return local;
    return chrome.i18n.getMessage(key, substitutions) || key;
  }

  async function apply(value) {
    preference = SUPPORTED.has(value) ? value : "auto";
    locale = resolveLocale(preference);
    await load(locale);
    return { preference, locale };
  }

  async function init() {
    const stored = await chrome.storage.local.get("uiLanguage");
    return apply(stored.uiLanguage || "auto");
  }

  async function setLanguage(value) {
    const next = SUPPORTED.has(value) ? value : "auto";
    await chrome.storage.local.set({ uiLanguage: next });
    return apply(next);
  }

  async function reloadFromStorage() {
    const stored = await chrome.storage.local.get("uiLanguage");
    return apply(stored.uiLanguage || "auto");
  }

  globalThis.AppI18n = {
    init,
    setLanguage,
    reloadFromStorage,
    t,
    get preference() {
      return preference;
    },
    get locale() {
      return locale;
    },
    get browserLocale() {
      return detectBrowserLocale();
    },
  };
})();
