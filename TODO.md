# TODO

## New Items

- Unify design across slot and lock data cards, with a preference towards the slot card design.
- **Config flow: lock reset step** — Add a config flow step that checks for existing
  unmanaged codes on the lock. Inform the user the lock will be reset before configuring.
  Either they cancel or proceed and all slots are cleared. LCM will re-set its managed
  codes immediately after.
- **Config flow: conflicting integration detection** — Check for Keymaster or other
  code management integrations at setup and warn the user.
- **"Clear all unmanaged" UI action** — Add a button or service to clear all unmanaged
  code slots on a lock, so users can clean up stale codes without manual intervention.
- Test visual editor for both cards.

## Refactors and Maintenance

### Simplify Code

#### Dual Storage and Config Entry Patterns

1. **Dual storage pattern** (`data` + `options` in config entries) - Can this be
   simplified?
2. **Clarify data vs options usage** — Document and standardize when to read from
   `config_entry.data` vs `config_entry.options`. Current understanding: prefer
   `options` only within the config entry update listener during options updates;
   elsewhere use `data` to avoid mid-update inconsistencies.

#### Sync Manager Follow-ups

- Consider coordinator owning sync managers instead of binary sensor entities
  (manager lifecycle would survive entity recreation during config updates)

### Convert Config and Internal Dicts to Dataclasses

Convert config entry data to typed dataclasses with `from_dict`/`from_entry`
class methods. Use object instances internally instead of iterating through raw
config dicts. Audit codebase for other complex dicts that would benefit from
dataclass conversion (e.g., slot data, lock state, coordinator data). This
improves type safety, IDE autocompletion, and code readability.

**Why not Voluptuous?** Voluptuous is for validation, not object instantiation.
Other options like `dacite` or Pydantic add dependencies.

### Add Optional Flags to `get_config_entry_data` Websocket Command

Not all callers need all the data from `get_config_entry_data`. Add optional
`include_entities` and `include_locks` flags (default `True` for backwards
compatibility) to skip expensive entity registry queries when not needed.

### Entity Registry Change Detection

Track entity registry updates and warn if LCM entities change entity IDs (reload
required).

### Add Provider Diagnostic Data Method

Add `get_diagnostic_data()` method to `BaseLock` for exposing provider-specific
diagnostic information.

### Drift Detection Failure Alerting

Add mechanism to alert users when drift detection consistently fails over
extended periods (e.g., lock offline). Currently failures are logged but there is
no visibility to users or entities.

## Features

### Add Support for Additional Lock Providers

**Current Providers:**

- Akuvox (`akuvox.py`) - via Local Akuvox custom integration
- Matter (`matter.py`)
- Schlage (`schlage.py`)
- Z-Wave JS (`zwave_js.py`)
- Virtual (`virtual.py`) - for testing only

**Open PRs:**

- **ZHA** (#739) - Zigbee Home Automation
- **MQTT/Zigbee2MQTT** (#740) - MQTT-based locks

**Potential Future Providers:**

- **Nuki** - Popular in Europe
- **SwitchBot** - Growing popularity
- **SmartThings** - Large user base

**Cannot Be Supported** (see README for details):

- `esphome` - No user code API in ESPHome
- `august`, `yale`, `yalexs_ble`, `yale_smart_alarm` - Library limitations

See `AGENTS.md` for implementation approach and `BaseLock` interface.

### Custom Jinja Templates

Ship a `.jinja` file with helper macros for LCM entity resolution (e.g.
`lcm_slot_entities(config_entry_id, slot_num)` to get PIN, name, enabled,
active entity IDs without regex matching). HA loads `.jinja` files from
`config/custom_templates/` at startup and they're used via
`{% from 'lcm.jinja' import macro_name %}`. LCM could auto-install the
file to `custom_templates/` during setup, similar to how it installs
Lovelace resources.

### Improve Dashboard UI/UX

- Bulk operations (enable/disable multiple slots)
- Import/export slot configuration

### Add Service Actions

- `lock_code_manager.set_temporary_code` - Create time-limited PIN
- `lock_code_manager.generate_pin` - Auto-generate secure PIN
- `lock_code_manager.bulk_enable` / `bulk_disable` - Enable/disable multiple slots
- Note: `hard_refresh_usercodes` service already exists for lock-wide refresh.

### Configuration Validation

- Warn if PIN is too simple (e.g., "1234", "0000")
- Warn if multiple slots use same PIN
- Validate PIN format against lock requirements
- Check for slot conflicts across config entries

## Docs and Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
