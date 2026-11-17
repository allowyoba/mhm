let proxyExcludedSubnets = [];
function setProxyExclusions(subnets = []) {
  proxyExcludedSubnets = Array.isArray(subnets) ? subnets : [];
}
async function checkProxyPermission() {
  if (
    typeof browser !== "undefined" &&
    typeof browser.runtime?.getBrowserInfo === "function" &&
    browser.extension?.isAllowedIncognitoAccess &&
    !(await browser.extension.isAllowedIncognitoAccess())
  ) {
    const error = new Error(
      chrome.i18n?.getMessage("firefoxProxyPermission") ||
        "Firefox requires private-window access to control the proxy. Open Add-ons, select mhm, and allow Run in Private Windows.",
    );
    error.code = "FIREFOX_PRIVATE_ACCESS";
    throw error;
  }
}

/* Browser-specific proxy API adapter. */
let proxyUpdates = Promise.resolve();
function setBrowserProxy(localPort) {
  const update = proxyUpdates
    .catch(() => {})
    .then(() => applyBrowserProxy(localPort));
  proxyUpdates = update;
  return update;
}
async function applyBrowserProxy(localPort) {
  await checkProxyPermission();
  // Chromium also exposes `browser`. Only Firefox implements getBrowserInfo
  // and accepts ProxyConfig's proxyType/http/ssl fields.
  const firefox =
    typeof browser !== "undefined" &&
    typeof browser.runtime?.getBrowserInfo === "function";
  if (firefox && browser.proxy?.settings) {
    if (localPort === null)
      await browser.proxy.settings.clear({ scope: "regular" });
    else
      await browser.proxy.settings.set({
        value: {
          proxyType: "manual",
          http: `127.0.0.1:${localPort}`,
          ssl: `127.0.0.1:${localPort}`,
          passthrough: [
            "localhost",
            "127.0.0.1",
            "::1",
            ...proxyExcludedSubnets,
          ].join(", "),
          proxyDNS: true,
        },
        scope: "regular",
      });
  } else if (localPort === null) {
    await chrome.proxy.settings.clear({ scope: "regular" });
  } else {
    await chrome.proxy.settings.set({
      value: {
        mode: "fixed_servers",
        rules: {
          singleProxy: { scheme: "http", host: "127.0.0.1", port: localPort },
          bypassList: [
            "localhost",
            "127.0.0.1",
            "[::1]",
            ...proxyExcludedSubnets,
          ],
        },
      },
      scope: "regular",
    });
    const current = await chrome.proxy.settings.get({ incognito: false });
    if (current.levelOfControl !== "controlled_by_this_extension") {
      throw new Error(
        "Another extension or browser policy controls the proxy. Disable the conflicting proxy setting.",
      );
    }
  }
}
