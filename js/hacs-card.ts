import { LitElement, TemplateResult, html } from 'lit';
import { until } from 'lit-html/directives/until';

import { HomeAssistant } from './ha_type_stubs';
import { getHacsRepositoryId } from './helpers';

// hass.callWS({type: "hacs/repositories/list"})).filter((repo) => repo.full_name == "thomasloven/lovelace-fold-entity-row")

interface hacsCardConfig {
  repository_name: string;
}

class HacsCard extends LitElement {
  _hass: HomeAssistant;
  _config: hacsCardConfig;

  set hass(hass: HomeAssistant) {
    this._hass = hass;
  }

  setConfig(config: hacsCardConfig) {
    this._config = config;
  }

  protected render(): TemplateResult {
    return html` ${until(getHacsRepositoryId(this._hass, this._config.repository_name))}`;
  }
}

customElements.define('hacs-card', HacsCard);
