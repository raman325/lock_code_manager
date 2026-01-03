# TODO

## High Priority Investigation

### Fix Locks Out of Sync Issue

**Problem:** Users report that locks go out of sync and the integration does not
automatically re-sync them. The in-sync binary sensor shows "off" but the
integration does not trigger automatic sync operations to fix the issue.

**Initial Analysis:** Based on PR review comments, the current auto-sync logic
has issues:

**Current Behavior (in `binary_sensor.py`):**

- `_async_update_state()` runs on:
  - Initial load (sets in-sync state without operations)
  - Entity state changes (tracked via `async_track_state_change_event`)
- When out of sync, `_async_update_state()` performs a set/clear operation and
  refreshes the coordinator.
- `async_update()` is still used for periodic polling and retry callbacks.
  It only proceeds when:
  - The internal `_lock` is not locked
  - The entity is out of sync (`self.is_on` is False)
  - The lock state is available
  - The coordinator last update succeeded (or a retry is active)

**Issues Identified:**

1. **Sync triggers may miss coordinator-only changes:** State change events
   trigger sync attempts, but coordinator updates without any entity state
   changes still rely on polling (`should_poll=True`).
2. **Lock condition problematic:** `self._lock.locked()` check may prevent
   syncing when it should happen
3. **No user-initiated sync:** There is a `hard_refresh_usercodes` service, but
   no per-slot/manual sync operation

**Existing Coverage:**

- Tests cover startup flapping prevention, out-of-sync detection, retry
  scheduling, and disconnected lock handling (see `tests/test_binary_sensor.py`).

**Root Cause Areas to Investigate:**

1. **When does `async_update()` actually get called?**
   - Periodic polling (entity `should_poll=True`)
   - Retry callbacks (after `LockDisconnected` or refresh errors)
2. **What does `self._lock.locked()` check?**
   - This is an `asyncio.Lock()`, not lock state
   - Prevents concurrent sync operations
   - May block legitimate sync attempts if lock held
3. **Why do some state changes still not trigger sync?**
   - `_async_update_state()` exits early if entities are missing/unavailable,
     the coordinator update failed, or the event is not for a tracked entity.
   - Investigate whether coordinator-only changes should enqueue a sync.

**Proposed Solutions:**

#### Option 1: More Aggressive Auto-Sync

- Remove or adjust `self._lock.locked()` check in `async_update()`
- Add retry logic with exponential backoff
- Trigger sync on any PIN/name/active state change, not just during polling
- Add sync on coordinator refresh success

#### Option 2: Manual Sync Service

- Add `lock_code_manager.sync_slot` service action
- Add `lock_code_manager.sync_all_slots` service action
- Allow users to manually trigger sync when they notice issues
- Could be called from automations

#### Option 3: Better State Change Detection

- Enhance `_async_update_state()` to be more responsive
- Remove guards that prevent sync on legitimate state changes
- Add more detailed logging to understand why syncs do not happen
- Consider firing HA events when sync fails repeatedly

**Investigation Steps:**

1. Add detailed logging to understand when `async_update()` is called
2. Add logging to show why sync operations are skipped
3. Monitor `self._lock.locked()` state to see if it blocks syncs
4. Review state change event handling to ensure it triggers appropriately
5. Test with deliberately out-of-sync locks to reproduce issue

**Files to Modify:**

- `custom_components/lock_code_manager/binary_sensor.py` (`async_update`,
  `_async_update_state`, retry scheduling)
- `custom_components/lock_code_manager/__init__.py` (if adding new services)
- `tests/test_binary_sensor.py` (sync behavior, retries, coordinator-only changes)

**Estimated Effort:** High (16-24 hours)
**Priority:** High
**Status:** Not started
**Related Issues:** Review comments on PR indicating locks do not auto-sync

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

1. **Config Entry Runtime Data** (2024.8+)
   - `ConfigEntry.runtime_data` is already used for callbacks/locks/state
   - **Action:** Audit remaining `hass.data[DOMAIN]` usage and decide what
     should stay global vs. move into runtime data
2. **DataUpdateCoordinator `_async_setup()`** (2024.8+)
   - One-time initialization method
   - **Action:** Evaluate if any coordinator setup code should move to
     `_async_setup()`
3. **Entity Category Enhancements** (2024.x)
   - New entity categories available
   - **Action:** Review entity category assignments
4. **Selector Improvements** (2024.x)
   - New selector types for config flow
   - **Action:** Review config flow UI for better selectors
5. **Repair Platform** (2024.x)
   - Notify users of configuration issues
   - **Action:** Consider adding repairs for common misconfigurations
6. **Config Entry Diagnostics** (2024.x)
   - Better debugging information
   - **Action:** Add diagnostics download capability

**Estimated Effort:** Medium-High (12-20 hours)
**Priority:** Medium
**Status:** Not started

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
- Websocket commands already expose slot data via
  `lock_code_manager/subscribe_lock_slot_data`, and the
  `lock-code-data-card` frontend subscribes to it; follow-up is to document the
  API and card usage.

## Docs and Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
