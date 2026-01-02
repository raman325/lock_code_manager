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
    /** Configured PIN from LCM (for disabled slots with no code on lock) */
    configured_code?: string;
    /** Length of configured PIN when masked */
    configured_code_length?: number;
    /**
     * Whether the enabled switch is ON.
     * True = enabled, False = disabled by user, undefined = unknown
     */
    enabled?: boolean;
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

export interface LockCodeManagerLockDataCardConfig {
    code_display?: CodeDisplayMode;
    lock_entity_id: string;
    title?: string;
    type: 'custom:lock-code-manager-lock-data';
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
