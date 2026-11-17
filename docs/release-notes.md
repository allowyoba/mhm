mhm 0.1.2 adds IPv4/IPv6 subnet exclusions shared by all profiles. Matching traffic goes directly, including in global mode. Save and apply validates CIDR entries and reconnects the active profile.

Closing the last browser window or losing the Native Messaging channel now stops the main core and temporary WireGuard cores. Pending requests cannot restart a core after the browser session ends. Host-crash containment is covered by lifecycle regression tests.

Firefox profile creation works without debugging. Proxy connections explain the required private-window permission before starting a core. Install the signed extension from Mozilla Add-ons; new version approval is separate from GitHub publication. Update the companion app together with the extension to use the new lifecycle and subnet features.

Downloads include Chrome, Edge, Firefox and Opera packages, Windows amd64, macOS amd64/arm64 and Linux amd64/arm64 installers, deb packages and local APT repository archives. Generated Firefox XPI files are unsigned until Mozilla signs them. Windows signing and macOS notarization are not configured. SHA256SUMS covers all attached files.
