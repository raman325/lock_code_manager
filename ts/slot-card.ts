import {
    mdiChevronDown,
    mdiChevronRight,
    mdiChevronUp,
    mdiClockOutline,
    mdiDelete,
    mdiEye,
    mdiEyeOff,
    mdiKey,
    mdiPencil,
    mdiPlus
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

/** Domains the Manage Condition entity picker is restricted to. */
const CONDITION_DOMAINS = [
    'calendar',
    'schedule',
    'binary_sensor',
    'switch',
    'input_boolean'
] as const;

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
    @state() private _dialogEntityId: string | null = null;
    @state() private _dialogSaving = false;

    _hass?: HomeAssistant;
    private _entityRowCache = new Map<string, HTMLElement>();
    private _actionErrorTimer?: ReturnType<typeof setTimeout>;
    private _revealedForEdit = false;

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
        } catch (err) {
            // Log but don't surface to the user — this runs in the card
            // picker, where there's no dashboard banner to display errors.
            // eslint-disable-next-line no-console
            console.warn('lcm-slot: failed to fetch config entries for stub config', err);
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

        // Set initial collapsed state from config. Accept both 'condition'
        // (the new canonical singular) and 'conditions' (the original plural)
        // for backward compatibility with YAML written before the rename.
        const collapsed = config.collapsed_sections ?? ['condition', 'lock_status'];
        this._conditionsExpanded = !(
            collapsed.includes('condition') || collapsed.includes('conditions')
        );
        this._lockStatusExpanded = !collapsed.includes('lock_status');
        this._isStub = config.config_entry_id === 'stub';
        if (!this._isStub) {
            void this._subscribe();
        }
    }

    // The mixin provides connectedCallback (which kicks off the slot
    // subscription); we extend it here to also force-register HA's
    // ha-entity-picker before the user can open the condition dialog.
    override connectedCallback(): void {
        super.connectedCallback();
        void this._ensureEntityPickerLoaded();
    }

    // We override disconnectedCallback here to clean up the action-error
    // timer in addition to letting the mixin tear down its subscription.
    override disconnectedCallback(): void {
        super.disconnectedCallback();
        if (this._actionErrorTimer !== undefined) {
            clearTimeout(this._actionErrorTimer);
            this._actionErrorTimer = undefined;
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

    protected updated(changedProperties: Map<string, unknown>): void {
        super.updated(changedProperties);

        // Focus the appropriate input when entering edit mode
        if (this._editingField) {
            const selectors: Record<string, string> = {
                name: '.name-edit-input',
                pin: '.pin-edit-input'
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

        // State class for the card background — matches the lock card's
        // slot-chip state tinting (active = primary blue, inactive =
        // warning orange, disabled = muted) so a slot looks the same
        // regardless of which card you're viewing it on.
        let stateClass: 'active' | 'inactive' | 'disabled';
        if (enabled === false) {
            stateClass = 'disabled';
        } else if (active === false) {
            stateClass = 'inactive';
        } else {
            stateClass = 'active';
        }

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
        // The conditions section now always renders when enabled. The empty
        // state (no condition entity, no helpers) is handled inside the
        // collapsible body so users can add a condition from there.
        const showConditions = this._config.show_conditions !== false;

        return html`
            <ha-card class="slot-card-state-${stateClass}">
                ${this._actionError
                    ? html`<div class="action-error" role="alert">
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
                    ${showConditions ? this._renderConditionsSection(conditions) : nothing}
                    ${showLockStatus ? this._renderLockStatusSection(lockStatuses) : nothing}
                </div>
                ${this._renderEventRow()}
                ${this._showConditionDialog ? this._renderConditionDialog() : nothing}
            </ha-card>
        `;
    }

    /**
     * Render the click-through event row at the bottom of the card. Shows the
     * lock and a relative timestamp for the slot's most recent PIN use, or a
     * "Never used" empty state when the slot has been configured but the PIN
     * has not been used yet. Returns `nothing` when there is no event entity
     * (e.g. the slot has no PIN at all), when the event entity is missing
     * from `hass.states` entirely (registry race or removed entity), or when
     * it is in the `unavailable` state — clicking through to a more-info
     * dialog with no useful content would be misleading.
     *
     * Clicking the row opens HA's more-info dialog on the event entity, which
     * provides the full firing history. The row is keyboard-focusable
     * (`role="button"`, `tabindex="0"`) and responds to Enter/Space so screen
     * readers and keyboard users can reach the activity history too.
     */
    private _renderEventRow(): TemplateResult | typeof nothing {
        const lastUsed = this._data?.last_used;
        const lastUsedLock = this._data?.last_used_lock;
        const eventEntityId = this._data?.event_entity_id;

        if (!eventEntityId) return nothing;
        const eventState = this._hass?.states[eventEntityId];
        if (!eventState || eventState.state === 'unavailable') return nothing;

        const meta = lastUsed
            ? html`${lastUsedLock ?? 'Used'} ·
                  <ha-relative-time .hass=${this._hass} .datetime=${lastUsed}></ha-relative-time>`
            : html`Never used`;

        return html`
            <div
                class="event-row"
                role="button"
                tabindex="0"
                aria-label="View activity history"
                @click=${() => this._navigateToEventHistory()}
                @keydown=${(e: KeyboardEvent) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        this._navigateToEventHistory();
                    }
                }}
            >
                <ha-svg-icon class="event-icon" .path=${mdiClockOutline}></ha-svg-icon>
                <span class="event-name">Last used</span>
                <span class="event-meta">${meta}</span>
                <ha-svg-icon class="event-arrow" .path=${mdiChevronRight}></ha-svg-icon>
            </div>
        `;
    }

    /**
     * Click handler for the event row — opens the HA more-info dialog on the
     * slot's PIN-used event entity, which surfaces the full event firing
     * history. No-op when there is no event entity, when the entity is
     * missing from `hass.states`, or when it is `unavailable` (the row
     * itself is also suppressed in those cases, so this is defense in
     * depth).
     */
    private _navigateToEventHistory(): void {
        const eventEntityId = this._data?.event_entity_id;
        if (!eventEntityId) return;
        const eventState = this._hass?.states[eventEntityId];
        if (!eventState || eventState.state === 'unavailable') return;
        const event = new CustomEvent('hass-more-info', {
            bubbles: true,
            composed: true,
            detail: { entityId: eventEntityId }
        });
        this.dispatchEvent(event);
    }

    private _renderHeader(): TemplateResult {
        const slotKicker = this._renderSlotKicker();
        const stateChip = this._renderStateChip();

        return html`
            <div class="header">
                <div class="header-top">
                    <div class="header-icon">
                        <ha-svg-icon .path=${mdiKey}></ha-svg-icon>
                    </div>
                    <span class="header-title">${slotKicker}</span>
                    ${stateChip}
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
            text = 'Blocked by condition';
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

        const name = this._data?.name;
        const editingName = this._editingField === 'name';

        return html`
            <div class="hero">
                <div class="hero-row">
                    <div class="hero-field">
                        ${editingName
                            ? html`<input
                                  class="edit-input name-edit-input"
                                  type="text"
                                  .value=${name ?? ''}
                                  @blur=${this._handleEditBlur}
                                  @keydown=${this._handleEditKeydown}
                              />`
                            : html`<span
                                      class="hero-name-value editable"
                                      role="button"
                                      tabindex="0"
                                      aria-label="Edit name"
                                      @click=${() => this._startEditing('name')}
                                      @keydown=${(e: KeyboardEvent) => {
                                          if (e.key === 'Enter' || e.key === ' ') {
                                              e.preventDefault();
                                              this._startEditing('name');
                                          }
                                      }}
                                  >
                                      ${name
                                          ? html`${name}`
                                          : html`<em class="placeholder">Not named</em>`}
                                  </span>
                                  <ha-icon-button
                                      class="hero-name-pencil"
                                      .path=${mdiPencil}
                                      @click=${() => this._startEditing('name')}
                                      .label=${'Edit name'}
                                  ></ha-icon-button>`}
                    </div>
                </div>
                <div class="hero-row">
                    <div class="hero-field hero-pin">
                        <span class="hero-field-label">PIN</span>
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
                                  role="button"
                                  tabindex="0"
                                  aria-label="Edit PIN"
                                  @click=${() => this._startEditing('pin')}
                                  @keydown=${(e: KeyboardEvent) => {
                                      if (e.key === 'Enter' || e.key === ' ') {
                                          e.preventDefault();
                                          this._startEditing('pin');
                                      }
                                  }}
                              >
                                  ${displayPin ?? html`<em class="placeholder">No PIN set</em>`}
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
                        <ha-switch
                            .checked=${enabled === true}
                            .disabled=${enabled === null}
                            @change=${this._handleEnabledToggle}
                        ></ha-switch>
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Render the Conditions collapsible. The body is the new HA-entity-row +
     * LCM overlay layout (with a Manage / Add link directly below the block,
     * above any helpers). The collapsible summary names the entity inline
     * rather than counting passing/blocking conditions, since the section now
     * holds at most one condition entity.
     */
    private _renderConditionsSection(conditions: SlotCardConditions): TemplateResult {
        const hasEntity = conditions.condition_entity !== undefined;
        const hasHelpers =
            !this._isStub &&
            (this._config?.condition_helpers?.length ?? 0) > 0 &&
            this._config!.condition_helpers!.some((eid: string) => this._hass?.states[eid]);

        // No condition + no helpers: nothing to expand to. Render a static
        // header row with the Add button on the right; skip the chevron
        // entirely so the row doesn't read as collapsible.
        if (!hasEntity && !hasHelpers) {
            return html`
                <div class="collapsible-section">
                    <div class="collapsible-header static">
                        <div class="collapsible-title">Condition</div>
                        ${this._renderAddConditionButton()}
                    </div>
                </div>
            `;
        }

        // No condition + helpers exist: collapsible (helpers are content),
        // but the Add affordance is on the collapsed row instead of inside
        // the body. With a condition set, the summary badge takes the slot.
        const headerExtra = hasEntity
            ? this._renderConditionsSummary(conditions)
            : this._renderAddConditionButton();

        return this._renderCollapsible(
            'Condition',
            this._conditionsExpanded,
            this._toggleConditions,
            this._renderConditionsBody(conditions, hasEntity, hasHelpers),
            headerExtra
        );
    }

    private _renderAddConditionButton(): TemplateResult {
        return html`
            <button
                class="add-condition-btn"
                @click=${(e: Event) => {
                    e.stopPropagation();
                    this._openConditionDialog();
                }}
                aria-label="Add condition"
            >
                <ha-svg-icon .path=${mdiPlus}></ha-svg-icon>
                Add condition
            </button>
        `;
    }

    private _renderConditionsSummary(conditions: SlotCardConditions): TemplateResult {
        const entity = conditions.condition_entity;
        if (!entity) {
            return html`<span class="collapsible-badge muted">none</span>`;
        }
        const isAllowing = entity.state === 'on';
        const name = entity.friendly_name ?? entity.condition_entity_id;
        return html`<span class="collapsible-badge ${isAllowing ? '' : 'warning'}">
            ${isAllowing ? '✓' : '✗'} ${name}
        </span>`;
    }

    private _renderConditionsBody(
        conditions: SlotCardConditions,
        hasEntity: boolean,
        hasHelpers: boolean
    ): TemplateResult {
        return html`
            ${hasEntity
                ? html`${this._renderConditionBlock(conditions.condition_entity!)}
                      <span
                          class="remove-link"
                          role="button"
                          tabindex="0"
                          aria-label="Remove condition"
                          @click=${() => this._removeCondition()}
                          @keydown=${(e: KeyboardEvent) => {
                              if (e.key === 'Enter' || e.key === ' ') {
                                  e.preventDefault();
                                  this._removeCondition();
                              }
                          }}
                      >
                          <ha-svg-icon .path=${mdiDelete}></ha-svg-icon>
                          Remove condition
                      </span>`
                : nothing}
            ${hasHelpers ? this._renderHelpers() : nothing}
        `;
    }

    /**
     * Render the condition entity as an HA entity row (so calendars show the
     * event, schedules show the state, etc.) plus an LCM overlay strip below
     * it that explains what the entity's state means for *this slot* (allowing
     * or blocking access, with a short context line).
     */
    private _renderConditionBlock(entity: ConditionEntityInfo): TemplateResult {
        const isAllowing = entity.state === 'on';
        const overlayClass = isAllowing ? 'allowing' : 'blocking';
        const statusText = isAllowing ? '✓ Allowing access' : '✗ Blocking access';
        const context = this._renderOverlayContext(entity, isAllowing);

        return html`
            <div class="condition-block">
                ${until(
                    this._getEntityRow(entity.condition_entity_id),
                    html`<div class="entity-row-loading">Loading…</div>`
                )}
                <div class="lcm-overlay ${overlayClass}">
                    <span class="lcm-overlay-status">${statusText}</span>
                    <span class="lcm-overlay-context">${context}</span>
                </div>
            </div>
        `;
    }

    /**
     * Render a short, single-line context string for the overlay strip. The
     * line is rendered inside an `overflow: hidden; text-overflow: ellipsis`
     * span, so it must be plain text — keep it concise.
     */
    private _renderOverlayContext(entity: ConditionEntityInfo, isAllowing: boolean): string {
        if (entity.domain === 'calendar' && isAllowing && entity.calendar?.summary) {
            const ends = entity.calendar.end_time
                ? ` · ends ${this._formatRelative(entity.calendar.end_time)}`
                : '';
            return `${entity.calendar.summary}${ends}`;
        }
        if (entity.domain === 'calendar' && !isAllowing && entity.calendar_next) {
            const start = entity.calendar_next.start_time
                ? ` starts ${this._formatRelative(entity.calendar_next.start_time)}`
                : '';
            return `Next: ${entity.calendar_next.summary ?? 'Event'}${start}`;
        }
        if (entity.domain === 'schedule' && entity.schedule?.next_event) {
            const label = isAllowing ? 'Ends' : 'Starts';
            return `${label} ${this._formatRelative(entity.schedule.next_event)}`;
        }
        return isAllowing ? 'Condition is on' : 'Condition is off';
    }

    /**
     * Render the helpers sub-list under the Conditions body. Helpers come from
     * the card config (not the slot data) and render as plain HA entity rows.
     *
     * The active condition entity is excluded from the helpers list so the
     * shared cached HTMLElement returned by `_getEntityRow` can't be moved
     * between two mount points — the DOM permits each node in only one
     * place, and Lit's `until()` would silently lose one of them.
     */
    private _renderHelpers(): TemplateResult {
        const conditionEntityId = this._data?.conditions?.condition_entity?.condition_entity_id;
        const helpers = [...new Set(this._config!.condition_helpers)].filter(
            (eid: string) => eid !== conditionEntityId && this._hass?.states[eid]
        );
        return html`
            <div class="helpers-label">Helpers</div>
            <div class="helpers-list">
                ${helpers.map(
                    (eid) =>
                        html`${until(
                            this._getEntityRow(eid),
                            html`<div class="entity-row-loading">Loading…</div>`
                        )}`
                )}
            </div>
        `;
    }

    /**
     * Format an ISO timestamp as a short relative phrase suitable for the
     * single-line overlay context (e.g. "in 5 days", "3 days ago", "today").
     * Anything strictly under one day in either direction collapses to
     * "today" — the overlay isn't the place for hour-level precision; the
     * entity row above shows full state. Past the one-day boundary we round
     * to the nearest whole day count.
     */
    private _formatRelative(iso: string): string {
        const ms = new Date(iso).getTime() - Date.now();
        if (Math.abs(ms) < 86400000) return 'today';
        const days = Math.round(Math.abs(ms) / 86400000);
        if (days === 1) return ms > 0 ? 'in 1 day' : '1 day ago';
        return ms > 0 ? `in ${days} days` : `${days} days ago`;
    }

    /**
     * Force HA's `ha-entity-picker` element to register. The picker is
     * lazy-loaded by Home Assistant — the Lovelace editor context loads it
     * eagerly, but a standalone custom card never triggers the load, so
     * the unregistered tag inside our condition dialog stays empty. We
     * piggyback on the entities-card config element, which uses
     * ha-entity-picker internally, so requesting its `getConfigElement()`
     * forces the picker to register as a side effect.
     *
     * Idempotent: short-circuits once the picker is in the
     * customElements registry. Failures are swallowed (logged via
     * console.warn) because the dialog itself still opens — the picker
     * just won't render until HA registers it some other way.
     */
    private async _ensureEntityPickerLoaded(): Promise<void> {
        if (customElements.get('ha-entity-picker')) return;
        const loadHelpers = (window as Window & { loadCardHelpers?: () => Promise<unknown> })
            .loadCardHelpers;
        if (!loadHelpers) return;
        try {
            const helpers = (await loadHelpers()) as {
                createCardElement: (config: { entities: string[]; type: string }) => HTMLElement & {
                    constructor: { getConfigElement?: () => Promise<unknown> };
                };
            };
            const cardElement = helpers.createCardElement({ entities: [], type: 'entities' });
            await cardElement.constructor.getConfigElement?.();
        } catch (err) {
            // eslint-disable-next-line no-console
            console.warn('lcm-slot: failed to lazy-load ha-entity-picker', err);
        }
    }

    /**
     * Lazy-loads (and caches) an HA entity row element using the
     * `loadCardHelpers().createRowElement()` helper. Falls back to a plain
     * text node when the helper isn't available (e.g. older HA, jsdom test
     * environment without the global stub). On any failure (loadHelpers
     * throws, createRowElement throws, etc.) returns a visible error
     * placeholder and surfaces a friendly message via _setActionError so
     * the user isn't stuck with an infinite "Loading…" spinner. The error
     * placeholder is intentionally NOT cached so the next render retries.
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
        try {
            const helpers = await loadHelpers();
            const el = helpers.createRowElement({ entity: entityId }) as HTMLElement;
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (el as any).hass = this._hass;
            this._entityRowCache.set(entityId, el);
            return el;
        } catch (err) {
            this._setActionError(
                `Failed to load entity row for ${entityId}: ${err instanceof Error ? err.message : String(err)}`
            );
            const errorEl = document.createElement('div');
            errorEl.className = 'entity-row-error';
            errorEl.textContent = `Failed to load row for ${entityId}`;
            return errorEl;
        }
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
                        role="button"
                        tabindex="0"
                        aria-label="View ${lock.name} more info"
                        @click=${() => this._navigateToLock(lock.lockEntityId)}
                        @keydown=${(e: KeyboardEvent) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                                e.preventDefault();
                                this._navigateToLock(lock.lockEntityId);
                            }
                        }}
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
                <div
                    class="collapsible-header"
                    role="button"
                    tabindex="0"
                    aria-expanded=${expanded ? 'true' : 'false'}
                    @click=${onToggle}
                    @keydown=${(e: KeyboardEvent) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                            e.preventDefault();
                            onToggle();
                        }
                    }}
                >
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

    /**
     * Open the condition dialog. Always opens in add mode with an empty
     * picker — to swap conditions, the user removes the existing one via
     * the inline Remove link and then re-adds. Keeping the dialog Add-only
     * removes the destructive Remove button from the dialog body and means
     * a successful pick can commit immediately on selection.
     */
    private _openConditionDialog(): void {
        // Defensive: connectedCallback already kicks the picker load, but
        // re-trigger here in case the card connected in a state where the
        // global helper wasn't yet available. Idempotent — short-circuits
        // once the element is registered.
        void this._ensureEntityPickerLoaded();
        this._dialogEntityId = null;
        this._showConditionDialog = true;
    }

    /**
     * Close the condition dialog and reset its transient picker state. Does
     * NOT reset `_dialogSaving` — that flag tracks whether a commit/remove WS
     * call is still in flight, and closing the dialog (e.g. via the Esc
     * key) must not drop that signal or the re-entry guards in
     * `_removeCondition` and `_commitConditionPick` would let a second
     * request through. Success-path callers reset `_dialogSaving` after the
     * resubscribe resolves; error-path callers reset it inside their catch
     * blocks.
     */
    private _closeConditionDialog(): void {
        this._showConditionDialog = false;
        this._dialogEntityId = null;
    }

    /**
     * Clear the condition entity from the slot. Triggered by the inline
     * Remove condition link below the condition block (the dialog itself is
     * Add-only). The link's warning color is the visual safety — there is
     * no extra confirmation step. The early `_dialogSaving` guard prevents
     * double-firing `clear_slot_condition` from rapid clicks while a
     * remove/commit is already in flight. The subscribe happens via `await`
     * so a resubscribe failure surfaces in the user-visible error banner
     * instead of being lost as a fire-and-forget rejection.
     */
    private async _removeCondition(): Promise<void> {
        if (this._dialogSaving) return;
        this._dialogSaving = true;
        try {
            await this._clearSlotCondition();
            this._closeConditionDialog();
            this._unsubscribe();
            await this._subscribe();
            // _subscribe() catches its own errors and stores them on
            // this._error rather than rethrowing, so we promote that to a
            // banner error here.
            if (this._error) {
                throw new Error(this._error);
            }
            this._dialogSaving = false;
        } catch (err) {
            this._setActionError(
                `Failed to remove condition: ${err instanceof Error ? err.message : String(err)}`
            );
            this._dialogSaving = false;
        }
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
        return html`
            <ha-dialog open @closed=${this._closeConditionDialog} .heading=${'Add condition'}>
                <div class="dialog-content">
                    <p class="dialog-description">
                        PIN is active only when this entity is on. Pick a calendar, schedule, binary
                        sensor, switch, or input boolean.
                    </p>
                    <ha-entity-picker
                        .hass=${this._hass}
                        .value=${this._dialogEntityId ?? ''}
                        .label=${'Condition entity'}
                        .includeDomains=${CONDITION_DOMAINS}
                        .entityFilter=${(state: { entity_id: string }) =>
                            (CONDITION_DOMAINS as readonly string[]).includes(
                                state.entity_id.split('.')[0]
                            )}
                        @value-changed=${(e: CustomEvent) => this._handlePickerChange(e)}
                    ></ha-entity-picker>
                    ${this._dialogSaving
                        ? html`<div class="dialog-saving" aria-live="polite">Saving…</div>`
                        : nothing}
                </div>
            </ha-dialog>
        `;
    }

    private _handlePickerChange(e: CustomEvent): void {
        const newValue = (e.detail?.value as string) || null;
        this._dialogEntityId = newValue;
        if (!newValue) return;
        // Re-entry guard while a previous commit is in flight.
        if (this._dialogSaving) return;
        void this._commitConditionPick(newValue);
    }

    private async _commitConditionPick(entityId: string): Promise<void> {
        if (!this._hass || !this._config) {
            this._setActionError('Card not initialized');
            return;
        }
        if (!(entityId in this._hass.states)) {
            this._setActionError(`Selected entity not found: ${entityId}`);
            return;
        }
        this._dialogSaving = true;
        try {
            await this._setSlotCondition(entityId);
            this._closeConditionDialog();
            this._unsubscribe();
            await this._subscribe();
            if (this._error) {
                throw new Error(this._error);
            }
            this._dialogSaving = false;
        } catch (err) {
            this._setActionError(
                `Failed to set condition: ${err instanceof Error ? err.message : String(err)}`
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
            this._revealedForEdit = true;
            this._unsubscribe();
            this._subscribe()
                .then(() => {
                    this._editingField = 'pin';
                })
                .catch((err: unknown) => {
                    // Resubscribe failed — revert the optimistic reveal so
                    // the UI doesn't claim the PIN was revealed when we
                    // never got the data, and surface the failure.
                    this._revealed = false;
                    this._revealedForEdit = false;
                    this._setActionError(
                        `Failed to reveal PIN: ${err instanceof Error ? err.message : String(err)}`
                    );
                });
        } else {
            this._editingField = field;
        }
    }

    private _exitPinEdit(): void {
        this._editingField = null;
        if (this._revealedForEdit) {
            this._revealed = false;
            this._revealedForEdit = false;
            // Resubscribe with reveal=false so the backend stops sending the
            // unmasked PIN. Fire-and-forget; if it fails, the UI already
            // reflects masked state and the next data tick will catch up.
            this._unsubscribe();
            void this._subscribe().catch((err: unknown) => {
                this._setActionError(
                    `Failed to remask PIN: ${err instanceof Error ? err.message : String(err)}`
                );
            });
        }
    }

    private _handleEditBlur(e: Event): void {
        const target = e.target as HTMLInputElement;
        const wasPin = this._editingField === 'pin';
        this._saveEditValue(target.value);
        if (wasPin) {
            this._exitPinEdit();
        } else {
            this._editingField = null;
        }
    }

    private _handleEditKeydown(e: KeyboardEvent): void {
        if (e.key === 'Enter') {
            const target = e.target as HTMLInputElement;
            const wasPin = this._editingField === 'pin';
            this._saveEditValue(target.value);
            if (wasPin) {
                this._exitPinEdit();
            } else {
                this._editingField = null;
            }
        } else if (e.key === 'Escape') {
            if (this._editingField === 'pin') {
                this._exitPinEdit();
            } else {
                this._editingField = null;
            }
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
        // Track the timer id so a follow-up error can clear the previous
        // timer instead of letting it fire early on the new error and
        // dismiss the banner mid-read. The timer is also cleared in
        // disconnectedCallback so we don't leak it across element removal.
        if (this._actionErrorTimer !== undefined) {
            clearTimeout(this._actionErrorTimer);
        }
        this._actionErrorTimer = setTimeout(() => {
            this._actionError = undefined;
            this._actionErrorTimer = undefined;
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
