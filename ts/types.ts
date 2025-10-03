import { EntityRegistryEntry } from './ha_type_stubs';

export interface LockCodeManagerEntityEntry extends EntityRegistryEntry {
    key: string;
    lockEntityId?: string;
    slotNum: number;
}

export interface LockCodeManagerStrategyConfig {
    include_code_slot_sensors?: boolean;
    include_in_sync_sensors?: boolean;
}

export interface LockCodeManagerDashboardStrategyConfig extends LockCodeManagerStrategyConfig {
    type: 'custom:lock-code-manager';
}

export interface SlotMapping {
    calendarEntityId: string | null | undefined;
    codeEventEntity?: LockCodeManagerEntityEntry;
    codeSensorEntities: LockCodeManagerEntityEntry[];
    conditionEntities: LockCodeManagerEntityEntry[];
    inSyncEntities: LockCodeManagerEntityEntry[];
    mainEntities: LockCodeManagerEntityEntry[];
    pinActiveEntity?: LockCodeManagerEntityEntry;
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

export interface LockCodeManagerConfigEntryData {
    locks: string[];
    slots: { [key: number]: string | null };
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
