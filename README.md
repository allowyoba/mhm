# mhm

A browser extension for mihomo profiles and subscriptions, with encrypted local storage, traffic budgets and custom routing rules.

[Русская инструкция](docs/README.ru.md) · [Downloads](https://github.com/allowyoba/mhm/releases) · [Privacy](docs/privacy.md) · [Contributing](docs/contributing.md)

## Supported platforms

| Component | Platforms |
| --- | --- |
| Extension | Chrome, Edge, Firefox 142+, Opera |
| Native host | Windows amd64, macOS arm64/amd64, Linux arm64/amd64 |
| Linux package | Debian 12+ and compatible Ubuntu with glibc 2.36+, including Ubuntu 24.04 |
| Interface | English, Russian, Simplified Chinese |

mhm controls the browser proxy. It does not provide a system VPN. WireGuard is an optional transport for subscription updates, not a persistent tunnel for other applications.

## Install

Download the extension and native installer for your platform from [Releases](https://github.com/allowyoba/mhm/releases).

For Chrome, Edge and Opera, extract the browser ZIP and load the directory in developer mode at `chrome://extensions`, `edge://extensions` or `opera://extensions`. Firefox requires a Mozilla-signed extension for permanent installation. The initial version has been submitted to [Mozilla Add-ons](https://addons.mozilla.org/firefox/addon/mhm/) and is awaiting review. An unsigned XPI can only be loaded temporarily through `about:debugging`.

Open mhm and follow the setup screen. It checks the native component and provides a command for your browser, OS, architecture and extension ID.

### Windows and macOS

Run the downloaded installer from its directory. For Apple Silicon:

```sh
chmod +x ./mhm-setup-0.1.1-macos-arm64
./mhm-setup-0.1.1-macos-arm64 install --browser chrome --extension-id YOUR_EXTENSION_ID
```

For Windows, use PowerShell:

```powershell
.\mhm-setup-0.1.1-windows-amd64.exe install --browser edge --extension-id YOUR_EXTENSION_ID
```

The standalone installer includes Python and the native host. It downloads an official stable mihomo build and verifies its SHA-256 digest. To use an existing binary, pass `--mihomo PATH`. Firefox uses `--browser firefox --extension-id mhm@allowyoba.github.io`.

Windows binaries are not Authenticode-signed. macOS binaries are not notarized.

### Linux

```sh
sudo apt install ./mhm_0.1.1_amd64.deb
mhm-setup install --browser edge --extension-id YOUR_EXTENSION_ID
```

Use the arm64 package on ARM machines. The deb includes the native host, mihomo and `mhm-setup`. Register your browser as your desktop user, without sudo. The installer can discover an installed extension, or you can copy its ID from the setup screen.

Encrypted storage requires an unlocked Secret Service provider, such as GNOME Keyring. The package installs `secret-tool`. If the keyring is unavailable, unlock it and retry; new secrets are never stored as plaintext.

## Use

Add a subscription URL or a self-contained mihomo YAML profile, select it and connect. The Connection view includes routing mode, proxy groups and latency checks. The toolbar icon shows connected, connecting, disconnected or blocked states. Its tooltip includes the active profile name.

Profiles preserve YAML groups, DNS, rules and HTTPS/inline providers. Node-only subscriptions receive a selection group and a final routing rule. Subscription updates can run manually or every 6, 12 or 24 hours while the browser is running. Updates are validated before saving; a failed download leaves the working profile intact. Apply a new revision to reconnect, or roll back to the previous revision.

### Traffic and limits

The Traffic view shows received and sent bytes for today, the calendar month and all time, a 30-day chart and recent sessions. History is stored locally in SQLite.

Local budgets count upload plus download across all profiles on this computer. Choose a daily or monthly period and a monthly billing day. Days beyond the end of a month are clamped to its last day. At 80%, mhm shows a warning. At the limit, it can warn or stop the proxy. The native host enforces blocking even with the popup closed, with a short telemetry delay. Reconnecting stays blocked until the period resets or the budget changes. Explicitly disconnecting restores the browser's normal connection.

Provider quotas from subscription response headers appear separately in profile details and refresh with the subscription.

### Custom rules

Choose a profile in Rules. Add exact domains, domain suffixes, keywords, IPv4/IPv6 subnets or a final MATCH rule. Targets include DIRECT, REJECT and the profile's proxy groups. Reorder, remove or edit rules as mihomo text.

Custom rules run before subscription rules and survive subscription updates. Save validates the complete resulting configuration. Reconnect or apply the profile to activate changes.

### WireGuard and HTTPS

Subscription updates can use a temporary WireGuard tunnel. Import a `.conf` with one Interface and one Peer. Partial AllowedIPs, IPv4/IPv6, DNS, MTU, PresharedKey and PersistentKeepalive are supported. The subscription server and internal DNS must be reachable through those routes. The tunnel stops after the download. A tunnel failure never falls back to a direct download.

HTTPS options include system trust, a custom PEM/DER CA and an explicit certificate-verification bypass. The CA and settings are stored with the encrypted profile. Bypass disables certificate and hostname checks and displays a warning. Custom trust settings apply only to the original subscription origin; redirects to another origin use system trust.

## Storage and network scope

Windows uses current-user DPAPI. macOS and Linux use AES-256-GCM with a key protected by Keychain or Secret Service. Secret values do not appear in process arguments. YAML, subscription credentials and WireGuard keys stay out of browser storage.

Runtime configuration uses a private directory and is removed after the engine loads it. Provider caches are encrypted when the core stops. A watchdog terminates child processes and cleans runtime files after host failure.

The Native Messaging name is `io.mhm.browser`; the Firefox ID is `mhm@allowyoba.github.io`. Data lives under `mhm` in the user's application-data directory. TUN, LAN access and additional listeners are disabled. File providers require a self-contained configuration; embed required keys and certificates in YAML. Browser DNS, DoH, WebRTC and QUIC behavior still needs checking in your environment.

## Maintenance

```sh
mhm-setup diagnose --json
mhm-setup repair
mhm-setup uninstall
```

On Windows and macOS, replace `mhm-setup` with the downloaded installer filename. Uninstall preserves profiles and traffic history. `uninstall --remove-user-data` removes them and the encryption key.

On Linux, remove the system package with `sudo apt remove mhm`. To also remove user data, first run `mhm-setup uninstall --remove-user-data` as the desktop user.

## Development

Use Python 3.12+, Node.js and Docker for Linux packaging. Runtime versions and IDs are generated from `product.json`.

```sh
make setup
make version
make lint test
make build extensions
make smoke SMOKE_ARGS='--mihomo /path/to/mihomo'
make linux
```

`make linux` builds and installs a deb in a disposable container, then checks Secret Service encryption, four browser registrations, rules, budgets, a real core and signed APT installation. Output goes to `.build/linux-output`. Set `MHM_LINUX_ARCH=amd64` or `arm64` when using a matching runner or emulation.

The native host separates configuration, profiles, encrypted storage, network transport, TLS, traffic, policy and platform services. Native Messaging is the application boundary. UI modules handle API access, formatting, traffic and rules; background adapters handle native requests, proxy settings and toolbar state.

### Releases

CI tests and builds Windows amd64, macOS arm64/amd64 and Linux arm64/amd64, then packages all four browser extensions and runs `web-ext lint`. A tag such as `0.1.1` must exactly match `product.json`. Successful tag builds publish a GitHub Release with packages and `SHA256SUMS`.


The separate `Submit Firefox to Mozilla` workflow uses repository secrets `AMO_JWT_ISSUER` and `AMO_JWT_SECRET`. Initial submission can also use `python3 scripts/sign_firefox.py --env-file .env`. Do not resubmit an existing version. Mozilla review and signing are separate from GitHub publication; the generated Firefox package remains unsigned until a signed file is attached separately.

APT archives are not a hosted service. To prepare a signed repository on Linux:

```sh
python scripts/build_apt_repository.py dist/mhm_0.1.1_amd64.deb --output dist/apt --signing-key YOUR_GPG_KEY_ID
```

This creates Packages, Release, InRelease, Release.gpg and an exported public key. Host the output over HTTPS and configure clients with `signed-by`. Without a signing key, the script generates unsigned local metadata. Installing a local deb with `apt install ./file.deb` does not require your own repository.
