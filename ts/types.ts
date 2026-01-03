import { EntityRegistryEntry } from './ha_type_stubs';

export interface LockCodeManagerEntityEntry extends EntityRegistryEntry {
    key: string;
    lockEntityId?: string;
    slotNum: number;
}

export interface LockCodeManagerStrategyConfig {
    code_data_view_code_display?: CodeDisplayMode;
    include_code_data_view?: boolean;
    include_code_slot_sensors?: boolean;
    include_in_sync_sensors?: boolean;
    /** Use the new streamlined slot cards instead of entities cards (default: true for new installs) */
    use_slot_cards?: boolean;
}

export interface LockCodeManagerDashboardStrategyConfig extends LockCodeManagerStrategyConfig {
    type: 'custom:lock-code-manager';
}

export interface SlotMapping {
    calendarEntityId: string | null | undefined;
    codeEventEntity: LockCodeManagerEntityEntry;
    codeSensorEntities: LockCodeManagerEntityEntry[];
    conditionEntities: LockCodeManagerEntityEntry[];
    inSyncEntities: LockCodeManagerEntityEntry[];
    mainEntities: LockCodeManagerEntityEntry[];
    pinActiveEntity: LockCodeManagerEntityEntry;
    slotNum: number;
}

export interface LockCodeManagerViewStrategyConfig extends LockCodeManagerStrategyConfig {
    config_entry_id?: string;
    config_entry_title?: string;
    type: 'custom:lock-code-manager';
}

export interface LockCodeManagerEntitiesResponse {
    config_entry: ConfigEntryJSONFragment;
    entities: EntityRegistryEntry[];
}

export interface LockCoordinatorSlotData {
    /**
     * Whether the slot is currently active (enabled + conditions met).
     * True = active, False = inactive (conditions blocking), undefined = unknown
     */
    active?: boolean;
    code: number | string | null;
    /** Present when masked (code is null but slot has a code) */
    code_length?: number;
    /** Config entry ID of the LCM instance managing this slot (for navigation) */
    config_entry_id?: string;
    /** Configured PIN from LCM (for disabled slots with no code on lock) */
    configured_code?: string;
    /** Length of configured PIN when masked */
    configured_code_length?: number;
    /**
     * Whether the enabled switch is ON.
     * True = enabled, False = disabled by user, undefined = unknown
     */
    enabled?: boolean;
    /** Whether the code is in sync with the lock */
    in_sync?: boolean;
    /** True if slot is managed by LCM */
    managed?: boolean;
    /** Slot name from LCM configuration, if set */
    name?: string;
    slot: number | string;
}

export interface LockCoordinatorData {
    lock_entity_id: string;
    lock_name: string;
    slots: LockCoordinatorSlotData[];
}

export interface LockCodeManagerConfigEntryData {
    locks: string[];
    slots: { [key: number]: string | null };
}

export type CodeDisplayMode = 'masked' | 'unmasked' | 'masked_with_reveal';

export interface LockCodesCardConfig {
    code_display?: CodeDisplayMode;
    lock_entity_id: string;
    title?: string;
    type: 'custom:lcm-lock-codes-card';
}

export interface LockCodeManagerSlotCardConfig {
    /** How to display code/PIN values (consistent with lock-data card) */
    code_display?: CodeDisplayMode;
    /** Sections to show collapsed by default */
    collapsed_sections?: ('conditions' | 'lock_status')[];
    /** Config entry ID for the LCM instance (use this OR config_entry_title) */
    config_entry_id?: string;
    /** Config entry title for the LCM instance (use this OR config_entry_id) */
    config_entry_title?: string;
    /** Show code sensors (actual code on lock) in lock status section (default: true) */
    show_code_sensors?: boolean;
    /** Show conditions section (default: true) */
    show_conditions?: boolean;
    /** Show lock status section (default: true) */
    show_lock_status?: boolean;
    /** Show sync status per lock in lock status (default: true) */
    show_lock_sync?: boolean;
    /** Slot number to display */
    slot: number;
    type: 'custom:lcm-slot-card';
}

export interface SlotCardLockStatus {
    /** Current code on the lock */
    code: string | null;
    /** Code length when masked */
    code_length?: number;
    entity_id: string;
    /** Whether code is synced to lock */
    in_sync: boolean;
    /** Last sync timestamp (ISO) */
    last_synced?: string;
    name: string;
}

/** Calendar event information */
export interface CalendarEventInfo {
    /** Whether there's an active event now */
    active: boolean;
    /** Event end time (ISO datetime) */
    end_time?: string;
    /** Event title/summary */
    summary?: string;
}

/** Next calendar event information */
export interface CalendarNextEventInfo {
    /** Event start time (ISO datetime) */
    start_time: string;
    /** Event title/summary */
    summary?: string;
}

export interface SlotCardConditions {
    /** Current calendar event info */
    calendar?: CalendarEventInfo;
    /** Calendar entity ID for reference */
    calendar_entity_id?: string;
    /** Next upcoming calendar event */
    calendar_next?: CalendarNextEventInfo;
    /** Number of uses remaining */
    number_of_uses?: number;
}

export interface SlotCardEntities {
    active?: string | null;
    enabled?: string | null;
    name?: string | null;
    number_of_uses?: string | null;
    pin?: string | null;
}

export interface SlotCardData {
    /** Whether conditions are met and code is active */
    active: boolean | null;
    conditions: SlotCardConditions;
    /** LCM config entry ID (for navigation) */
    config_entry_id: string;
    /** LCM config entry title (for display) */
    config_entry_title: string;
    /** Whether the slot is enabled by user */
    enabled: boolean | null;
    /** Entity IDs for updating slot fields */
    entities?: SlotCardEntities;
    /** Event entity ID for PIN usage events */
    event_entity_id?: string;
    /** Last PIN used timestamp (ISO) */
    last_used?: string;
    /** Lock name where PIN was last used */
    last_used_lock?: string;
    /** Per-lock sync status */
    locks: SlotCardLockStatus[];
    name: string;
    /** The PIN value (actual or masked) */
    pin: string | null;
    /** PIN length when masked */
    pin_length?: number;
    slot_num: number;
}

export interface LockInfo {
    entity_id: string;
    name: string;
}

export interface GetLocksResponse {
    locks: LockInfo[];
}

export interface ConfigEntryJSONFragment {
    disabled_by: string;
    domain: string;
    entry_id: string;
    pref_disable_new_entities: boolean;
    pref_disable_polling: boolean;
    reason: string | null;
    source: string;
    state: string;
    supports_options: boolean;
    supports_remove_device: boolean;
    supports_unload: boolean;
    title: string;
}

export type GetConfigEntriesResponse = ConfigEntryJSONFragment[];
