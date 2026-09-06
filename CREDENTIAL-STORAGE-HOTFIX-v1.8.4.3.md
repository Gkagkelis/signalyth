# SIGNALYTH v1.8.4.3 Credential Storage Hotfix

- Apify and OpenAI credentials now use independent provider endpoints.
- Secret updates are atomic merge operations protected by a process lock, preventing concurrent-save lost updates.
- Local credentials persist outside the versioned application folder at `~/.signalyth/secrets.env`, so app upgrades/relocations do not erase them.
- Secrets are never returned to the browser.
- Added sequential, concurrent, and restart-persistence regression tests.
