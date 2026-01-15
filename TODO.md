# TODO

## New Items

- Unify design across slot and lock data cards, with a preference towards the slot card design.
- Test visual editor for both cards.

## Testing

- Strategy UI module has unit tests in `ts/*.test.ts` and Python tests for
  resource registration/unload in `tests/test_init.py`; still need end-to-end
  UI coverage (Lovelace resource registration + reload in a real frontend).
- Add provider tests when new integrations beyond Z-Wave JS and virtual are added.
- Add Z-Wave JS provider tests (requires Z-Wave JS door lock mocks/fixtures).
- Test rate limiting and connection failure timing in live environment.

## Refactors and Maintenance

### Simplify Tests

**Current Issues:**

- `tests/common.py` has some duplicated mock setup code
- Some tests may have redundant assertions
- Test fixtures could potentially be more reusable across test files

**Potential Improvements:**

- Consolidate duplicate test setup code into shared fixtures
- Review test naming conventions for clarity
- Consider parametrized tests where multiple similar tests exist
- Reduce test boilerplate with helper functions

**Estimated Effort:** Medium (4-8 hours)
**Priority:** Medium
**Status:** Not started

### Simplify Code

#### Remove Other Unnecessary Complexity

**Areas to Review:**

1. **Dual storage pattern** (`data` + `options` in config entries) - Can this be
   simplified?
2. **Entity unique ID format** - Is `{entry_id}|{slot}|{type}` optimal?
3. **Multiple coordinator instances** - One per lock - could this be unified?
4. **Internal method wrappers** - `async_internal_*` methods with locks - are
   all necessary?

#### Clarify Config Entry Data vs Options Usage

Document and standardize when to read from `config_entry.data` vs
`config_entry.options`. Current understanding: prefer `options` only within the
config entry update listener during options updates; elsewhere use `data` to
avoid mid-update inconsistencies. Add helper(s) or guidance to reduce ambiguity
and prevent regressions.

**Specific Items:**

- Review if all `_get_entity_state()` calls can be simplified
- Evaluate if `_entity_id_map` dictionary caching is worth the complexity
- Consider if provider `async_internal_*` wrapper methods can be simplified
- Review entity base classes for potential consolidation

**Estimated Effort:** High (20+ hours)
**Priority:** Low-Medium
**Status:** Not started

#### Move Sync Logic to Coordinator

**Current Architecture Issues:**

- In-sync binary sensor reads PIN config from text entities via
  `_get_entity_state()`
- Reads lock state from coordinator data
- Compares them and triggers sync operations (set_usercode/clear_usercode)
- Cross-entity dependencies create race conditions during startup
- Current guard uses `_attr_is_on is None` to avoid initial sync operations

**Proposed Solution:**
Move sync logic entirely into the coordinator:

- Coordinator already has access to both desired state (from config) and actual
  state (from lock)
- Coordinator performs sync operations during its `_async_update_data()` cycle
- Binary sensor becomes read-only, just displays coordinator's computed in-sync
  status
- Text/number/switch entities remain as config views

**Example Implementation:**

```python
# In coordinator._async_update_data()
actual_code = await self.provider.get_usercodes()
desired_code = self.config_entry.data[slot]["pin"]

if actual_code != desired_code and slot_enabled:
    await self.provider.set_usercode(slot, desired_code)

return {"in_sync": actual_code == desired_code, "actual_code": actual_code}
```

**Benefits:**

- Eliminates cross-entity state reading
- Removes `_initial_state_loaded` flag and startup detection logic
- No race conditions during startup
- Simpler, more centralized sync logic
- Coordinator is single source of truth

**Considerations:**

- Major architectural change
- Would need to update binary sensor to be read-only
- Config updates still flow through text/switch entities
- Need to ensure coordinator runs sync on config changes

**Estimated Effort:** High (16-24 hours)
**Priority:** Medium
**Status:** Not started

### Convert Config and Internal Dicts to Dataclasses

Convert config entry data to typed dataclasses with `from_dict`/`from_entry`
class methods. Use object instances internally instead of iterating through raw
config dicts. Audit codebase for other complex dicts that would benefit from
dataclass conversion (e.g., slot data, lock state, coordinator data). This
improves type safety, IDE autocompletion, and code readability.

**Why not Voluptuous?** Voluptuous is for validation, not object instantiation.
Other options like `dacite` or Pydantic add dependencies.

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

