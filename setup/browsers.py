"""Browser channels and native messaging integration contracts."""

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserSpec:
    label: str
    store_hosts: frozenset[str]
    registry_owners: tuple[str, ...]
    mac_native: tuple[str, ...]
    linux_native: tuple[str, ...]


BROWSERS = {
    "chrome": BrowserSpec(
        "Chrome",
        frozenset({"chromewebstore.google.com", "chrome.google.com"}),
        (r"Software\Google\Chrome", r"Software\Chromium"),
        ("Google/Chrome/NativeMessagingHosts", "Chromium/NativeMessagingHosts"),
        (
            "google-chrome/NativeMessagingHosts",
            "google-chrome-beta/NativeMessagingHosts",
            "chromium/NativeMessagingHosts",
        ),
    ),
    "edge": BrowserSpec(
        "Edge",
        frozenset(
            {
                "microsoftedge.microsoft.com",
                "chromewebstore.google.com",
                "chrome.google.com",
            }
        ),
        (r"Software\Microsoft\Edge",),
        ("Microsoft Edge/NativeMessagingHosts",),
        ("microsoft-edge/NativeMessagingHosts",),
    ),
    "firefox": BrowserSpec(
        "Firefox",
        frozenset({"addons.mozilla.org"}),
        (r"Software\Mozilla",),
        ("Mozilla/NativeMessagingHosts",),
        ("@home/.mozilla/native-messaging-hosts",),
    ),
    "opera": BrowserSpec(
        "Opera",
        frozenset(
            {"addons.opera.com", "chromewebstore.google.com", "chrome.google.com"}
        ),
        (r"Software\Google\Chrome", r"Software\Chromium"),
        (
            "Google/Chrome/NativeMessagingHosts",
            "com.operasoftware.Opera/NativeMessagingHosts",
        ),
        ("opera/NativeMessagingHosts", "google-chrome/NativeMessagingHosts"),
    ),
}
