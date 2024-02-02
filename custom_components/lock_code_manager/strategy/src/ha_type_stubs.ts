import { HassEntities, MessageBase } from 'home-assistant-js-websocket';

export interface HomeAssistant {
  states: HassEntities;
  callWS<T>(msg: MessageBase): Promise<T>; // eslint-disable-line typescript-sort-keys/interface
}

export interface ConfigEntry {
  entry_id: string;
  title: string;
}

export interface EntityRegistryEntry {
  config_entry_id: string;
  entity_id: string;
  unique_id: string;
}