### Handle Disabled Lock Slots

**Context:** Z-Wave locks have `userIdStatus` which can be `Enabled`, `Available`,
or `Disabled`. Currently the coordinator only stores the code value, not the status.

**Investigation Needed:**

1. Test what happens when LCM tries to set a user code on a slot with
   `userIdStatus=Disabled`
2. Determine if we need to explicitly enable the slot before setting a code
3. Check if different lock brands handle disabled slots differently

**Related:** See "Enhance Coordinator Data Model" below.

**Estimated Effort:** Medium (4-8 hours)
**Priority:** Medium
**Status:** Not started

### Enhance Coordinator Data Model with Slot Status

**Current State:** Coordinator stores `{slot: code}` mapping only.

**Proposed Change:** Store `{slot: {code, status}}` where status is a generic
LCM enum that providers map to.

**Generic Status Enum:**

```python
class SlotStatus(StrEnum):
    ENABLED = "enabled"    # Slot has active code
    AVAILABLE = "available"  # Slot can be used but is empty
    DISABLED = "disabled"  # Slot cannot be used (locked out)
```

**Provider Mapping:**

- Z-Wave JS: Maps `userIdStatus` (Enabled/Available/Disabled) → `SlotStatus`
- Other providers: Map their equivalent states to the generic enum

**Benefits:**

- Frontend can distinguish between "slot is empty" vs "slot is disabled"
- Better handling of disabled slots in sync logic
- More accurate representation of lock state
- Provider-agnostic data model

**Implementation:**

1. Define `SlotStatus` enum in `const.py`
2. Update Z-Wave JS provider to track userIdStatus and map to `SlotStatus`
   (currently filtered out in `on_value_updated`)
3. Change coordinator data schema from `dict[int, str]` to
   `dict[int, SlotData]` where `SlotData` includes code and status
4. Update all consumers of coordinator data (binary_sensor, sensor, websocket)
5. Update frontend types and rendering

**Estimated Effort:** High (12-16 hours)
**Priority:** Medium
**Status:** Not started

### Entity Registry Change Detection

Track entity registry updates and warn if LCM entities change entity IDs (reload
required).

### Drift Detection Failure Alerting

Add mechanism to alert users when drift detection consistently fails over
extended periods (e.g., lock offline). Currently failures are logged but there is
no visibility to users or entities.

## Features

### Add Schedule and Entity Condition Types

**Problem:** Users report that managing calendars in HA is too painful for simple
recurring schedules. Calendars are overkill for "weekdays 9-5" patterns.

**Proposed Solution:** Add alternative condition types alongside `calendar`:

| Condition Type | Use Case |
| -------------- | -------- |
| `calendar` | One-time events, external calendar sync (existing) |
| `schedule` | Recurring weekly patterns via HA schedule helper |
| `entity` | Custom logic via any binary sensor/input_boolean |

**Implementation:**

1. **Config Flow Changes:**
   - Add condition type selector (calendar/schedule/entity)
   - Show appropriate entity selector based on type
   - `schedule`: EntitySelector filtered to `schedule` domain
   - `entity`: EntitySelector filtered to `binary_sensor`, `input_boolean`

2. **Binary Sensor Changes:**
   - Update `_get_entity_state()` to handle different condition types
   - Schedule entities: check if current time is within schedule (state = "on")
   - Entity conditions: directly use entity state

3. **Slot Data Model:**
   - Add `condition_type` field: `calendar | schedule | entity`
   - Add `condition_entity` field (alternative to `calendar`)
   - Maintain backward compatibility with existing `calendar` field

4. **Frontend Updates:**
   - Update slot card to show condition type
   - Show appropriate icon/label for each type

**Benefits:**

- Schedule helper is much simpler for recurring patterns
- Entity condition allows maximum flexibility (templates, automations)
- Calendar remains available for complex/one-time events

**Estimated Effort:** Medium (8-12 hours)
**Priority:** Medium-High
**Status:** Not started

### Advanced Calendar Configuration

**Feature Request:** Allow slot number and PIN to be configured directly in
calendar event metadata, eliminating need for separate slot configuration.

**Design Considerations:**

**Current Behavior:**

- Slot configured in config flow with fixed PIN
- Calendar event (when present) only controls whether slot is active/inactive
- PIN and slot number are static configuration

