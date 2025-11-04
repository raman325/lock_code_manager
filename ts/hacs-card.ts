import { LitElement, TemplateResult, html } from 'lit';
import { until } from 'lit-html/directives/until';

import { HomeAssistant } from './ha_type_stubs';

async function downloadHacsRepository(hass: HomeAssistant, repository: string): Promise<void> {
    await hass.callWS<void>({ repository, type: 'hacs/repository/download' });
}

async function getHacsRepositoryId(
    hass: HomeAssistant,
    repoName: string
): Promise<string | undefined> {
    const repo = await hass
        .callWS<{ full_name: string; id: string }[]>({ type: 'hacs/repositories/list' })
        .then((repos) => repos.find((_repo) => _repo.full_name === repoName));
    return repo.id;
}

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

customElements.define('custom:hacs-card', HacsCard);
