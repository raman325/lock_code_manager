# TODO

## Testing

- Test strategy UI module end-to-end (resource registration, YAML mode, reload).
- Add provider tests for integrations beyond Z-Wave JS and virtual.
- Add Z-Wave JS provider tests (requires Z-Wave JS door lock mocks/fixtures).
- Test rate limiting and connection failure timing in live environment.

## Refactors / Maintenance

### Entity registry change detection

Track entity registry updates and warn if LCM entities change entity IDs (reload required).

### Drift detection failure alerting

Add mechanism to alert users when drift detection consistently fails over extended periods (e.g., lock offline). Currently failures are logged but there's no visibility to users or entities.

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
- Add websocket commands to expose per-lock coordinator data (used slots/PINs) and confirm whether an existing card (e.g., template card) can render it before building a custom card for tabular lock data display.

## Docs

- Keep `AGENTS.md` and `CLAUDE.md` in sync after architecture changes.