**Proposed Behavior:**

- Calendar event contains slot number and PIN in its metadata
- User configures regex/pattern to extract slot number and PIN from:
  - Event title (e.g., "Slot 3: 1234")
  - Event description
  - Event location
  - Custom calendar properties

**Implementation Requirements:**

1. **Config Flow Changes:**
   - Add "advanced calendar mode" toggle per slot
   - When enabled, show pattern configuration UI
   - Pattern fields: slot number regex, PIN regex, which field to parse
     (title/description/location)
2. **Entity Changes:**
   - Calendar event listener needs to parse event metadata
   - Extract slot number and PIN based on user-defined patterns
   - Validate extracted values (numeric slot, PIN format)
3. **Binary Sensor Changes:**
   - Check if calendar event contains valid extracted values
   - Use extracted PIN instead of configured PIN
   - Handle multiple calendar events with different slots

**Example Patterns:**

- Title: "Guest Access: Slot 5, PIN 9876" -> Slot: `\d+`, PIN: `\d{4}$`
- Description: "Code: 1234 for slot 3" -> Slot: `slot (\d+)`, PIN: `Code: (\d+)`
- Location: "5:1234" -> Slot: `^(\d+):`, PIN: `:(\d+)$`

**Challenges:**

- Multiple calendar events with different slots
- Error handling for invalid patterns
- UI for testing patterns
- Backward compatibility with existing simple calendar mode

**Estimated Effort:** Very High (40+ hours)
**Priority:** Medium
**Status:** Not started

### Add Relevant New Home Assistant Core Features

**Analysis:** Review Home Assistant release notes from 2024.1 through 2025.10
and integrate relevant new features.

**Key Features to Evaluate (2024-2025):**

1. **Entity Category Enhancements** (2024.x)
   - New entity categories available
   - **Action:** Review entity category assignments
2. **Selector Improvements** (2024.x)
   - New selector types for config flow
   - **Action:** Review config flow UI for better selectors
3. **Repair Platform** (2024.x)
   - Notify users of configuration issues
   - **Action:** Consider adding repairs for common misconfigurations

**Already Evaluated (No Changes Needed):**

- **Config Entry Runtime Data**: `hass.data[DOMAIN]` correctly holds global
  cross-config-entry state (lock registry, resource flag). Per-entry data
  already uses `runtime_data`.
- **DataUpdateCoordinator `_async_setup()`**: All coordinator setup is
  synchronous (timer registration). No async initialization needed.
- **Config Entry Diagnostics**: No significant internal state to expose beyond
  what's already visible via entities and config entries.

**Estimated Effort:** Low-Medium (4-8 hours)
**Priority:** Medium
**Status:** Partially evaluated

### Add Support for Additional Lock Providers

**Current Providers:**

- Z-Wave JS (`zwave_js.py`)
- Virtual (`virtual.py`) - for testing only

**Available Lock Integrations in Home Assistant Core:**

**Smart Home Platforms:**

- `deconz` - deCONZ (Zigbee/Z-Wave gateway)
- `esphome` - ESPHome devices
- `homematic` - Homematic (CCU)
- `homematicip_cloud` - Homematic IP Cloud
- `homekit_controller` - HomeKit accessories
- `matter` - Matter protocol
- `mqtt` - MQTT locks
- `smartthings` - SmartThings
- `zha` - Zigbee Home Automation
- `zwave_js` - Z-Wave JS (already supported)
- `zwave_me` - Z-Wave.Me

**Brand-Specific Integrations:**

- `abode` - Abode Security
- `august` - August Smart Locks
- `bmw_connected_drive` - BMW Connected Drive
- `dormakaba_dkey` - Dormakaba dkey
- `igloohome` - igloohome
- `kiwi` - Kiwi (Eufy)
- `loqed` - Loqed Smart Lock
- `nuki` - Nuki Smart Lock
- `schlage` - Schlage Encode
- `sesame` - Sesame Smart Lock
- `switchbot` - SwitchBot Lock
- `switchbot_cloud` - SwitchBot Cloud
- `tedee` - Tedee Smart Lock
- `yale` - Yale Access (August partnership)
- `yalexs_ble` - Yale/August BLE
- `yolink` - YoLink

**Security Systems:**

