/* Browser-specific proxy API adapter. */
async function setBrowserProxy(localPort) {
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
          passthrough: "localhost, 127.0.0.1, ::1",
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
          bypassList: ["localhost", "127.0.0.1", "[::1]"],
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
