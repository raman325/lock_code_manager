import { ConfigEntry, EntityRegistryEntry } from './ha_type_stubs';

export interface LockCodeManagerEntityEntry extends EntityRegistryEntry {
    key: string;
    lockEntityId?: string;
    slotNum: number;
}

export interface LockCodeManagerStrategyConfig {
    include_code_slot_sensors?: boolean;
}

export interface LockCodeManagerDashboardStrategyConfig extends LockCodeManagerStrategyConfig {
    type: 'custom:lock-code-manager';
}

export interface SlotMapping {
    codeEventEntityId: string;
    codeSensorEntityIds: string[];
    conditionEntityIds: string[];
    mainEntityIds: string[];
    pinShouldBeEnabledEntity: LockCodeManagerEntityEntry;
    slotNum: number;
}

export interface LockCodeManagerViewStrategyConfig extends LockCodeManagerStrategyConfig {
    config_entry_id?: string;
    config_entry_title?: string;
    type: 'custom:lock-code-manager';
}

export type LockCodeManagerEntitiesResponse = [string, string, EntityRegistryEntry[]];

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