- `simplisafe` - SimpliSafe
- `verisure` - Verisure
- `yale_smart_alarm` - Yale Smart Alarm

**Vehicle Integrations:**

- `subaru` - Subaru Starlink
- `tesla_fleet` - Tesla Fleet API
- `teslemetry` - Teslemetry
- `tessie` - Tessie (Tesla)
- `starline` - StarLine

**Other Integrations:**

- `fibaro` - Fibaro
- `freedompro` - Freedompro
- `insteon` - Insteon
- `isy994` - Universal Devices ISY
- `keba` - KEBA EV Charger
- `overkiz` - Overkiz (Somfy TaHoma)
- `surepetcare` - Sure Petcare
- `unifiprotect` - UniFi Protect
- `vera` - Vera
- `wallbox` - Wallbox EV Charger
- `xiaomi_aqara` - Xiaomi Aqara

**Utility/Helper Integrations:**

- `demo` - Demo platform
- `group` - Lock groups
- `kitchen_sink` - Testing platform
- `switch_as_x` - Convert switches to locks
- `template` - Template locks
- `homee` - Homee gateway

**Recommended Priorities for Support:**

**High Priority** (popular, widely used):

1. **ZHA (Zigbee Home Automation)** - Very popular, supports many lock brands
2. **Matter** - Future-proof, industry standard
3. **ESPHome** - DIY community, custom locks
4. **MQTT** - Generic protocol, many custom implementations

**Medium Priority** (brand-specific, popular):

1. **August/Yale** (`august`, `yale`, `yalexs_ble`) - Popular smart lock brand
2. **Nuki** - Popular in Europe
3. **Schlage** - Popular in North America
4. **SwitchBot** - Growing popularity

**Low Priority** (niche or less common):

- Vehicle locks (Tesla, BMW, Subaru) - different use case
- Security system locks - usually managed by their own systems
- Utility integrations (template, group) - may work without specific provider

**Implementation Approach:**

For each new provider:

1. Create `providers/INTEGRATION_NAME.py`
2. Subclass `BaseLock`
3. Implement required methods
4. Add integration-specific event listeners
5. Add tests in `tests/INTEGRATION_NAME/test_provider.py`
6. Update documentation

**Estimated Effort per Provider:** Medium (6-12 hours each)
**Priority:** Medium-High
**Status:** Not started
**Recommended First Addition:** ZHA (Zigbee)

### Improve Dashboard UI/UX

**Ideas:**

- Add visual indicator when codes are out of sync
- Bulk operations (enable/disable multiple slots)
- Import/export slot configuration
- QR code generation for PIN sharing
- History view showing when codes were used
- Slot templates for quick configuration

**Estimated Effort:** High (20+ hours)
**Priority:** Medium
**Status:** Not started

### Add Service Actions

**Ideas:**

- `lock_code_manager.set_temporary_code` - Create time-limited PIN
- `lock_code_manager.generate_pin` - Auto-generate secure PIN
- `lock_code_manager.bulk_enable` - Enable multiple slots at once
- `lock_code_manager.bulk_disable` - Disable multiple slots at once
- `lock_code_manager.copy_slot` - Copy configuration from one slot to another
- Note: `hard_refresh_usercodes` service already exists for lock-wide refresh.

**Estimated Effort:** Medium (8-16 hours)
**Priority:** Medium-Low
**Status:** Not started

### Enhanced Notifications

**Ideas:**

- Notify when PIN is used (already have event entity, could add notification
  action)
- Notify when code goes out of sync
- Notify when calendar event starts (PIN becomes active)
- Notify when number of uses is depleted

**Estimated Effort:** Medium (6-10 hours)
**Priority:** Low
**Status:** Not started

### Configuration Validation

**Ideas:**

- Warn if PIN is too simple (e.g., "1234", "0000")
- Warn if multiple slots use same PIN
- Validate PIN format against lock requirements
- Check for slot conflicts across config entries

**Estimated Effort:** Low-Medium (4-8 hours)
**Priority:** Medium
**Status:** Not started

### Additional Feature Notes

- Better out-of-sync visibility in the UI.
- Websocket commands expose lock data via `lock_code_manager/subscribe_lock_codes`
  and slot data via `lock_code_manager/subscribe_code_slot`. The `lcm-lock-codes`
  and `lcm-slot` frontend cards subscribe to these respectively.

## Docs and Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
