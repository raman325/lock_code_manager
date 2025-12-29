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

- Migrate `hass.data[DOMAIN]` to `config_entry.runtime_data` if it does not add complexity.
- On slot changes, trigger a partial coordinator refresh (or update coordinator data from value updates) so polling only corrects drift/out-of-HA changes.
- Deduplicate coordinator refresh vs `hard_refresh_usercodes` cache refresh logic for Z-Wave JS.
- Move coordinator setup into `_async_setup()` where it reduces boilerplate.
- Review dispatcher usage and simplify if a smaller pattern works.
- Track entity registry updates and warn if LCM entities change entity IDs (reload required).
- Explore using HA's scheduler instead of direct sleeps, with task tracking managed by HA.

## Features

- Manual sync services (per-slot and bulk).
- Better out-of-sync visibility in the UI.

## Docs

- Keep `AGENTS.md` and `CLAUDE.md` in sync after architecture changes.
