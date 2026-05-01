import {
    mdiCalendar,
    mdiCalendarClock,
    mdiCalendarRemove,
    mdiChevronDown,
    mdiChevronUp,
    mdiDelete,
    mdiEye,
    mdiEyeOff,
    mdiPencil,
    mdiPlus,
    mdiToggleSwitch,
    mdiToggleSwitchOutline
} from '@mdi/js';
import { MessageBase } from 'home-assistant-js-websocket';
import { LitElement, TemplateResult, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';
import { until } from 'lit/directives/until.js';

import { HomeAssistant } from './ha_type_stubs';
import { slotCardStyles } from './slot-card.styles';
import { LcmSubscriptionMixin } from './subscription-mixin';
import {
    CodeDisplayMode,
    ConditionEntityInfo,
    GetConfigEntriesResponse,
    LockCodeManagerSlotCardConfig,
    SLOT_CODE_UNREADABLE,
    SlotCardConditions,
    SlotCardData,
    isSlotEmpty
} from './types';

const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

/** Internal interface for lock sync status display */
interface LockSyncStatus {
    /** Current code on the lock (actual or masked) */
    code: string | null;
    /** Code length when masked */
    codeLength?: number;
    entityId: string;
    inSync: boolean | null;
    /** Last synced timestamp (ISO) */
    lastSynced?: string;
    lockEntityId: string;
    name: string;
    /** Granular sync status: in_sync, out_of_sync, syncing, suspended */
    syncStatus?: string;
}

/** Maps editable field names to their entity key, HA service, and value transform. */
const EDIT_FIELD_CONFIG: Record<
    'name' | 'pin',
    {
        entityKey: keyof NonNullable<SlotCardData['entities']>;
        service: string;
        serviceData: (v: string) => Record<string, unknown>;
    }
> = {
    name: {
        entityKey: 'name',
        service: 'text.set_value',
        serviceData: (v) => {
            return { value: v.trim() };
        }
    },
    pin: {
        entityKey: 'pin',
        service: 'text.set_value',
        serviceData: (v) => {
            return { value: v.trim() };
        }
    }
};

// Base class with subscription mixin
const LcmSlotCardBase = LcmSubscriptionMixin(LitElement);

/**
 * Streamlined slot card for Lock Code Manager.
 *
 * Phase 3: Uses websocket subscription for real-time updates.
 */
class LockCodeManagerSlotCard extends LcmSlotCardBase {
    static styles = slotCardStyles;

    // Note: _revealed, _unsub, _subscribing provided by LcmSubscriptionMixin
    @state() _config?: LockCodeManagerSlotCardConfig;
    @state() _data?: SlotCardData;
    @state() _error?: string;
    @state() private _actionError?: string;
    @state() private _conditionsExpanded = false;
    @state() private _editingField: 'name' | 'pin' | null = null;
    @state() private _lockStatusExpanded = false;

    // Condition dialog state
    @state() private _showConditionDialog = false;
    @state() private _dialogMode: 'add-entity' | 'edit-entity' = 'add-entity';
    @state() private _dialogEntityId: string | null = null;
    @state() private _dialogSaving = false;

    // Confirmation dialog state
    @state() private _confirmDialog: {
        onConfirm: () => void;
        text: string;
        title: string;
    } | null = null;

    _hass?: HomeAssistant;
    private _entityRowCache = new Map<string, HTMLElement>();

    get hass(): HomeAssistant | undefined {
        return this._hass;
    }

    @property({ attribute: false })
    set hass(hass: HomeAssistant) {
        this._hass = hass;
        void this._subscribe();
    }

    static getConfigElement(): HTMLElement {
        return document.createElement('lcm-slot-editor');
    }

    static async getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>> {
        const stub = { config_entry_id: 'stub', slot: 1, type: 'custom:lcm-slot' };
        try {
            return await Promise.race([
                (async () => {
                    const entries = await hass.callWS<GetConfigEntriesResponse>({
                        domain: 'lock_code_manager',
                        type: 'config_entries/get'
                    });
                    if (entries.length > 0) {
                        return {
                            config_entry_id: entries[0].entry_id,
                            slot: 1,
                            type: 'custom:lcm-slot'
                        };
                    }
                    return stub;
                })(),
                new Promise<Record<string, unknown>>((resolve) =>
                    setTimeout(() => resolve(stub), 2000)
                )
            ]);
        } catch {
            return stub;
        }
    }

    setConfig(config: LockCodeManagerSlotCardConfig): void {
        if (!config.config_entry_id && !config.config_entry_title) {
            throw new Error('config_entry_id or config_entry_title is required');
        }
        if (typeof config.slot !== 'number' || config.slot < 1 || config.slot > 9999) {
            throw new Error('slot must be a number between 1 and 9999');
        }
        // If config changed, unsubscribe and resubscribe
        if (
            this._config?.config_entry_id !== config.config_entry_id ||
            this._config?.config_entry_title !== config.config_entry_title ||
            this._config?.slot !== config.slot
        ) {
            this._unsubscribe();
            this._data = undefined;
        }
        this._config = config;

        // Set initial collapsed state from config
        const collapsed = config.collapsed_sections ?? ['conditions', 'lock_status'];
        this._conditionsExpanded = !collapsed.includes('conditions');
        this._lockStatusExpanded = !collapsed.includes('lock_status');
        this._isStub = config.config_entry_id === 'stub';
        if (!this._isStub) {
            void this._subscribe();
        }
    }

    // Mixin abstract method implementations
    protected _getDefaultCodeDisplay(): CodeDisplayMode {
        return DEFAULT_CODE_DISPLAY;
    }

    protected _buildSubscribeMessage(): MessageBase {
        if (!this._config) {
            throw new Error('Config not set');
        }
        const msg: MessageBase & {
            config_entry_id?: string;
            config_entry_title?: string;
            reveal: boolean;
            slot: number;
        } = {
            reveal: this._shouldReveal(),
            slot: this._config.slot,
            type: 'lock_code_manager/subscribe_code_slot'
        };
        if (this._config.config_entry_id) {
            msg.config_entry_id = this._config.config_entry_id;
        } else if (this._config.config_entry_title) {
            msg.config_entry_title = this._config.config_entry_title;
        }
        return msg;
    }

    protected _handleSubscriptionData(data: unknown): void {
        this._data = data as SlotCardData;
    }

    // connectedCallback and disconnectedCallback provided by mixin

    protected updated(changedProperties: Map<string, unknown>): void {
        super.updated(changedProperties);

        // Focus the appropriate input when entering edit mode
        if (this._editingField) {
            const selectors: Record<string, string> = {
                name: '.name .name-edit-input',
                pin: '.hero-pin .pin-edit-input'
            };
            const input = this.shadowRoot?.querySelector<HTMLInputElement>(
                selectors[this._editingField]
            );
            if (input && this.shadowRoot?.activeElement !== input) {
                input.focus();
                input.select();
            }
        }
    }

    protected render(): TemplateResult {
        if (!this._hass || !this._config) {
            return html`<ha-card><div class="message">Loading...</div></ha-card>`;
        }

        // Show static preview for card picker (stub config)
        if (this._isStub) {
            return html`<ha-card>
                <div class="message">Lock Code Manager Slot Card</div>
            </ha-card>`;
        }

        if (this._error) {
            return html`<ha-card>
                <div class="message error">${this._error}</div>
            </ha-card>`;
        }

        // Use websocket data if available
        if (this._data) {
            return this._renderFromData(this._data);
        }

        // Fallback to state-based approach (initial load before subscription)
        return html`<ha-card><div class="message">Connecting...</div></ha-card>`;
    }

    private _renderFromData(data: SlotCardData): TemplateResult {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const { pin, enabled, active, conditions, locks } = data;
        const pinLength = data.pin_length;

        // Transform locks to the expected format
        const lockStatuses = locks.map((lock) => {
            return {
                code: lock.code,
                codeLength: lock.code_length,
                entityId: lock.entity_id,
                inSync: lock.in_sync,
                lastSynced: lock.last_synced,
                lockEntityId: lock.entity_id,
                name: lock.name,
                syncStatus: lock.sync_status
            };
        });

        const showLockStatus = this._config.show_lock_status !== false;

        // Only show conditions section if at least one condition is configured
        const hasConditionHelpers =
            (this._config?.condition_helpers?.length ?? 0) > 0 &&
            this._config!.condition_helpers!.some((eid: string) => this._hass?.states[eid]);
        const hasConditions =
            conditions.condition_entity !== undefined ||
            conditions.calendar !== undefined ||
            hasConditionHelpers;
        const showConditions = this._config.show_conditions !== false && hasConditions;
        // Show "Manage Conditions" row when no conditions exist
        const showManageConditions = this._config.show_conditions !== false && !hasConditions;

        return html`
            <ha-card>
                ${this._actionError
                    ? html`<div class="action-error">
                          <span>${this._actionError}</span>
                          <button
                              class="action-error-dismiss"
                              @click=${this._dismissActionError}
                              aria-label="Dismiss error"
                          >
                              ✕
                          </button>
                      </div>`
                    : nothing}
                ${this._renderHeader()}
                <div class="content">
                    ${this._renderHero(pin, pinLength, enabled, mode)}
                    ${this._renderStatus(enabled, active)}
                    ${showManageConditions ? this._renderManageConditionsRow() : nothing}
                    ${showConditions ? this._renderConditionsSection(conditions) : nothing}
                    ${showLockStatus ? this._renderLockStatusSection(lockStatuses) : nothing}
                </div>
                ${this._showConditionDialog ? this._renderConditionDialog() : nothing}
                ${this._confirmDialog ? this._renderConfirmDialog() : nothing}
            </ha-card>
        `;
    }

    private _renderHeader(): TemplateResult {
        const slotKicker = this._renderSlotKicker();
        const stateChip = this._renderStateChip();
        const name = this._data?.name;
        const editingName = this._editingField === 'name';

        return html`
            <div class="header">
                <div class="header-top">
                    <span class="slot-kicker">${slotKicker}</span>
                    ${stateChip}
                </div>
                <div class="name">
                    ${editingName
                        ? html`<input
                              class="edit-input name-edit-input"
                              type="text"
                              .value=${name ?? ''}
                              @blur=${this._handleEditBlur}
                              @keydown=${this._handleEditKeydown}
                          />`
                        : html`
                              ${name
                                  ? html`${name}`
                                  : html`<em class="placeholder">&lt;No Name&gt;</em>`}
                              <ha-icon-button
                                  class="pencil"
                                  .path=${mdiPencil}
                                  @click=${() => this._startEditing('name')}
                                  .label=${'Edit name'}
                              ></ha-icon-button>
                          `}
                </div>
            </div>
        `;
    }

    private _renderSlotKicker(): string {
        const slot = this._config?.slot;
        const title = this._configEntryTitle();
        return title ? `Slot ${slot} · ${title}` : `Slot ${slot}`;
    }

    private _configEntryTitle(): string | undefined {
        // Prefer the explicit config_entry_title from the card config when set;
        // otherwise fall back to the title surfaced in the websocket payload.
        if (this._config?.config_entry_title) {
            return this._config.config_entry_title;
        }
        return this._data?.config_entry_title || undefined;
    }

    private _renderStateChip(): TemplateResult {
        const enabled = this._data?.enabled;
        const active = this._data?.active;
        let cls: 'active' | 'inactive' | 'disabled';
        let text: string;
        if (enabled === false) {
            cls = 'disabled';
            text = 'Disabled by user';
        } else if (active === true) {
            cls = 'active';
            text = 'Active';
        } else if (active === false) {
            cls = 'inactive';
            text = 'Blocked by conditions';
        } else {
            cls = 'disabled';
            text = 'Unknown';
        }
        return html` <span class="state-chip ${cls}"> <span class="dot"></span>${text} </span> `;
    }

    private _renderHero(
        pin: string | null,
        pinLength: number | undefined,
        enabled: boolean | null,
        mode: CodeDisplayMode
    ): TemplateResult {
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);
        const hasPin = pin !== null || pinLength !== undefined;
        const displayPin = pin
            ? shouldMask
                ? '•'.repeat(pin.length)
                : pin
            : pinLength !== undefined
              ? '•'.repeat(pinLength)
              : null;

        return html`
            <div class="hero">
                <div class="hero-pin">
                    <span class="hero-pin-label">PIN</span>
                    ${this._editingField === 'pin'
                        ? html`<input
                              class="edit-input pin-edit-input"
                              type="text"
                              inputmode="numeric"
                              pattern="[0-9]*"
                              .value=${pin ?? ''}
                              @blur=${this._handleEditBlur}
                              @keydown=${this._handleEditKeydown}
                          />`
                        : html`<span
                              class="hero-pin-value editable ${shouldMask && hasPin
                                  ? 'masked'
                                  : ''}"
                              @click=${() => this._startEditing('pin')}
                          >
                              ${displayPin ?? html`<em class="placeholder">&lt;No PIN&gt;</em>`}
                          </span>`}
                    ${mode === 'masked_with_reveal' && hasPin && this._editingField !== 'pin'
                        ? html`<ha-icon-button
                              class="reveal"
                              .path=${this._revealed ? mdiEyeOff : mdiEye}
                              @click=${this._toggleReveal}
                              .label=${this._revealed ? 'Hide PIN' : 'Reveal PIN'}
                          ></ha-icon-button>`
                        : nothing}
                </div>
                <div class="hero-toggle">
                    <span class="hero-toggle-label">Enabled</span>
                    <ha-switch
                        .checked=${enabled === true}
                        .disabled=${enabled === null}
                        @change=${this._handleEnabledToggle}
                    ></ha-switch>
                </div>
            </div>
        `;
    }

    private _renderStatus(enabled: boolean | null, active: boolean | null): TemplateResult {
        let statusClass: string;
        let statusText: string;
        let statusDetail: string;

        if (enabled === false) {
            statusClass = 'disabled';
            statusText = 'Disabled';
            statusDetail = 'Slot is disabled by user';
        } else if (active === true) {
            statusClass = 'active';
            statusText = 'Active';
            statusDetail = '';
        } else if (active === false) {
            statusClass = 'inactive';
            statusText = 'Inactive';
            statusDetail = 'Blocked by conditions';
        } else {
            statusClass = 'disabled';
            statusText = 'Unknown';
            statusDetail = 'Status unavailable';
        }

        return html`
            <div class="lcm-section">
                <div class="lcm-section-header">Status</div>
                <div class="status-row">
                    <span class="lcm-status-dot ${statusClass}"></span>
                    <span class="status-text">${statusText}</span>
                </div>
                ${statusDetail ? html`<div class="status-detail">${statusDetail}</div>` : nothing}
            </div>
        `;
    }

    private _renderConditionsSection(conditions: SlotCardConditions): TemplateResult {
        const { condition_entity } = conditions;
        const hasConditionEntity = condition_entity !== undefined;
        const entityBlocking = hasConditionEntity && condition_entity.state !== 'on';

        return this._renderCollapsible(
            'Conditions',
            this._conditionsExpanded,
            this._toggleConditions,
            this._renderConditionContent(conditions, hasConditionEntity),
            this._renderConditionHeaderExtra(conditions, hasConditionEntity, entityBlocking)
        );
    }

    private _renderConditionHeaderExtra(
        conditions: SlotCardConditions,
        hasConditionEntity: boolean,
        entityBlocking: boolean
    ): TemplateResult | undefined {
        if (!hasConditionEntity) return undefined;

        const totalConditions = 1;
        const blockingConditions = entityBlocking ? 1 : 0;
        const passingConditions = totalConditions - blockingConditions;
        const allPassing = blockingConditions === 0;

        return html`<span class="collapsible-badge ${allPassing ? 'muted' : 'warning'}"
                >${allPassing ? '✓' : '✗'} ${passingConditions}/${totalConditions}</span
            >
            <span class="condition-blocking-icons">
                <ha-svg-icon
                    class="condition-icon ${entityBlocking ? 'blocking' : ''}"
                    .path=${this._getConditionEntityIcon(
                        conditions.condition_entity!.domain,
                        !entityBlocking
                    )}
                    title="${entityBlocking
                        ? 'Condition blocking access'
                        : 'Condition allowing access'}"
                ></ha-svg-icon>
            </span>`;
    }

    private _renderConditionContent(
        conditions: SlotCardConditions,
        hasConditionEntity: boolean
    ): TemplateResult {
        const { condition_entity } = conditions;

        return html`
            ${hasConditionEntity ? this._renderConditionEntity(condition_entity!, true) : nothing}
            ${!this._isStub && this._config?.condition_helpers?.length
                ? html`<div class="condition-helpers">
                      ${[...new Set(this._config.condition_helpers)]
                          .filter((eid: string) => this._hass?.states[eid])
                          .map(
                              (eid: string) =>
                                  html`${until(
                                      this._getEntityRow(eid),
                                      html`<div>Loading...</div>`
                                  )}`
                          )}
                  </div>`
                : nothing}
            ${!hasConditionEntity
                ? html`<div class="add-condition-links">
                      <span
                          class="add-condition-link"
                          @click=${() => this._openConditionDialog('add-entity')}
                          >+ Add on/off entity</span
                      >
                  </div>`
                : nothing}
        `;
    }

    /**
     * Get the appropriate icon for a condition entity based on its domain.
     * Uses icons consistent with Home Assistant core.
     */
    private _getConditionEntityIcon(domain: string, isActive: boolean): string {
        switch (domain) {
            case 'calendar':
                return isActive ? mdiCalendar : mdiCalendarRemove;
            case 'binary_sensor':
                // HA core uses mdi:eye for generic binary sensors
                return mdiEye;
            case 'switch':
                // HA core uses mdi:toggle-switch / mdi:toggle-switch-outline
                return isActive ? mdiToggleSwitch : mdiToggleSwitchOutline;
            case 'schedule':
                // HA core uses mdi:calendar-clock for schedule entities
                return mdiCalendarClock;
            case 'input_boolean':
                // HA core uses mdi:toggle-switch-outline for input_boolean
                return isActive ? mdiToggleSwitch : mdiToggleSwitchOutline;
            default:
                return mdiEye;
        }
    }

    /**
     * Get domain-specific status text for condition entities.
     */
    private _getConditionStatusText(domain: string, isActive: boolean): string {
        const accessText = isActive ? 'Access allowed' : 'Access blocked';
        switch (domain) {
            case 'calendar':
                return isActive ? `Event active – ${accessText}` : `No event – ${accessText}`;
            case 'schedule':
                return isActive
                    ? `In schedule – ${accessText}`
                    : `Outside schedule – ${accessText}`;
            default:
                return isActive ? `On – ${accessText}` : `Off – ${accessText}`;
        }
    }

    /**
     * Format a schedule date for display (today, tomorrow, or weekday).
     * Returns empty string for today, "tomorrow " or "Mon " etc for other days.
     */
    private _formatScheduleDate(date: Date): string {
        const now = new Date();
        const isToday = date.toDateString() === now.toDateString();
        if (isToday) return '';

        const tomorrow = new Date(now);
        tomorrow.setDate(tomorrow.getDate() + 1);
        const isTomorrow = date.toDateString() === tomorrow.toDateString();
        if (isTomorrow) return 'tomorrow ';

        return `${date.toLocaleDateString([], { weekday: 'short' })} `;
    }

    /**
     * Get a human-readable domain label for display in the UI.
     */
    private _getDomainLabel(domain: string): string {
        const labels: Record<string, string> = {
            binary_sensor: 'Sensor',
            calendar: 'Calendar',
            input_boolean: 'Toggle',
            schedule: 'Schedule',
            switch: 'Switch'
        };
        return labels[domain] ?? domain;
    }

    /**
     * Render a unified condition entity display.
     * Consistent structure across all domain types with domain-specific context.
     */
    private async _getEntityRow(entityId: string): Promise<HTMLElement> {
        const cached = this._entityRowCache.get(entityId);
        if (cached) {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (cached as any).hass = this._hass;
            return cached;
        }

        // Use HA's loadCardHelpers to get createRowElement, which handles
        // lazy-loading and domain-to-row mapping automatically
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const loadHelpers = (window as any).loadCardHelpers;
        if (!loadHelpers) {
            const fallback = document.createElement('div');
            fallback.textContent = entityId;
            return fallback;
        }
        const helpers = await loadHelpers();
        const el = helpers.createRowElement({ entity: entityId }) as HTMLElement;
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (el as any).hass = this._hass;
        this._entityRowCache.set(entityId, el);
        return el;
    }

    private _renderConditionEntity(entity: ConditionEntityInfo, showEdit = false): TemplateResult {
        const isActive = entity.state === 'on';
        const statusIcon = this._getConditionEntityIcon(entity.domain, isActive);
        const statusText = this._getConditionStatusText(entity.domain, isActive);
        const statusClass = isActive ? 'active' : 'inactive';
        const displayName = entity.friendly_name ?? entity.condition_entity_id;
        const domainLabel = this._getDomainLabel(entity.domain);

        return html`
            <div
                class="condition-entity clickable"
                @click=${() => this._openEntityMoreInfo(entity.condition_entity_id)}
                title="Click to view ${displayName}"
            >
                <div class="condition-entity-header">
                    <ha-svg-icon
                        class="condition-entity-icon ${statusClass}"
                        .path=${statusIcon}
                    ></ha-svg-icon>
                    <span class="condition-entity-status">${statusText}</span>
                    <span class="condition-entity-domain">${domainLabel}</span>
                    ${showEdit
                        ? html`<span class="condition-action-icons">
                              <ha-svg-icon
                                  class="condition-edit-icon"
                                  .path=${mdiPencil}
                                  title="Edit condition entity"
                                  @click=${(e: Event) => {
                                      e.stopPropagation();
                                      this._openConditionDialog('edit-entity');
                                  }}
                              ></ha-svg-icon>
                              <ha-svg-icon
                                  class="condition-delete-icon"
                                  .path=${mdiDelete}
                                  title="Remove condition entity"
                                  @click=${(e: Event) => {
                                      e.stopPropagation();
                                      this._deleteConditionEntity();
                                  }}
                              ></ha-svg-icon>
                          </span>`
                        : nothing}
                </div>
                <div class="condition-entity-name">${displayName}</div>
                ${this._renderConditionContext(entity, isActive)}
            </div>
        `;
    }

    private _renderConditionContext(
        entity: ConditionEntityInfo,
        isActive: boolean
    ): TemplateResult | typeof nothing {
        if (entity.domain === 'calendar') {
            if (isActive && entity.calendar) {
                return html`
                    ${entity.calendar.summary
                        ? html`<div class="condition-context">
                              <span class="condition-context-label">Event:</span>${entity.calendar
                                  .summary}
                          </div>`
                        : nothing}
                    ${entity.calendar.start_time
                        ? html`<div class="condition-context">
                              <span class="condition-context-label">Started:</span>
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${entity.calendar.start_time}
                              ></ha-relative-time>
                          </div>`
                        : nothing}
                    ${entity.calendar.end_time
                        ? html`<div class="condition-context">
                              <span class="condition-context-label">Ends:</span>
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${entity.calendar.end_time}
                              ></ha-relative-time>
                          </div>`
                        : nothing}
                    ${entity.calendar_next
                        ? html`<div class="condition-context condition-context-next">
                              <span class="condition-context-label">Next:</span>${entity
                                  .calendar_next.summary ?? 'Event'}
                              starts
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${entity.calendar_next.start_time}
                              ></ha-relative-time>
                          </div>`
                        : nothing}
                `;
            }
            if (!isActive && entity.calendar_next) {
                return html`
                    <div class="condition-context">
                        <span class="condition-context-label">Next:</span>${entity.calendar_next
                            .summary ?? 'Event'}
                    </div>
                    <div class="condition-context">
                        <span class="condition-context-label">Starts:</span>
                        <ha-relative-time
                            .hass=${this._hass}
                            .datetime=${entity.calendar_next.start_time}
                        ></ha-relative-time>
                    </div>
                `;
            }
        }

        if (entity.domain === 'schedule' && entity.schedule?.next_event) {
            const nextEvent = new Date(entity.schedule.next_event);
            const timeStr = nextEvent.toLocaleTimeString([], {
                hour: 'numeric',
                minute: '2-digit'
            });
            const dateStr = this._formatScheduleDate(nextEvent);
            const label = isActive ? 'Ends:' : 'Starts:';
            return html`
                <div class="condition-context">
                    <span class="condition-context-label">${label}</span>
                    ${dateStr}${timeStr}
                </div>
            `;
        }

        return nothing;
    }

    /**
     * Open the more-info dialog for an entity.
     */
    private _openEntityMoreInfo(entityId: string): void {
        const event = new CustomEvent('hass-more-info', {
            bubbles: true,
            composed: true,
            detail: { entityId }
        });
        this.dispatchEvent(event);
    }

    private _renderLockStatusSection(lockStatuses: LockSyncStatus[]): TemplateResult {
        const syncedCount = lockStatuses.filter((l) => l.inSync === true).length;
        const totalCount = lockStatuses.length;

        const headerExtra =
            totalCount > 0
                ? html`<span class="collapsible-badge">${syncedCount}/${totalCount}</span>`
                : undefined;

        const content =
            lockStatuses.length > 0
                ? html`${lockStatuses.map((lock) => this._renderLockRow(lock))}`
                : html`<div class="no-conditions">No locks found</div>`;

        return this._renderCollapsible(
            'Lock Status',
            this._lockStatusExpanded,
            this._toggleLockStatus,
            content,
            headerExtra
        );
    }

    private _renderLockRow(lock: LockSyncStatus): TemplateResult {
        const showSync = this._config?.show_lock_sync !== false;
        const showCodeSensors = this._config?.show_code_sensors !== false;
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const showRevealButton = showCodeSensors && mode === 'masked_with_reveal';

        let iconClass: string;
        let statusText: string;
        let icon: string;

        switch (lock.syncStatus) {
            case 'in_sync':
                iconClass = 'synced';
                statusText = 'Synced';
                icon = 'mdi:check-circle';
                break;
            case 'out_of_sync':
                iconClass = 'pending';
                statusText = 'Out of sync';
                icon = 'mdi:clock-outline';
                break;
            case 'syncing':
                iconClass = 'syncing';
                statusText = 'Syncing';
                icon = 'mdi:sync';
                break;
            case 'suspended':
                iconClass = 'suspended';
                statusText = 'Suspended';
                icon = 'mdi:alert-circle';
                break;
            default:
                // Fallback for when sync_status is not available
                if (lock.inSync === true) {
                    iconClass = 'synced';
                    statusText = 'Synced';
                    icon = 'mdi:check-circle';
                } else if (lock.inSync === false) {
                    iconClass = 'pending';
                    statusText = 'Out of sync';
                    icon = 'mdi:clock-outline';
                } else {
                    iconClass = 'unknown';
                    statusText = 'Unknown';
                    icon = 'mdi:help-circle';
                }
        }

        // Format code display
        const codeDisplay = this._formatLockCode(lock);

        return html`
            <div class="lock-row">
                ${showSync
                    ? html`<ha-icon class="lcm-sync-icon ${iconClass}" icon="${icon}"></ha-icon>`
                    : nothing}
                <div class="lock-info">
                    <span
                        class="lock-name"
                        title="View lock codes"
                        @click=${() => this._navigateToLock(lock.lockEntityId)}
                    >
                        ${lock.name}
                    </span>
                    ${showSync && lock.syncStatus !== 'suspended' && lock.lastSynced
                        ? html`<span class="lock-synced-time">
                              Last synced to lock
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${lock.lastSynced}
                              ></ha-relative-time>
                          </span>`
                        : showSync
                          ? html`<span class="lock-synced-time ${iconClass}">${statusText}</span>`
                          : nothing}
                </div>
                ${showCodeSensors && codeDisplay
                    ? html`<div class="lock-code-field">
                          <span class="lock-code-value">${codeDisplay}</span>
                          ${showRevealButton
                              ? html`<ha-icon-button
                                    class="lcm-reveal-button"
                                    .path=${this._revealed ? mdiEyeOff : mdiEye}
                                    @click=${this._toggleReveal}
                                    .label=${this._revealed ? 'Hide codes' : 'Reveal codes'}
                                ></ha-icon-button>`
                              : nothing}
                      </div>`
                    : nothing}
            </div>
        `;
    }

    private _formatLockCode(lock: LockSyncStatus): string | null {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);

        if (isSlotEmpty(lock.code)) return null;
        if (lock.code === SLOT_CODE_UNREADABLE) return '• • •';
        return shouldMask ? '•'.repeat(String(lock.code).length) : String(lock.code);
    }

    // _toggleReveal inherited from mixin

    // Render helper for collapsible sections
    private _renderCollapsible(
        title: string,
        expanded: boolean,
        onToggle: () => void,
        content: TemplateResult,
        headerExtra?: TemplateResult
    ): TemplateResult {
        return html`
            <div class="collapsible-section">
                <div class="collapsible-header" @click=${onToggle}>
                    <div class="collapsible-title">${title} ${headerExtra ?? nothing}</div>
                    <ha-svg-icon
                        class="collapsible-chevron"
                        .path=${expanded ? mdiChevronUp : mdiChevronDown}
                    ></ha-svg-icon>
                </div>
                <div class="collapsible-content ${expanded ? 'expanded' : ''}">${content}</div>
            </div>
        `;
    }

    private _toggleConditions(): void {
        this._conditionsExpanded = !this._conditionsExpanded;
    }

    private _toggleLockStatus(): void {
        this._lockStatusExpanded = !this._lockStatusExpanded;
    }

    private _renderManageConditionsRow(): TemplateResult {
        return html`
            <div class="empty-conditions-header">
                <span class="empty-conditions-title">Conditions</span>
                <span class="empty-conditions-badge">0/0</span>
                <span class="empty-conditions-spacer"></span>
                <div class="empty-conditions-actions">
                    <button
                        class="empty-conditions-btn"
                        @click=${() => this._openConditionDialog('add-entity')}
                        title="Add on/off condition entity"
                    >
                        <ha-svg-icon .path=${mdiPlus}></ha-svg-icon>
                        On/Off Entity
                    </button>
                </div>
            </div>
        `;
    }

    private _openConditionDialog(mode: 'add-entity' | 'edit-entity'): void {
        this._dialogMode = mode;
        const conditions = this._data?.conditions;

        if (mode === 'edit-entity') {
            // Initialize with current entity
            this._dialogEntityId = conditions?.condition_entity?.condition_entity_id ?? null;
        } else if (mode === 'add-entity') {
            // Start fresh for adding
            this._dialogEntityId = null;
        }

        this._showConditionDialog = true;
    }

    private _closeConditionDialog(): void {
        this._showConditionDialog = false;
        this._dialogSaving = false;
    }

    private _deleteConditionEntity(): void {
        this._confirmDialog = {
            onConfirm: async () => {
                try {
                    await this._clearSlotCondition();
                    // Force re-subscribe to get updated data
                    this._unsubscribe();
                    void this._subscribe();
                } catch (err) {
                    this._setActionError(
                        `Failed to remove condition: ${err instanceof Error ? err.message : 'Unknown error'}`
                    );
                }
            },
            text: 'This will remove the condition entity from controlling when this PIN is active.',
            title: 'Remove condition entity?'
        };
    }

    private async _setSlotCondition(entity_id: string): Promise<void> {
        if (!this._hass || !this._config) return;
        const msg: MessageBase & Record<string, unknown> = {
            entity_id,
            slot: this._config.slot,
            type: 'lock_code_manager/set_slot_condition'
        };
        if (this._config.config_entry_id) {
            msg.config_entry_id = this._config.config_entry_id;
        } else if (this._config.config_entry_title) {
            msg.config_entry_title = this._config.config_entry_title;
        }
        await this._hass.callWS(msg);
    }

    private async _clearSlotCondition(): Promise<void> {
        if (!this._hass || !this._config) return;
        const msg: MessageBase & Record<string, unknown> = {
            slot: this._config.slot,
            type: 'lock_code_manager/clear_slot_condition'
        };
        if (this._config.config_entry_id) {
            msg.config_entry_id = this._config.config_entry_id;
        } else if (this._config.config_entry_title) {
            msg.config_entry_title = this._config.config_entry_title;
        }
        await this._hass.callWS(msg);
    }

    private _renderConditionDialog(): TemplateResult {
        const dialogTitle =
            this._dialogMode === 'add-entity' ? 'Add Condition Entity' : 'Edit Condition Entity';

        return html`
            <ha-dialog open @closed=${this._closeConditionDialog} .heading=${dialogTitle}>
                <div class="dialog-content">
                    <div class="dialog-section">
                        <div class="dialog-section-description">
                            PIN is active only when this entity is "on"
                        </div>
                        <input
                            type="text"
                            class="entity-select"
                            list="condition-entity-list"
                            placeholder="Search or select entity..."
                            .value=${this._dialogEntityId ?? ''}
                            @focus=${(e: Event) => {
                                // Select all on focus for easier replacement
                                (e.target as HTMLInputElement).select();
                            }}
                            @input=${(e: Event) => {
                                const val = (e.target as HTMLInputElement).value;
                                // Only set if it's a valid entity ID
                                if (this._hass?.states[val]) {
                                    this._dialogEntityId = val;
                                } else if (val === '') {
                                    this._dialogEntityId = null;
                                }
                            }}
                            @change=${(e: Event) => {
                                const val = (e.target as HTMLInputElement).value;
                                if (this._hass?.states[val]) {
                                    this._dialogEntityId = val;
                                } else if (val === '') {
                                    this._dialogEntityId = null;
                                }
                            }}
                        />
                        <datalist id="condition-entity-list">
                            ${this._hass
                                ? Object.keys(this._hass.states)
                                      .filter((eid) =>
                                          [
                                              'calendar',
                                              'schedule',
                                              'binary_sensor',
                                              'switch',
                                              'input_boolean'
                                          ].includes(eid.split('.')[0])
                                      )
                                      .sort()
                                      .map(
                                          (eid) => html`
                                              <option
                                                  value=${eid}
                                                  label="${this._hass.states[eid]?.attributes
                                                      ?.friendly_name ?? eid}"
                                              ></option>
                                          `
                                      )
                                : nothing}
                        </datalist>
                    </div>
                </div>
                <ha-button slot="secondaryAction" @click=${() => this._closeConditionDialog()}>
                    Cancel
                </ha-button>
                <ha-button
                    slot="primaryAction"
                    @click=${() => this._saveConditionChanges()}
                    .disabled=${this._dialogSaving}
                >
                    ${this._dialogSaving ? 'Saving...' : 'Save'}
                </ha-button>
            </ha-dialog>
        `;
    }

    private _renderConfirmDialog(): TemplateResult {
        if (!this._confirmDialog) return html``;

        return html`
            <ha-dialog open @closed=${() => (this._confirmDialog = null)}>
                <div slot="heading">${this._confirmDialog.title}</div>
                <div class="confirm-dialog-content">${this._confirmDialog.text}</div>
                <ha-button slot="secondaryAction" @click=${() => (this._confirmDialog = null)}>
                    Cancel
                </ha-button>
                <ha-button
                    slot="primaryAction"
                    class="destructive"
                    @click=${() => {
                        this._confirmDialog?.onConfirm();
                        this._confirmDialog = null;
                    }}
                >
                    Remove
                </ha-button>
            </ha-dialog>
        `;
    }

    private async _saveConditionChanges(): Promise<void> {
        if (!this._hass || !this._config) {
            this._setActionError('Card not initialized');
            return;
        }

        this._dialogSaving = true;

        try {
            if (this._dialogMode === 'add-entity' || this._dialogMode === 'edit-entity') {
                const entityId =
                    typeof this._dialogEntityId === 'string' ? this._dialogEntityId.trim() : '';
                if (!entityId) {
                    this._setActionError('Please select an entity before saving');
                    this._dialogSaving = false;
                    return;
                }
                if (!(entityId in this._hass.states)) {
                    this._setActionError(`Selected entity not found: ${entityId}`);
                    this._dialogSaving = false;
                    return;
                }
                await this._setSlotCondition(entityId);
            } else {
                throw new Error(`Unknown dialog mode: ${this._dialogMode}`);
            }

            this._closeConditionDialog();
            // Force re-subscribe to get updated data since config changes
            // don't trigger entity state changes
            this._unsubscribe();
            void this._subscribe();
        } catch (err) {
            this._setActionError(
                `Failed to save: ${err instanceof Error ? err.message : String(err)}`
            );
            this._dialogSaving = false;
        }
    }

    private async _handleEnabledToggle(e: Event): Promise<void> {
        const target = e.target as HTMLInputElement;
        const newState = target.checked;

        if (!this._hass) return;
        const enabledEntityId = this._data?.entities?.enabled ?? undefined;
        if (!enabledEntityId) return;

        const service = newState ? 'turn_on' : 'turn_off';
        try {
            await this._hass.callService('switch', service, {
                entity_id: enabledEntityId
            });
        } catch (err) {
            this._setActionError(
                `Failed to ${newState ? 'enable' : 'disable'} slot: ${err instanceof Error ? err.message : 'Unknown error'}`
            );
        }
    }

    // Consolidated edit handlers for name and pin fields
    private _startEditing(field: 'name' | 'pin'): void {
        // Special handling for PIN: reveal first to show current value
        if (field === 'pin' && !this._revealed) {
            this._revealed = true;
            this._unsubscribe();
            void this._subscribe().then(() => {
                this._editingField = 'pin';
            });
        } else {
            this._editingField = field;
        }
    }

    private _handleEditBlur(e: Event): void {
        const target = e.target as HTMLInputElement;
        this._saveEditValue(target.value);
        this._editingField = null;
    }

    private _handleEditKeydown(e: KeyboardEvent): void {
        if (e.key === 'Enter') {
            const target = e.target as HTMLInputElement;
            this._saveEditValue(target.value);
            this._editingField = null;
        } else if (e.key === 'Escape') {
            this._editingField = null;
        }
    }

    private async _saveEditValue(rawValue: string): Promise<void> {
        if (!this._hass || !this._editingField) return;

        const config = EDIT_FIELD_CONFIG[this._editingField];
        const entityId = this._data?.entities?.[config.entityKey];
        const fieldLabel = this._editingField;

        if (!entityId) {
            this._setActionError(`Cannot update ${fieldLabel}: entity is unavailable`);
            return;
        }

        const entityState = this._hass.states[entityId];
        if (!entityState || entityState.state === 'unavailable') {
            this._setActionError(`Cannot update ${fieldLabel}: entity is unavailable or disabled`);
            return;
        }

        const serviceData = config.serviceData(rawValue);
        if (Object.keys(serviceData).length === 0) return;

        const [domain, service] = config.service.split('.');
        try {
            await this._hass.callService(domain, service, {
                entity_id: entityId,
                ...serviceData
            });
        } catch (err) {
            this._setActionError(
                `Failed to update ${fieldLabel}: ${err instanceof Error ? err.message : 'Unknown error'}`
            );
        }
    }

    private _setActionError(message: string): void {
        this._actionError = message;
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            this._actionError = undefined;
        }, 5000);
    }

    private _dismissActionError(): void {
        this._actionError = undefined;
    }

    private _navigateToLock(lockEntityId: string): void {
        // Open the more-info dialog for this lock entity
        const event = new CustomEvent('hass-more-info', {
            bubbles: true,
            composed: true,
            detail: { entityId: lockEntityId }
        });
        this.dispatchEvent(event);
    }

    // _unsubscribe, _shouldReveal, _subscribe inherited from mixin
}

customElements.define('lcm-slot', LockCodeManagerSlotCard);

declare global {
    interface Window {
        customCards?: Array<{
            description: string;
            name: string;
            preview?: boolean;
            type: string;
        }>;
    }
}

window.customCards = window.customCards || [];
window.customCards.push({
    description: 'Displays and controls a Lock Code Manager code slot',
    name: 'LCM Slot Card',
    preview: true,
    type: 'lcm-slot'
});
