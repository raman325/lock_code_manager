import { HassConfig, HassEntities, MessageBase } from 'home-assistant-js-websocket';

export interface ConfigEntry {
    entry_id: string;
    title: string;
}

export interface EntityRegistryEntry {
    config_entry_id: string;
    entity_id: string;
    name: string;
    original_name: string;
    unique_id: string;
}

export interface HomeAssistant {
    config: HassConfig;
    resources: object;
    states: HassEntities;
    callWS<T>(msg: MessageBase): Promise<T>; // eslint-disable-line typescript-sort-keys/interface
}

export interface LovelaceBaseViewConfig {
    back_path?: string;
    background?: string;
    icon?: string;
    index?: number;
    panel?: boolean;
    path?: string;
    subview?: boolean;
    theme?: string;
    title?: string;
    visible?: boolean;
}

export interface LovelaceCardConfig {
    [key: string]: unknown;
    index?: number;
    type: string;
    view_index?: number;
    view_layout?: unknown;
}

export interface LovelaceResource {
    id: string;
    type: 'css' | 'js' | 'module' | 'html';
    url: string;
}

export interface LovelaceViewConfig extends LovelaceBaseViewConfig {
    badges?: Array<string | object>;
    cards?: object[];
    type?: string;
}
