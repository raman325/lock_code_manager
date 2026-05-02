import { mdiCheck, mdiClockOutline, mdiClose, mdiEye, mdiEyeOff } from '@mdi/js';
import { MessageBase } from 'home-assistant-js-websocket';
import { LitElement, TemplateResult, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import { lockCodesCardStyles } from './lock-codes-card.styles';
import { LcmSubscriptionMixin } from './subscription-mixin';
import {
    CodeDisplayMode,
    GetConfigEntriesResponse,
    LockCodeManagerConfigEntryDataResponse,
    LockCodesCardConfig,
    LockCoordinatorData,
    LockCoordinatorSlotData,
    SLOT_CODE_UNREADABLE,
    isSlotEmpty,
    isSlotOccupied
} from './types';

const DEFAULT_TITLE = 'Lock Codes';
const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

// Base class with subscription mixin
const LockCodesCardBase = LcmSubscriptionMixin(LitElement);

interface SlotGroup {
    /** For empty groups, the range string like "4-10" or "4, 6, 8" */
    rangeLabel?: string;
    slots: LockCoordinatorSlotData[];
    type: 'active' | 'empty';
}

class LockCodesCard extends LockCodesCardBase {
    static styles = lockCodesCardStyles;

    // Note: _revealed, _unsub, _subscribing provided by LcmSubscriptionMixin
    @property({ attribute: false }) _hass?: HomeAssistant;

    /** Slot currently being edited (for unmanaged slots) */
    @state() private _editingSlot: number | string | null = null;
    /** Current edit value */
    @state() private _editValue = '';
    /** Whether we're saving (to prevent double-submit) */
    @state() private _saving = false;

    _config?: LockCodesCardConfig;
    _data?: LockCoordinatorData;
    _error?: string;

    /** Whether codes were revealed before editing started */
    private _wasRevealedBeforeEdit = false;

    set hass(hass: HomeAssistant) {
        this._hass = hass;
        void this._subscribe();
    }

    static getConfigElement(): HTMLElement {
        return document.createElement('lcm-lock-codes-editor');
    }

    static async getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>> {
        const stub = { lock_entity_id: 'lock.stub', type: 'custom:lcm-lock-codes' };
        try {
            return await Promise.race([
                (async () => {
                    const entries = await hass.callWS<GetConfigEntriesResponse>({
                        domain: 'lock_code_manager',
                        type: 'config_entries/get'
                    });
                    if (entries.length > 0) {
                        const data = await hass.callWS<LockCodeManagerConfigEntryDataResponse>({
                            config_entry_id: entries[0].entry_id,
                            type: 'lock_code_manager/get_config_entry_data'
                        });
                        if (data.locks.length > 0) {
                            return {
                                lock_entity_id: data.locks[0].entity_id,
                                type: 'custom:lcm-lock-codes'
                            };
                        }
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

    setConfig(config: LockCodesCardConfig): void {
        if (!config.lock_entity_id) {
            throw new Error('lock_entity_id is required');
        }
        if (this._config?.lock_entity_id && this._config.lock_entity_id !== config.lock_entity_id) {
            this._unsubscribe();
            this._data = undefined;
        }
        this._config = config;
        this._isStub = config.lock_entity_id === 'lock.stub';
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
        return {
            lock_entity_id: this._config.lock_entity_id,
            reveal: this._shouldReveal(),
            type: 'lock_code_manager/subscribe_lock_codes'
        };
    }

    protected _handleSubscriptionData(data: unknown): void {
        this._data = data as LockCoordinatorData;
    }

    // connectedCallback and disconnectedCallback provided by mixin

    protected render(): TemplateResult {
        // Show static preview for card picker (stub config)
        if (this._isStub) {
            return html`<ha-card>
                <div class="card-header">
                    <div class="header-icon"><ha-icon icon="mdi:lock-smart"></ha-icon></div>
                    <span class="card-header-title">Lock Code Manager Lock Codes</span>
                </div>
            </ha-card>`;
        }

        const hassLockName =
            this._hass?.states[this._config?.lock_entity_id ?? '']?.attributes?.friendly_name;
        const lockName =
            this._data?.lock_name ?? hassLockName ?? this._config?.lock_entity_id ?? '';
        const headerTitle = this._config?.title ?? lockName ?? DEFAULT_TITLE;

        const isSuspended = this._data?.sync_status === 'suspended';

        return html`
            <ha-card class="${isSuspended ? 'suspended' : ''}">
                <div class="card-header">
                    <div class="header-icon">
                        <ha-icon icon="mdi:lock-smart"></ha-icon>
                    </div>
                    <span class="card-header-title">${headerTitle}</span>
                </div>
                ${isSuspended
                    ? html`<div class="suspended-banner">
                          <ha-icon icon="mdi:alert-circle"></ha-icon>
                          <span>Sync suspended — lock is unreachable</span>
                      </div>`
                    : nothing}
                <div class="card-content">
                    ${this._error
                        ? html`<div class="message">${this._error}</div>`
                        : this._renderSlots()}
                    ${this._renderSummaryTable()}
                </div>
            </ha-card>
        `;
    }

    // Editing methods for unmanaged slots
    private _startEditing(e: Event, slot: LockCoordinatorSlotData): void {
        e.stopPropagation();
        // Save current reveal state before editing
        this._wasRevealedBeforeEdit = this._revealed;
        // For editing, we need the actual code - trigger reveal if masked
        if (!this._revealed) {
            this._revealed = true;
            this._unsubscribe();
            void this._subscribe();
        }
        // Get the current code value (if any); sentinels are not editable values
        const currentCode =
            isSlotOccupied(slot.code) && slot.code !== SLOT_CODE_UNREADABLE
                ? String(slot.code)
                : '';
        this._editValue = currentCode;
        this._editingSlot = slot.slot;
        // Focus input after render
        this.updateComplete.then(() => {
            const input = this.shadowRoot?.querySelector<HTMLInputElement>('.slot-code-input');
            input?.focus();
            input?.select();
        });
    }

    private _handleEditInput(e: Event): void {
        const input = e.target as HTMLInputElement;
        this._editValue = input.value;
    }

    private _handleEditKeydown(e: KeyboardEvent): void {
        if (e.key === 'Enter' && this._editingSlot !== null) {
            void this._saveCode(this._editingSlot);
        } else if (e.key === 'Escape') {
            this._cancelEdit();
        }
    }

    private _cancelEdit(): void {
        this._editingSlot = null;
        this._editValue = '';
        // Restore reveal state if it was changed for editing
        if (this._revealed !== this._wasRevealedBeforeEdit) {
            this._revealed = this._wasRevealedBeforeEdit;
            this._unsubscribe();
            void this._subscribe();
        }
    }

    private async _saveCode(slot: number | string): Promise<void> {
        if (!this._hass || !this._config || this._saving) return;

        this._saving = true;
        const usercode = this._editValue.trim();

        try {
            const slotNum = typeof slot === 'string' ? parseInt(slot, 10) : slot;
            if (usercode) {
                await this._hass.connection.sendMessagePromise({
                    code_slot: slotNum,
                    lock_entity_id: this._config.lock_entity_id,
                    type: 'lock_code_manager/set_usercode',
                    usercode
                });
            } else {
                await this._hass.connection.sendMessagePromise({
                    code_slot: slotNum,
                    lock_entity_id: this._config.lock_entity_id,
                    type: 'lock_code_manager/clear_usercode'
                });
            }
            // Success - exit edit mode
            this._editingSlot = null;
            this._editValue = '';
        } catch (err) {
            // eslint-disable-next-line no-console -- User-facing error, no logger available in card
            console.error('Failed to set usercode:', err);
        } finally {
            this._saving = false;
        }
    }

    private _navigateToSlot(configEntryId: string | undefined): void {
        if (!configEntryId) return;
        // Navigate to the LCM config entry page
        const url = `/config/integrations/integration/lock_code_manager#config_entry=${configEntryId}`;
        history.pushState(null, '', url);
        window.dispatchEvent(new CustomEvent('location-changed'));
    }

    // _toggleReveal, _unsubscribe, _shouldReveal, _subscribe inherited from mixin

    private _renderSlots(): TemplateResult {
        const slots = this._data?.slots ?? [];
        if (slots.length === 0) {
            return html`<div class="message">No codes reported</div>`;
        }

        const groups = this._groupSlots(slots);
        const borrowedSlots = this._identifyBorrowedSlots(groups);
        const result = this._renderGroupsWithBorrowing(groups, borrowedSlots);
        return html`<div class="slots-grid">${result}</div>`;
    }

    /** Find empty-group slots to "borrow" so lone active slots get a grid partner. */
    private _identifyBorrowedSlots(groups: SlotGroup[]): Set<number | string> {
        const borrowed = new Set<number | string>();
        for (let i = 0; i < groups.length; i++) {
            const group = groups[i];
            if (group.type === 'active' && group.slots.length === 1) {
                const [slot] = group.slots;
                const slotNum = typeof slot.slot === 'string' ? parseInt(slot.slot, 10) : slot.slot;
                const isOdd = slotNum % 2 === 1;
                const prevGroup = i > 0 ? groups[i - 1] : null;
                const nextGroup = i < groups.length - 1 ? groups[i + 1] : null;

                if (isOdd && nextGroup?.type === 'empty' && nextGroup.slots.length > 0) {
                    borrowed.add(nextGroup.slots[0].slot);
                } else if (!isOdd && prevGroup?.type === 'empty' && prevGroup.slots.length > 0) {
                    borrowed.add(prevGroup.slots[prevGroup.slots.length - 1].slot);
                }
            }
        }
        return borrowed;
    }

    /** Render slot groups, pairing lone active slots with borrowed empty neighbors. */
    private _renderGroupsWithBorrowing(
        groups: SlotGroup[],
        borrowedSlots: Set<number | string>
    ): TemplateResult[] {
        const result: TemplateResult[] = [];
        for (let i = 0; i < groups.length; i++) {
            const group = groups[i];
            const prevGroup = i > 0 ? groups[i - 1] : null;
            const nextGroup = i < groups.length - 1 ? groups[i + 1] : null;

            if (group.type === 'active') {
                if (group.slots.length === 1) {
                    const [slot] = group.slots;
                    const slotNum =
                        typeof slot.slot === 'string' ? parseInt(slot.slot, 10) : slot.slot;
                    const isOdd = slotNum % 2 === 1;

                    if (isOdd && nextGroup?.type === 'empty' && nextGroup.slots.length > 0) {
                        result.push(this._renderSlotChip(slot, false));
                        result.push(this._renderEmptySlotChip(nextGroup.slots[0]));
                    } else if (
                        !isOdd &&
                        prevGroup?.type === 'empty' &&
                        prevGroup.slots.length > 0
                    ) {
                        result.push(
                            this._renderEmptySlotChip(prevGroup.slots[prevGroup.slots.length - 1])
                        );
                        result.push(this._renderSlotChip(slot, false));
                    } else {
                        result.push(this._renderSlotChip(slot, true));
                    }
                } else {
                    for (const slot of group.slots) {
                        result.push(this._renderSlotChip(slot, false));
                    }
                }
            } else {
                const remainingSlots = group.slots.filter((s) => !borrowedSlots.has(s.slot));
                if (remainingSlots.length > 0) {
                    result.push(
                        this._renderEmptySummary({
                            ...group,
                            rangeLabel: this._formatSlotRange(remainingSlots),
                            slots: remainingSlots
                        })
                    );
                }
            }
        }
        return result;
    }

    private _renderEmptySlotChip(slot: LockCoordinatorSlotData): TemplateResult {
        return html`
            <div class="slot-chip empty">
                <div class="slot-top">
                    <span class="slot-label">Slot ${slot.slot}</span>
                    <div class="slot-badges">
                        <span class="lcm-badge empty">Empty</span>
                    </div>
                </div>
            </div>
        `;
    }

    private _groupSlots(slots: LockCoordinatorSlotData[]): SlotGroup[] {
        const groups: SlotGroup[] = [];
        let currentEmpty: LockCoordinatorSlotData[] = [];
        let currentActive: LockCoordinatorSlotData[] = [];

        const flushEmpty = (): void => {
            if (currentEmpty.length > 0) {
                groups.push({
                    rangeLabel: this._formatSlotRange(currentEmpty),
                    slots: currentEmpty,
                    type: 'empty'
                });
                currentEmpty = [];
            }
        };

        const flushActive = (): void => {
            if (currentActive.length > 0) {
                groups.push({ slots: currentActive, type: 'active' });
                currentActive = [];
            }
        };

        for (const slot of slots) {
            // Expand slots that:
            // - Have active code on lock, OR
            // - Are managed by LCM AND have either configured code or explicit state
            const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);
            const hasManagedState =
                slot.managed === true && (slot.enabled !== undefined || slot.active !== undefined);
            const shouldExpand =
                this._hasCode(slot) ||
                (slot.managed === true && (hasConfiguredCode || hasManagedState));
            if (shouldExpand) {
                flushEmpty();
                currentActive.push(slot);
            } else {
                flushActive();
                currentEmpty.push(slot);
            }
        }
        flushActive();
        flushEmpty();

        return groups;
    }

    private _formatSlotRange(slots: LockCoordinatorSlotData[]): string {
        if (slots.length === 0) return '';
        if (slots.length === 1) return `${slots[0].slot}`;

        const nums = slots.map((s) => Number(s.slot)).filter((n) => !isNaN(n));
        if (nums.length !== slots.length) {
            // Non-numeric slots, just list them
            return `${slots.map((s) => s.slot).join(', ')}`;
        }

        // Find consecutive ranges
        const ranges: string[] = [];
        const [startValue] = nums;
        let start = startValue;
        let end = startValue;

        for (let i = 1; i < nums.length; i++) {
            if (nums[i] === end + 1) {
                end = nums[i];
            } else {
                ranges.push(start === end ? `${start}` : `${start} – ${end}`);
                start = nums[i];
                end = nums[i];
            }
        }
        ranges.push(start === end ? `${start}` : `${start} – ${end}`);

        return `${ranges.join(', ')}`;
    }

    private _renderSlotChip(slot: LockCoordinatorSlotData, isAlone: boolean): TemplateResult {
        const hasCode = this._hasCode(slot);
        const slotName = slot.name?.trim();
        const { managed } = slot;
        const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const showName = !!slotName || managed !== false;
        const isClickable = managed === true && !!slot.config_entry_id;

        // Pending: slot has a configured code but the lock doesn't have it yet.
        // Mirrors the .lcm-code.pending detection on the PIN — out-of-sync,
        // syncing, etc. Defensive default when enabled is unknown (undefined
        // doesn't mean "off").
        const lockHasCode = !isSlotEmpty(slot.code) || !!slot.code_length;
        const isPending = hasConfiguredCode && !lockHasCode && slot.enabled !== false;

        // Determine state for LCM-managed slots:
        // - Active: active=true (code on lock, conditions met)
        // - Inactive: enabled=true + active=false (enabled but conditions blocking)
        // - Disabled: enabled=false (user explicitly disabled)
        // - Empty: no code and no configured_code (unmanaged empty slot)
        let stateClass: string;
        let statusText: string;
        let statusClass: string;

        if (slot.active === true) {
            // Binary sensor ON = active (code on lock, conditions met)
            stateClass = 'active';
            statusText = 'Active';
            statusClass = 'active';
        } else if (slot.enabled === true && slot.active === false) {
            // Enabled switch ON but binary sensor OFF = inactive (conditions blocking)
            stateClass = 'inactive';
            statusText = 'Inactive';
            statusClass = 'inactive';
        } else if (slot.enabled === false) {
            // Enabled switch OFF = disabled by user
            stateClass = 'disabled';
            statusText = 'Disabled';
            statusClass = 'disabled';
        } else if (hasCode) {
            // Fallback: has code on lock but no LCM state info
            stateClass = 'active';
            statusText = 'Active';
            statusClass = 'active';
        } else if (hasConfiguredCode) {
            // Fallback: has configured code but unknown state
            stateClass = 'inactive';
            statusText = 'Inactive';
            statusClass = 'inactive';
        } else {
            stateClass = 'empty';
            statusText = 'Empty';
            statusClass = 'empty';
        }

        // Determine managed class for styling
        const managedClass = managed === true ? 'managed' : managed === false ? 'unmanaged' : '';
        const clickableClass = isClickable ? 'clickable' : '';
        const pendingClass = isPending ? 'pending' : '';

        return html`
            <div
                class="slot-chip ${stateClass} ${pendingClass} ${managedClass} ${clickableClass} ${isAlone
                    ? 'full-width'
                    : ''}"
                title=${isClickable ? 'Click to manage this slot' : nothing}
                role=${isClickable ? 'button' : nothing}
                tabindex=${isClickable ? '0' : nothing}
                aria-label=${isClickable
                    ? `Manage slot ${slot.slot}${slot.config_entry_title ? ` · ${slot.config_entry_title}` : ''}`
                    : nothing}
                @click=${isClickable ? () => this._navigateToSlot(slot.config_entry_id) : nothing}
                @keydown=${isClickable
                    ? (e: KeyboardEvent) => {
                          if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault();
                              this._navigateToSlot(slot.config_entry_id);
                          }
                      }
                    : nothing}
            >
                <div class="slot-top">
                    <span class="slot-label">
                        Slot
                        ${slot.slot}${slot.config_entry_title
                            ? html` ·
                                  <span class="slot-entry-title">${slot.config_entry_title}</span>`
                            : nothing}
                    </span>
                    <div class="slot-badges">
                        <span class="lcm-badge ${statusClass}">
                            ${statusClass === 'active' ||
                            statusClass === 'inactive' ||
                            statusClass === 'disabled'
                                ? html`<span class="dot"></span>`
                                : nothing}
                            ${statusText}
                        </span>
                        ${managed === undefined
                            ? nothing
                            : html`<span class="lcm-badge ${managed ? 'managed' : 'external'}">
                                  ${managed ? 'Managed' : 'Unmanaged'}
                              </span>`}
                    </div>
                </div>
                <div class="slot-content-row">
                    ${showName
                        ? html`<span class="slot-name-row">
                              ${isPending
                                  ? html`<ha-svg-icon
                                        class="slot-name-pending-icon"
                                        .path=${mdiClockOutline}
                                    ></ha-svg-icon>`
                                  : nothing}
                              <span class="slot-name ${slotName ? '' : 'unnamed'}">
                                  ${slotName ?? 'Unnamed'}
                              </span>
                          </span>`
                        : html`<span class="slot-name-row"></span>`}
                    ${this._renderCodeSection(slot, hasCode, mode)}
                </div>
            </div>
        `;
    }

    private _renderCodeSection(
        slot: LockCoordinatorSlotData,
        hasCode: boolean,
        mode: CodeDisplayMode
    ): TemplateResult {
        const isEditing = this._editingSlot === slot.slot;
        const isUnmanaged = slot.managed !== true;

        if (isEditing && isUnmanaged) {
            return this._renderCodeEditMode(slot);
        }
        return this._renderCodeDisplayMode(slot, hasCode, mode, isUnmanaged && !isEditing);
    }

    private _renderCodeEditMode(slot: LockCoordinatorSlotData): TemplateResult {
        return html`
            <div class="slot-code-edit" @click=${(e: Event) => e.stopPropagation()}>
                <div class="slot-code-edit-row">
                    <input
                        class="slot-code-input"
                        type="text"
                        inputmode="numeric"
                        pattern="[0-9]*"
                        placeholder="PIN"
                        .value=${this._editValue}
                        @input=${this._handleEditInput}
                        @keydown=${this._handleEditKeydown}
                        ?disabled=${this._saving}
                    />
                    <div class="slot-code-edit-buttons">
                        <ha-icon-button
                            .path=${mdiCheck}
                            @click=${() => this._saveCode(slot.slot)}
                            .label=${'Save'}
                            ?disabled=${this._saving}
                        ></ha-icon-button>
                        <ha-icon-button
                            .path=${mdiClose}
                            @click=${this._cancelEdit}
                            .label=${'Cancel'}
                            ?disabled=${this._saving}
                        ></ha-icon-button>
                    </div>
                </div>
                <span class="slot-edit-help">
                    ${this._saving ? 'Saving...' : 'Enter to save, Esc to cancel'}
                </span>
            </div>
        `;
    }

    private _renderCodeDisplayMode(
        slot: LockCoordinatorSlotData,
        hasCode: boolean,
        mode: CodeDisplayMode,
        isEditable: boolean
    ): TemplateResult {
        const editableClass = isEditable ? 'editable' : '';
        const codeClass = this._getCodeClass(slot);
        const isPending = codeClass.split(' ').includes('pending');
        return html`
            <div class="slot-code-row">
                <span
                    class="lcm-code ${codeClass} ${editableClass}"
                    title=${isEditable ? 'Click to edit' : nothing}
                    @click=${isEditable ? (e: Event) => this._startEditing(e, slot) : nothing}
                >
                    ${isPending
                        ? html`<ha-svg-icon
                              class="lcm-code-pending-icon"
                              .path=${mdiClockOutline}
                          ></ha-svg-icon>`
                        : nothing}
                    ${this._formatCode(slot)}
                </span>
                ${mode === 'masked_with_reveal' &&
                (hasCode || !!slot.configured_code || !!slot.configured_code_length)
                    ? html`<span class="slot-code-actions">
                          <ha-icon-button
                              class="lcm-reveal-button"
                              .path=${this._revealed ? mdiEyeOff : mdiEye}
                              @click=${(e: Event) => {
                                  // Stop propagation so the click doesn't also
                                  // trigger the parent slot-chip's navigation
                                  // when the chip is clickable (managed slots).
                                  e.stopPropagation();
                                  this._toggleReveal();
                              }}
                              .label=${this._revealed ? 'Hide codes' : 'Reveal codes'}
                          ></ha-icon-button>
                      </span>`
                    : nothing}
            </div>
        `;
    }

    private _getCodeClass(slot: LockCoordinatorSlotData): string {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);
        const maskSuffix = shouldMask ? ' masked' : '';

        if (slot.code === SLOT_CODE_UNREADABLE || slot.code_length) return 'masked';
        if (!isSlotEmpty(slot.code)) return '';

        // Empty/null code on the lock — distinguish "off" (user disabled the slot)
        // from "pending" (slot enabled but code not yet on the lock). Pending is the
        // defensive default when the enabled state is unknown — undefined doesn't
        // mean "off".
        if (slot.configured_code || slot.configured_code_length) {
            const cause = slot.enabled === false ? 'off' : 'pending';
            return `${cause}${maskSuffix}`;
        }
        return 'no-code';
    }

    private _formatCode(slot: LockCoordinatorSlotData): string {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);

        // Active code on the lock
        if (slot.code === SLOT_CODE_UNREADABLE) return '• • •';
        if (isSlotEmpty(slot.code)) {
            if (slot.code_length) return '•'.repeat(slot.code_length);
            // Fall through to configured code or dash below
        } else if (slot.code !== null) {
            return shouldMask ? '•'.repeat(String(slot.code).length) : String(slot.code);
        }

        // Disabled LCM slot: show configured code (respect masking)
        if (slot.configured_code) {
            return shouldMask ? '•'.repeat(slot.configured_code.length) : slot.configured_code;
        }
        if (slot.configured_code_length) {
            return '•'.repeat(slot.configured_code_length);
        }

        return '—';
    }

    private _renderEmptySummary(group: SlotGroup): TemplateResult {
        return html`<div class="empty-summary">
            <ha-icon icon="mdi:minus-circle-outline"></ha-icon>
            <span class="empty-summary-label">Empty slots</span>
            <span class="empty-summary-range">${group.rangeLabel}</span>
        </div>`;
    }

    private _renderSummaryTable(): TemplateResult {
        const slots = this._data?.slots ?? [];
        if (slots.length === 0) {
            return html``;
        }

        // Count by state and managed/unmanaged
        // All slots must be counted - total should equal slots.length
        let managedActive = 0;
        let managedInactive = 0;
        let managedDisabled = 0;
        let unmanagedActive = 0;
        // Empty unmanaged slots (no code, collapsed in UI)
        let unmanagedInactive = 0;

        for (const slot of slots) {
            const hasCode = this._hasCode(slot);
            const isManaged = slot.managed === true;
            const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);

            if (isManaged) {
                // Managed slot - use LCM state fields
                if (slot.active === true) {
                    managedActive += 1;
                } else if (slot.enabled === true && slot.active === false) {
                    // Enabled but conditions blocking
                    managedInactive += 1;
                } else if (slot.enabled === false) {
                    // User explicitly disabled via LCM switch
                    managedDisabled += 1;
                } else if (hasCode) {
                    // Fallback: has code on lock but no state info - treat as active
                    managedActive += 1;
                } else if (hasConfiguredCode) {
                    // Fallback: configured code but unknown state - treat as inactive
                    managedInactive += 1;
                } else {
                    // Fallback: no code and no state info - treat as inactive
                    managedInactive += 1;
                }
            } else if (hasCode) {
                // Unmanaged slot with code = active
                unmanagedActive += 1;
            } else {
                // Unmanaged slot without code = inactive (collapsed in UI)
                unmanagedInactive += 1;
            }
        }

        const managedTotal = managedActive + managedInactive + managedDisabled;
        const unmanagedTotal = unmanagedActive + unmanagedInactive;
        const totalActive = managedActive + unmanagedActive;
        const totalInactive = managedInactive + unmanagedInactive;
        const totalDisabled = managedDisabled;
        // Should always equal managedTotal + unmanagedTotal
        const grandTotal = slots.length;

        const cellClass = (val: number): string => (val === 0 ? 'summary-cell-zero' : '');

        return html`
            <table class="summary-table">
                <thead>
                    <tr>
                        <th></th>
                        <th>Active</th>
                        <th>Inactive</th>
                        <th>Disabled</th>
                        <th>Total</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Managed</td>
                        <td class="${cellClass(managedActive)}">${managedActive}</td>
                        <td class="${cellClass(managedInactive)}">${managedInactive}</td>
                        <td class="${cellClass(managedDisabled)}">${managedDisabled}</td>
                        <td>${managedTotal}</td>
                    </tr>
                    <tr>
                        <td>Unmanaged</td>
                        <td class="${cellClass(unmanagedActive)}">${unmanagedActive}</td>
                        <td class="${cellClass(unmanagedInactive)}">${unmanagedInactive}</td>
                        <td class="${cellClass(0)}">–</td>
                        <td>${unmanagedTotal}</td>
                    </tr>
                    <tr class="total-row">
                        <td>Total</td>
                        <td class="${cellClass(totalActive)}">${totalActive}</td>
                        <td class="${cellClass(totalInactive)}">${totalInactive}</td>
                        <td class="${cellClass(totalDisabled)}">${totalDisabled}</td>
                        <td>${grandTotal}</td>
                    </tr>
                </tbody>
            </table>
        `;
    }

    private _hasCode(slot: LockCoordinatorSlotData): boolean {
        return isSlotOccupied(slot.code, slot.code_length);
    }
}

customElements.define('lcm-lock-codes', LockCodesCard);

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
    description: 'Displays lock slot codes from Lock Code Manager',
    name: 'LCM Lock Codes Card',
    preview: true,
    type: 'lcm-lock-codes'
});
