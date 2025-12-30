# TODO

## Testing

- Test strategy UI module end-to-end (resource registration, YAML mode, reload).
- Test lock providers beyond Z-Wave JS (virtual, future providers).
- Add regression tests for "startup no flapping" and retry behavior.
- Add Z-Wave JS provider tests (requires Z-Wave JS door lock mocks/fixtures).
- Test lock offline/disconnected behavior (retry scheduler, `_retry_unsub`).
- Test rate limiting and connection failure timing in live environment.
- Test entity availability and wait-for-state behavior.

## Refactors / Maintenance

- On slot changes, trigger a partial coordinator refresh (or update coordinator data from value updates) so polling only corrects drift/out-of-HA changes.
- Review dispatcher usage and simplify if a smaller pattern works.
- Track entity registry updates and warn if LCM entities change entity IDs (reload required).
- Add push mechanism support to the coordinator for lock integrations that support real-time value updates. Integrations can use both: polling for drift detection (periodic hard refresh with checksum) and push for immediate updates. The coordinator should accept direct data updates from push-enabled integrations.

## Features

- Manual sync services (per-slot and bulk).
- Better out-of-sync visibility in the UI.

## Docs

- Keep `AGENTS.md` and `CLAUDE.md` in sync after architecture changes.
