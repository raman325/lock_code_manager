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

### Entity registry change detection

Track entity registry updates and warn if LCM entities change entity IDs (reload required).

### Push mechanism for coordinator

Add push mechanism support to the coordinator for lock integrations that support real-time value updates. Integrations can use both: polling for drift detection (periodic hard refresh with checksum) and push for immediate updates. The coordinator should accept direct data updates from push-enabled integrations.

### Convert config and internal dicts to dataclasses

Convert config entry data to typed dataclasses with `from_dict`/`from_entry` class methods. Use object instances internally instead of iterating through raw config dicts. Audit codebase for other complex dicts that would benefit from dataclass conversion (e.g., slot data, lock state, coordinator data). This improves type safety, IDE autocompletion, and code readability.

**Why not Voluptuous?** Voluptuous is for validation, not object instantiation. Other options like `dacite` or Pydantic add dependencies.

**Example implementation:**

```python
@dataclass
class SlotConfig:
    name: str
    pin: str
    enabled: bool = True
    calendar: str | None = None
    number_of_uses: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> SlotConfig:
        return cls(
            name=data[CONF_NAME],
            pin=data[CONF_PIN],
            enabled=data.get(CONF_ENABLED, True),
            calendar=data.get(CONF_CALENDAR),
            number_of_uses=data.get(CONF_NUMBER_OF_USES),
        )

@dataclass
class LCMConfig:
    locks: list[str]
    slots: dict[int, SlotConfig]

    @classmethod
    def from_entry(cls, entry: ConfigEntry) -> LCMConfig:
        return cls(
            locks=get_entry_data(entry, CONF_LOCKS, []),
            slots={
                int(k): SlotConfig.from_dict(v)
                for k, v in get_entry_data(entry, CONF_SLOTS, {}).items()
            },
        )
```

**Places to audit for dict-to-dataclass conversion:**

- `config_entry.data` / `config_entry.options` access patterns
- `get_entry_data()` / `get_slot_data()` return values
- Coordinator `self.data` structure
- Lock provider internal state

## Features

- Manual sync services (per-slot and bulk).
- Better out-of-sync visibility in the UI.

## Docs

- Keep `AGENTS.md` and `CLAUDE.md` in sync after architecture changes.
