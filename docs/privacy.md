# Privacy

mhm contains no analytics, advertising or diagnostic uploads to its developer.

Profiles, subscription URLs, WireGuard keys and custom rules are stored locally in encrypted storage. Encryption keys are protected by Windows DPAPI, macOS Keychain or Linux Secret Service. Traffic history is stored locally in SQLite.

The extension sends commands to a local native host through Native Messaging. The native host runs mihomo. Subscription updates contact the server selected by the user. Proxy traffic goes through nodes in the active profile. Those services receive information needed to handle connections and apply their own retention policies.

Latency checks make network requests through the selected node. The standalone installer downloads official mihomo releases from GitHub. Subscription HTTPS certificate verification can only be disabled by an explicit user choice.

Deleting a profile removes its saved local revisions. To remove application data, use the installer's user-data removal option. Removing only the extension does not remove native host data.

Questions and bug reports: https://github.com/allowyoba/mhm/issues. Do not include subscription credentials, private keys or configurations containing secrets in public reports.

[Русская версия](ru/privacy.md)
