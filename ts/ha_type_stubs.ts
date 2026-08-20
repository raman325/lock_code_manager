import { Connection, HassConfig, HassEntities, MessageBase } from 'home-assistant-js-websocket';

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

/** What a service returns when asked for its response. */
export interface ServiceCallResponse {
    response?: unknown;
}

export interface HomeAssistant {
    config: HassConfig;
    connection: Connection;
    resources: object;
    states: HassEntities;
    // Home Assistant's own signature, which carries more than the void this
    // used to claim: a service declaring a response hands it back here, and
    // `generate_pin` is one.
    // eslint-disable-next-line typescript-sort-keys/interface -- Methods grouped logically, not alphabetically
    callService(
        domain: string,
        service: string,
        data?: object,
        target?: object,
        notifyOnError?: boolean,
        returnResponse?: boolean
    ): Promise<ServiceCallResponse>;
    callWS<T>(msg: MessageBase): Promise<T>; // eslint-disable-line typescript-sort-keys/interface -- Methods grouped logically, not alphabetically
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

export interface LovelaceSectionStrategyConfig {
    [key: string]: unknown;
    type: string;
}

export interface LovelaceSectionConfig {
    cards?: LovelaceCardConfig[];
    strategy?: LovelaceSectionStrategyConfig;
    title?: string;
    type?: string;
}

export interface LovelaceViewConfig extends LovelaceBaseViewConfig {
    badges?: Array<string | object>;
    cards?: object[];
    sections?: LovelaceSectionConfig[];
    type?: string;
}
