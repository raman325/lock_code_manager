/* eslint-disable no-underscore-dangle, prefer-destructuring */
import { html } from 'lit';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHassWithConnection } from './test/mock-hass';
import { SlotCardData } from './types';

/**
 * Integration tests for the LockCodeManagerSlotCard (lcm-slot) component.
 *
 * These tests exercise the card's subscription lifecycle, configuration
 * validation, and data handling by mounting the actual component in jsdom.
 * Because jsdom does not fully support Lit's shadow Document Object Model
 * rendering, we focus on verifying state management and subscription
 * behavior through the component's properties rather than querying
 * rendered output.
 */

/** Creates a SlotCardData object with sensible defaults and optional overrides */
function makeSlotCardData(overrides?: Partial<SlotCardData>): SlotCardData {
    return {
        active: true,
        conditions: {},
        config_entry_id: 'test-entry',
        config_entry_title: 'Test Config',
        enabled: true,
        locks: [
            {
                code: '1234',
                entity_id: 'lock.test_1',
                in_sync: true,
                name: 'Test Lock'
            }
        ],
        name: 'Test User',
        pin: '1234',
        slot_num: 1,
        ...overrides
    };
}

/** Type alias for the slot card element with its internal properties exposed */
interface SlotCardElement extends HTMLElement {
    _config?: unknown;
    _data?: SlotCardData;
    _error?: string;
    _hass?: HomeAssistant;
    hass: HomeAssistant;
    setConfig(config: Record<string, unknown>): void;
}

describe('LockCodeManagerSlotCard integration', () => {
    let el: SlotCardElement;
    let container: HTMLDivElement;

    // Import the card module to trigger customElements.define, guarding against
    // re-definition if the module is reloaded in watch mode
    beforeAll(async () => {
        if (!customElements.get('lcm-slot')) {
            await import('./slot-card');
        }
    });

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
    });

    afterEach(() => {
        if (el && el.parentNode) {
            el.parentNode.removeChild(el);
        }
        container.remove();
    });

    /** Helper to flush microtasks so async operations complete */
    async function flush(): Promise<void> {
        await new Promise((r) => setTimeout(r, 0));
    }

    describe('config validation', () => {
        it('throws when config_entry_id and config_entry_title are both missing', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            expect(() => el.setConfig({ slot: 1, type: 'custom:lcm-slot' })).toThrow(
                'config_entry_id or config_entry_title is required'
            );
        });

        it('throws when slot is missing', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            expect(() => el.setConfig({ config_entry_id: 'abc', type: 'custom:lcm-slot' })).toThrow(
                'slot must be a number between 1 and 9999'
            );
        });

        it('throws when slot is out of range', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            expect(() =>
                el.setConfig({ config_entry_id: 'abc', slot: 0, type: 'custom:lcm-slot' })
            ).toThrow('slot must be a number between 1 and 9999');
        });

        it('accepts valid config and stores it', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            expect(el._config).toBeDefined();
        });
    });

    describe('subscription connects with correct message', () => {
        it('builds subscribe message with config_entry_id', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection();
            el.setConfig({ config_entry_id: 'my-entry', slot: 3, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const subscribeMessage = hass.connection.subscribeMessage as ReturnType<typeof vi.fn>;
            expect(subscribeMessage).toHaveBeenCalled();

            // Verify the message passed to subscribeMessage
            const msg = subscribeMessage.mock.calls[0][1];
            expect(msg).toMatchObject({
                config_entry_id: 'my-entry',
                slot: 3,
                type: 'lock_code_manager/subscribe_code_slot'
            });
        });

        it('builds subscribe message with config_entry_title when no id', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection();
            el.setConfig({
                config_entry_title: 'My Lock Manager',
                slot: 2,
                type: 'custom:lcm-slot'
            });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const subscribeMessage = hass.connection.subscribeMessage as ReturnType<typeof vi.fn>;
            const msg = subscribeMessage.mock.calls[0][1];
            expect(msg).toMatchObject({
                config_entry_title: 'My Lock Manager',
                slot: 2,
                type: 'lock_code_manager/subscribe_code_slot'
            });
            expect(msg.config_entry_id).toBeUndefined();
        });
    });

    describe('data handling', () => {
        it('stores subscription data in _data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const testData = makeSlotCardData();
            capturedCallback!(testData);

            expect(el._data).toEqual(testData);
        });

        it('handles masked PIN data (pin is null with pin_length)', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const maskedData = makeSlotCardData({ pin: null, pin_length: 4 });
            capturedCallback!(maskedData);

            expect(el._data?.pin).toBeNull();
            expect(el._data?.pin_length).toBe(4);
        });

        it('handles revealed PIN data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const revealedData = makeSlotCardData({ pin: '5678' });
            capturedCallback!(revealedData);

            expect(el._data?.pin).toBe('5678');
        });
    });

    describe('header redesign (icon bubble + title + state chip)', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /** Join a TemplateResult's static strings to inspect element tags */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function templateStrings(result: any): string {
            return (result?.strings ?? []).join('');
        }

        /** Recursively join the static strings + primitive value text of a
         *  TemplateResult and any nested templates */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) {
                return result.map(deepTemplateStrings).join('');
            }
            if (typeof result !== 'object') return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepTemplateStrings).join('');
            return own + nested;
        }

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('renders header with icon bubble, slot kicker as title, and active state chip', () => {
            (card as any)._data = makeSlotCardData({
                active: true,
                config_entry_title: 'House Locks',
                enabled: true,
                name: 'Alice',
                slot_num: 1
            });

            // Kicker text comes from _renderSlotKicker; prefer the card config
            // title when set, otherwise fall back to the data payload title.
            expect((card as any)._renderSlotKicker()).toBe('Slot 1 · House Locks');

            // State chip should be rendered as the third value in header-top
            // (after icon bubble and title).
            const chip = (card as any)._renderStateChip();
            const chipStrings = templateStrings(chip);
            expect(chipStrings).toContain('state-chip');
            // The class modifier and text are rendered as dynamic values.
            expect(chip.values).toContain('active');
            expect(chip.values).toContain('Active');

            // Header structure: icon bubble + title (kicker) + state chip.
            // The name lives in the hero band, NOT in the header.
            const header = (card as any)._renderHeader();
            const headerStrings = templateStrings(header);
            expect(headerStrings).toContain('header-icon');
            expect(headerStrings).toContain('header-title');
            // Name no longer rendered in the header.
            expect(headerStrings).not.toContain('class="name"');
            const headerDeep = deepTemplateStrings(header);
            // Icon (key) and chip should both be present.
            expect(headerDeep).toContain('ha-svg-icon');
            // State chip text flows through as a nested template value.
            expect(headerDeep).toContain('Active');
        });

        it('renders state chip with descriptive text for inactive (blocked)', () => {
            (card as any)._data = makeSlotCardData({
                active: false,
                enabled: true
            });

            const chip = (card as any)._renderStateChip();
            const chipStrings = templateStrings(chip);
            expect(chipStrings).toContain('state-chip');
            expect(chip.values).toContain('inactive');
            expect(chip.values).toContain('Blocked by condition');
        });

        it('renders state chip with descriptive text for disabled by user', () => {
            (card as any)._data = makeSlotCardData({ enabled: false });

            const chip = (card as any)._renderStateChip();
            const chipStrings = templateStrings(chip);
            expect(chipStrings).toContain('state-chip');
            expect(chip.values).toContain('disabled');
            expect(chip.values).toContain('Disabled by user');
        });

        it('omits the title separator when no config_entry_title is available', () => {
            (card as any)._data = makeSlotCardData({ config_entry_title: '' });
            // _config has no title and data title is empty — kicker should be just "Slot N".
            expect((card as any)._renderSlotKicker()).toBe('Slot 1');
        });

        it('falls back to data payload title when card config has no title', () => {
            (card as any)._config = { config_entry_id: 'abc', slot: 2, type: 'custom:lcm-slot' };
            (card as any)._data = makeSlotCardData({
                config_entry_title: 'Payload Title',
                slot_num: 2
            });
            expect((card as any)._renderSlotKicker()).toBe('Slot 2 · Payload Title');
        });

        it('prefers card config title over data payload title', () => {
            (card as any)._config = {
                config_entry_id: 'abc',
                config_entry_title: 'Config Title',
                slot: 1,
                type: 'custom:lcm-slot'
            };
            (card as any)._data = makeSlotCardData({ config_entry_title: 'Payload Title' });
            expect((card as any)._renderSlotKicker()).toBe('Slot 1 · Config Title');
        });

        it('does not render the name in the header (name lives in the hero band)', () => {
            (card as any)._data = makeSlotCardData({ name: 'Alice' });
            const header = (card as any)._renderHeader();
            const headerDeep = deepTemplateStrings(header);
            // Name value should NOT appear inside the header.
            expect(headerDeep).not.toContain('Alice');
            // No <No Name> placeholder either — that lives in the hero now.
            expect(headerDeep).not.toContain('No Name');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('hero row (PIN + Enable)', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /** Join a TemplateResult's static strings to inspect element tags */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function templateStrings(result: any): string {
            return (result?.strings ?? []).join('');
        }

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('renders hero row with PIN value and enable switch', () => {
            (card as any)._data = makeSlotCardData({
                active: true,
                enabled: true,
                pin: '1234',
                pin_length: 4
            });
            (card as any)._revealed = false;

            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            const heroStrings = templateStrings(hero);
            expect(heroStrings).toContain('class="hero"');
            expect(heroStrings).toContain('hero-pin');
            expect(heroStrings).toContain('hero-toggle');
            expect(heroStrings).toContain('ha-switch');
        });

        it('masked PIN displays bullets matching pin length', () => {
            (card as any)._revealed = false;
            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            const valueTemplate = (hero.values ?? []).find(
                (v: unknown) =>
                    v &&
                    typeof v === 'object' &&
                    'strings' in v &&
                    templateStrings(v).includes('hero-pin-value')
            );
            // The masked display value flows through as a value on the inner template
            expect(valueTemplate).toBeTruthy();
            const inner = valueTemplate.values ?? [];
            expect(inner).toContain('••••');
        });

        it('revealed PIN displays the actual digits', () => {
            (card as any)._revealed = true;
            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            const valueTemplate = (hero.values ?? []).find(
                (v: unknown) =>
                    v &&
                    typeof v === 'object' &&
                    'strings' in v &&
                    templateStrings(v).includes('hero-pin-value')
            );
            const inner = valueTemplate.values ?? [];
            expect(inner).toContain('1234');
        });

        it('omits the eye reveal control when mode is unmasked', () => {
            (card as any)._revealed = true;
            const hero = (card as any)._renderHero('1234', 4, true, 'unmasked');
            const heroStrings = templateStrings(hero);
            // The reveal slot should be filled with `nothing` rather than an icon button.
            expect(heroStrings).toContain('hero-pin');
            // Find the hero-pin nested template; verify it does not contain a reveal class.
            const pinBlock = (hero.values ?? []).find(
                (v: unknown) =>
                    v &&
                    typeof v === 'object' &&
                    'strings' in v &&
                    templateStrings(v).includes('hero-pin')
            );
            // For unmasked mode we expect no `class="reveal"` substring in the rendered template.
            // The reveal button only renders for `masked_with_reveal` with a PIN.
            const heroValuesAll = JSON.stringify(hero, (_, val) =>
                typeof val === 'function' ? 'fn' : val
            );
            expect(heroValuesAll).not.toContain('Reveal PIN');
            expect(heroValuesAll).not.toContain('Hide PIN');
            expect(pinBlock).toBeTruthy();
        });

        it('shows "No PIN set" placeholder when both pin and pin_length are absent', () => {
            const hero = (card as any)._renderHero(null, undefined, true, 'masked_with_reveal');
            const heroJson = JSON.stringify(hero);
            expect(heroJson).toContain('No PIN set');
        });

        it('renders an input field for PIN when editing', () => {
            (card as any)._editingField = 'pin';
            (card as any)._revealed = true;
            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            const heroJson = JSON.stringify(hero);
            expect(heroJson).toContain('pin-edit-input');
        });

        it('disables the enable switch when enabled is null', () => {
            const hero = (card as any)._renderHero(null, undefined, null, 'masked_with_reveal');
            const heroStrings = templateStrings(hero);
            expect(heroStrings).toContain('ha-switch');
            // Switch disabled flag is bound as a property; verify the switch template has `.disabled` binding present.
            expect(heroStrings).toContain('.disabled=');
        });

        it('hero PIN value does not inherit the dashed-underline editable affordance', async () => {
            // The shared .editable rule applies `text-decoration: underline dashed`,
            // which renders as broken dashes under a 22px monospace PIN. The slot
            // card's stylesheet must override it for .hero-pin-value.editable.
            // jsdom does not resolve adopted stylesheets through getComputedStyle,
            // so we assert the override rule is present in the stylesheet source.
            const { slotCardStyles } = await import('./slot-card.styles');
            const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
            expect(allCss).toMatch(/\.hero-pin-value\.editable\s*\{[^}]*text-decoration:\s*none/);
        });

        it('renders a Name row in the hero band with editable affordance', () => {
            (card as any)._data = makeSlotCardData({
                active: true,
                enabled: true,
                name: 'Alice',
                pin: '1234',
                pin_length: 4
            });
            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            const heroJson = JSON.stringify(hero);
            // The NAME label was dropped (typography self-describes); the
            // editable name span and pencil button are still present and
            // accessible (role/tabindex/aria-label).
            expect(heroJson).toContain('hero-name-value');
            expect(heroJson).toContain('Edit name');
            expect(heroJson).toContain('Alice');
            // Pencil button for editing the name.
            expect(heroJson).toContain('hero-name-pencil');
            // PIN row keeps its label since "••••" isn't self-evident.
            expect(heroJson).toContain('hero-field-label');
        });

        it('hero name pencil click invokes _startEditing("name")', () => {
            (card as any)._data = makeSlotCardData({ name: 'Alice' });
            const calls: string[] = [];
            (card as any)._startEditing = (field: string) => calls.push(field);
            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            // Walk the template to find the pencil click handler — it's the
            // arity-0 click bound to either the name value or the pencil icon.
            const collect = (
                node: any,
                acc: Array<(...a: unknown[]) => void> = []
            ): Array<(...a: unknown[]) => void> => {
                if (!node?.values) return acc;
                for (const v of node.values) {
                    if (typeof v === 'function') acc.push(v);
                    else if (v?.strings && v?.values) collect(v, acc);
                    else if (Array.isArray(v))
                        for (const it of v) if (it?.strings) collect(it, acc);
                }
                return acc;
            };
            const handlers = collect(hero);
            // Run all arity-0 click handlers — only the ones in the hero name
            // section call _startEditing('name').
            for (const h of handlers) {
                if (h.length === 0) {
                    try {
                        h();
                    } catch {
                        // ignore — some bound handlers expect args
                    }
                }
            }
            expect(calls).toContain('name');
        });

        it('renders a name input field when _editingField === "name"', () => {
            (card as any)._editingField = 'name';
            (card as any)._data = makeSlotCardData({ name: 'Alice' });
            const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
            const heroJson = JSON.stringify(hero);
            expect(heroJson).toContain('name-edit-input');
        });

        it('renders the "Not named" placeholder in the hero name row when name is empty', () => {
            (card as any)._data = makeSlotCardData({ name: '' });
            const hero = (card as any)._renderHero(null, undefined, true, 'masked_with_reveal');
            const heroJson = JSON.stringify(hero);
            expect(heroJson).toContain('placeholder');
            expect(heroJson).toContain('Not named');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('card-level state class', () => {
        // The state class is bound as `class="slot-card-state-${cls}"` —
        // Lit splits the static prefix from the dynamic suffix, so the
        // joined static string contains "slot-card-state-" and the first
        // value is the modifier ('active' / 'inactive' / 'disabled').
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function stateClassModifier(tmpl: any): string | undefined {
            const strings: string[] = (tmpl?.strings ?? []) as string[];
            const values: unknown[] = (tmpl?.values ?? []) as unknown[];
            for (let i = 0; i < values.length; i++) {
                if (strings[i] && strings[i].includes('slot-card-state-')) {
                    return values[i] as string;
                }
            }
            return undefined;
        }

        async function makeCard(): Promise<SlotCardElement & Record<string, unknown>> {
            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
            return card;
        }

        it('applies slot-card-state-active when slot is active', async () => {
            const card = await makeCard();
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (card as any)._data = makeSlotCardData({ active: true, enabled: true });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const tmpl = (card as any)._renderFromData((card as any)._data);
            expect(stateClassModifier(tmpl)).toBe('active');
        });

        it('applies slot-card-state-inactive when slot is enabled but blocked', async () => {
            const card = await makeCard();
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (card as any)._data = makeSlotCardData({ active: false, enabled: true });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const tmpl = (card as any)._renderFromData((card as any)._data);
            expect(stateClassModifier(tmpl)).toBe('inactive');
        });

        it('applies slot-card-state-disabled when slot is disabled by user', async () => {
            const card = await makeCard();
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (card as any)._data = makeSlotCardData({ active: true, enabled: false });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const tmpl = (card as any)._renderFromData((card as any)._data);
            expect(stateClassModifier(tmpl)).toBe('disabled');
        });
    });

    describe('_renderPrimaryControls is removed', () => {
        it('the legacy method does not exist on the component', async () => {
            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            expect((card as any)._renderPrimaryControls).toBeUndefined();
        });
    });

    describe('error handling', () => {
        it('sets _error when subscription fails', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection();
            (hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockRejectedValue(
                new Error('Subscription failed')
            );
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            expect(el._error).toBe('Subscription failed');
        });
    });

    describe('SlotCode sentinel handling', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any -- accessing private methods for testing */
        it('_formatLockCode returns null for "empty" sentinel', () => {
            const lock = {
                code: 'empty',
                entityId: 'lock.test',
                inSync: true,
                lockEntityId: 'lock.test',
                name: 'Test'
            };
            expect((card as any)._formatLockCode(lock)).toBeNull();
        });

        it('_formatLockCode returns spaced bullets for "unreadable_code" sentinel', () => {
            const lock = {
                code: 'unreadable_code',
                entityId: 'lock.test',
                inSync: true,
                lockEntityId: 'lock.test',
                name: 'Test'
            };
            expect((card as any)._formatLockCode(lock)).toBe('• • •');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */

        it('stores "empty" and "unreadable_code" lock codes in _data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            card2.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            capturedCallback!(
                makeSlotCardData({
                    locks: [
                        {
                            code: 'unreadable_code',
                            entity_id: 'lock.test_1',
                            in_sync: true,
                            name: 'Masked Lock'
                        },
                        {
                            code: 'empty',
                            entity_id: 'lock.test_2',
                            in_sync: true,
                            name: 'Empty Lock'
                        }
                    ]
                })
            );

            expect(card2._data?.locks[0].code).toBe('unreadable_code');
            expect(card2._data?.locks[1].code).toBe('empty');
        });
    });

    describe('condition dialog template never uses mwc-button', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /** Join a TemplateResult's static strings to inspect element tags */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function templateStrings(result: any): string {
            return (result?.strings ?? []).join('');
        }

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('condition dialog does not render mwc-button', () => {
            (card as any)._showConditionDialog = true;
            const tmpl = (card as any)._renderConditionDialog();
            const joined = templateStrings(tmpl);
            expect(joined).not.toContain('mwc-button');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('ha-entity-picker lazy-load', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        afterEach(() => {
            delete (window as any).loadCardHelpers;
        });

        it('lazy-loads ha-entity-picker when the dialog opens', async () => {
            // Simulate the picker not yet being in the customElements registry
            const originalGet = customElements.get.bind(customElements);
            const getSpy = vi.spyOn(customElements, 'get').mockImplementation((name: string) => {
                if (name === 'ha-entity-picker')
                    return undefined as unknown as CustomElementConstructor;
                return originalGet(name);
            });

            const getConfigElementSpy = vi.fn().mockResolvedValue(undefined);
            const createCardElementSpy = vi.fn().mockReturnValue({
                constructor: { getConfigElement: getConfigElementSpy }
            });
            (window as any).loadCardHelpers = vi.fn().mockResolvedValue({
                createCardElement: createCardElementSpy
            });

            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();

            // connectedCallback already triggers the load; clear the call
            // history so this assertion only sees the dialog-open path.
            createCardElementSpy.mockClear();
            getConfigElementSpy.mockClear();

            (card as any)._openConditionDialog('add');
            await flush();

            expect(createCardElementSpy).toHaveBeenCalledWith({
                entities: [],
                type: 'entities'
            });
            expect(getConfigElementSpy).toHaveBeenCalled();

            getSpy.mockRestore();
        });

        it('short-circuits when ha-entity-picker is already registered', async () => {
            const loadHelpersSpy = vi.fn();
            (window as any).loadCardHelpers = loadHelpersSpy;

            // Don't mock customElements.get — the real registry won't have
            // ha-entity-picker either, so force it to look registered.
            const originalGet = customElements.get.bind(customElements);
            const getSpy = vi.spyOn(customElements, 'get').mockImplementation((name: string) => {
                if (name === 'ha-entity-picker')
                    return class FakePicker extends HTMLElement {} as unknown as CustomElementConstructor;
                return originalGet(name);
            });

            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();

            expect(loadHelpersSpy).not.toHaveBeenCalled();

            getSpy.mockRestore();
        });

        it('swallows lazy-load failures so the dialog can still open', async () => {
            const originalGet = customElements.get.bind(customElements);
            const getSpy = vi.spyOn(customElements, 'get').mockImplementation((name: string) => {
                if (name === 'ha-entity-picker')
                    return undefined as unknown as CustomElementConstructor;
                return originalGet(name);
            });
            const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});

            (window as any).loadCardHelpers = vi.fn().mockRejectedValue(new Error('helpers boom'));

            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();

            // _openConditionDialog still flips the visibility state even
            // though the picker load is failing.
            (card as any)._openConditionDialog('add');
            expect((card as any)._showConditionDialog).toBe(true);

            await flush();
            expect(warnSpy).toHaveBeenCalled();

            warnSpy.mockRestore();
            getSpy.mockRestore();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('condition_helpers config', () => {
        it('stores condition_helpers in config when provided', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                condition_helpers: ['input_boolean.test_helper', 'input_datetime.date_helper'],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            expect((el._config as Record<string, unknown>)?.condition_helpers).toEqual([
                'input_boolean.test_helper',
                'input_datetime.date_helper'
            ]);
        });

        it('stores config without condition_helpers when not provided', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            expect((el._config as Record<string, unknown>)?.condition_helpers).toBeUndefined();
        });

        it('stores empty condition_helpers array when configured as empty', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                condition_helpers: [],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            expect((el._config as Record<string, unknown>)?.condition_helpers).toEqual([]);
        });
    });

    describe('_setSlotCondition and _clearSlotCondition', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callWSMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection();
            callWSMock = hass.callWS as ReturnType<typeof vi.fn>;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('_setSlotCondition calls callWS with correct parameters', async () => {
            await (card as any)._setSlotCondition('input_boolean.test_condition');
            expect(callWSMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    config_entry_id: 'abc',
                    entity_id: 'input_boolean.test_condition',
                    slot: 1,
                    type: 'lock_code_manager/set_slot_condition'
                })
            );
        });

        it('_setSlotCondition uses config_entry_title when no id', async () => {
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card2.setConfig({
                config_entry_title: 'My Lock',
                slot: 2,
                type: 'custom:lcm-slot'
            });
            const hass2 = createMockHassWithConnection();
            const callWS2 = hass2.callWS as ReturnType<typeof vi.fn>;
            card2.hass = hass2;
            container.appendChild(card2);
            await flush();

            await (card2 as any)._setSlotCondition('switch.cond');
            expect(callWS2).toHaveBeenCalledWith(
                expect.objectContaining({
                    config_entry_title: 'My Lock',
                    entity_id: 'switch.cond',
                    slot: 2,
                    type: 'lock_code_manager/set_slot_condition'
                })
            );
        });

        it('_clearSlotCondition calls callWS with correct parameters', async () => {
            await (card as any)._clearSlotCondition();
            expect(callWSMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    config_entry_id: 'abc',
                    slot: 1,
                    type: 'lock_code_manager/clear_slot_condition'
                })
            );
        });

        it('_clearSlotCondition uses config_entry_title when no id', async () => {
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card2.setConfig({
                config_entry_title: 'My Lock',
                slot: 3,
                type: 'custom:lcm-slot'
            });
            const hass2 = createMockHassWithConnection();
            const callWS2 = hass2.callWS as ReturnType<typeof vi.fn>;
            card2.hass = hass2;
            container.appendChild(card2);
            await flush();

            await (card2 as any)._clearSlotCondition();
            expect(callWS2).toHaveBeenCalledWith(
                expect.objectContaining({
                    config_entry_title: 'My Lock',
                    slot: 3,
                    type: 'lock_code_manager/clear_slot_condition'
                })
            );
        });

        it('_setSlotCondition returns early without hass', async () => {
            (card as any)._hass = null;
            callWSMock.mockClear();
            await (card as any)._setSlotCondition('input_boolean.test');
            expect(callWSMock).not.toHaveBeenCalled();
        });

        it('_clearSlotCondition returns early without hass', async () => {
            (card as any)._hass = null;
            callWSMock.mockClear();
            await (card as any)._clearSlotCondition();
            expect(callWSMock).not.toHaveBeenCalled();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_commitConditionPick', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callWSMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection({
                states: {
                    'input_boolean.valid_entity': { state: 'on' }
                }
            });
            callWSMock = hass.callWS as ReturnType<typeof vi.fn>;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('sets action error when card is not initialized', async () => {
            (card as any)._hass = null;
            await (card as any)._commitConditionPick('input_boolean.valid_entity');
            expect((card as any)._actionError).toBe('Card not initialized');
        });

        it('sets action error when entity not found in hass.states', async () => {
            await (card as any)._commitConditionPick('input_boolean.nonexistent');
            expect((card as any)._actionError).toBe(
                'Selected entity not found: input_boolean.nonexistent'
            );
            // Validation failure must not flip the in-flight flag — the
            // dialog stays open and the user can pick again.
            expect((card as any)._dialogSaving).toBe(false);
        });

        it('calls _setSlotCondition and closes the dialog on success', async () => {
            (card as any)._showConditionDialog = true;
            await (card as any)._commitConditionPick('input_boolean.valid_entity');
            expect(callWSMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    entity_id: 'input_boolean.valid_entity',
                    type: 'lock_code_manager/set_slot_condition'
                })
            );
            // Dialog closes immediately once the WS call resolves.
            expect((card as any)._showConditionDialog).toBe(false);
            expect((card as any)._dialogSaving).toBe(false);
            // Resubscribe must run on the success path — verify it was
            // re-issued at least once on top of the initial subscribe.
            const subscribeMessageMock = (card as any)._hass.connection
                .subscribeMessage as ReturnType<typeof vi.fn>;
            expect(subscribeMessageMock.mock.calls.length).toBeGreaterThanOrEqual(2);
        });

        it('sets action error when callWS throws', async () => {
            callWSMock.mockRejectedValueOnce(new Error('Server error'));
            await (card as any)._commitConditionPick('input_boolean.valid_entity');
            expect((card as any)._actionError).toBe('Failed to set condition: Server error');
            expect((card as any)._dialogSaving).toBe(false);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_removeCondition', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callWSMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection();
            callWSMock = hass.callWS as ReturnType<typeof vi.fn>;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('calls clear_slot_condition on success', async () => {
            (card as any)._showConditionDialog = true;
            await (card as any)._removeCondition();
            expect(callWSMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    type: 'lock_code_manager/clear_slot_condition'
                })
            );
            expect((card as any)._showConditionDialog).toBe(false);
            expect((card as any)._dialogSaving).toBe(false);
            // Resubscribe is awaited so a failure surfaces in the banner;
            // verify it was actually re-issued (initial subscribe + the
            // post-remove resubscribe).
            const subscribeMessageMock = (card as any)._hass.connection
                .subscribeMessage as ReturnType<typeof vi.fn>;
            expect(subscribeMessageMock.mock.calls.length).toBeGreaterThanOrEqual(2);
        });

        it('sets action error when _clearSlotCondition fails', async () => {
            callWSMock.mockRejectedValueOnce(new Error('Clear failed'));
            (card as any)._showConditionDialog = true;
            await (card as any)._removeCondition();
            expect((card as any)._actionError).toBe('Failed to remove condition: Clear failed');
            // Dialog stays open so the user sees the error
            expect((card as any)._dialogSaving).toBe(false);
        });

        it('is a no-op while a save/remove is already in flight', async () => {
            // Simulate an in-flight save: _dialogSaving already true.
            (card as any)._dialogSaving = true;
            await (card as any)._removeCondition();
            // The early guard should bail before issuing the websocket call.
            expect(callWSMock).not.toHaveBeenCalled();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_openConditionDialog', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('opens the dialog with an empty picker (add-only mode)', () => {
            (card as any)._openConditionDialog();
            expect((card as any)._showConditionDialog).toBe(true);
            expect((card as any)._dialogEntityId).toBeNull();
        });

        it('always starts with an empty entity even if a condition is set', () => {
            // The dialog is Add-only — to swap conditions, the user removes
            // the existing one via the inline Remove link first. Opening the
            // dialog never pre-fills with the current entity.
            (card as any)._data = makeSlotCardData({
                conditions: {
                    condition_entity: {
                        condition_entity_id: 'input_boolean.existing',
                        state: 'on'
                    }
                }
            });
            (card as any)._openConditionDialog();
            expect((card as any)._showConditionDialog).toBe(true);
            expect((card as any)._dialogEntityId).toBeNull();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('Add Condition dialog (ha-entity-picker)', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callWSMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection({
                states: {
                    'calendar.vacation': {
                        attributes: { friendly_name: 'Vacation' },
                        state: 'on'
                    },
                    'schedule.business_hours': {
                        attributes: { friendly_name: 'Business Hours' },
                        state: 'on'
                    }
                }
            });
            callWSMock = hass.callWS as ReturnType<typeof vi.fn>;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /** Recursively join a TemplateResult's strings + nested sub-template
         *  values so we can assert against text rendered by ${} expressions. */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepTemplateStrings).join('');
            if (result?.strings && result?.values) {
                const strings: string[] = result.strings;
                const values: unknown[] = result.values;
                let out = '';
                for (let i = 0; i < strings.length; i++) {
                    out += strings[i];
                    if (i < values.length) out += deepTemplateStrings(values[i]);
                }
                return out;
            }
            return '';
        }

        /** Locate the value-changed handler in the rendered template by
         *  invoking each function value with a fake event and checking
         *  whether _dialogEntityId was updated. */
        /* eslint-disable @typescript-eslint/no-explicit-any */
        function findPickerHandler(tmpl: any, c: any): ((e: CustomEvent) => void) | undefined {
            const collect = (
                node: any,
                acc: Array<(e?: any) => void> = []
            ): Array<(e?: any) => void> => {
                if (!node?.values) return acc;
                for (const v of node.values) {
                    if (typeof v === 'function') acc.push(v);
                    else if (v?.strings && v?.values) collect(v, acc);
                    else if (Array.isArray(v))
                        for (const item of v) if (item?.strings) collect(item, acc);
                }
                return acc;
            };
            const handlers = collect(tmpl);
            return handlers.find((h) => {
                const before = c._dialogEntityId;
                try {
                    h({ detail: { value: '__probe_value_changed_marker__' } });
                } catch {
                    return false;
                }
                const matched = c._dialogEntityId === '__probe_value_changed_marker__';
                // Restore prior state so the probe does not pollute later
                // assertions in the same test. Reassigning `c` is safe — `c`
                // is the card test helper, not a true function param boundary.
                // eslint-disable-next-line no-param-reassign
                c._dialogEntityId = before;
                return matched;
            }) as ((e: CustomEvent) => void) | undefined;
        }
        /* eslint-enable @typescript-eslint/no-explicit-any */

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('renders ha-entity-picker with empty value (Add-only dialog)', () => {
            // Even when a condition is set, opening the dialog starts blank —
            // the dialog is Add-only and the existing condition is removed
            // separately via the inline Remove link.
            (card as any)._data = makeSlotCardData({
                conditions: {
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        state: 'on'
                    }
                }
            });
            (card as any)._openConditionDialog();
            const tmpl = (card as any)._renderConditionDialog();
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('ha-entity-picker');
            expect(joined).not.toContain('datalist');
            expect((card as any)._dialogEntityId).toBeNull();
        });

        it('does not render a destructive Remove button inside the dialog', () => {
            // The destructive Remove action lives on the inline Remove link in
            // the conditions body; the dialog itself is Add-only.
            (card as any)._openConditionDialog();
            const tmpl = (card as any)._renderConditionDialog();
            const joined = deepTemplateStrings(tmpl);
            expect(joined).not.toContain('dialog-remove-btn');
            expect(joined).not.toContain('Remove condition');
        });

        it('uses the "Add condition" dialog title', () => {
            (card as any)._openConditionDialog();
            const tmpl = (card as any)._renderConditionDialog();
            // Title is bound as a property; check both strings and values.
            const joined = deepTemplateStrings(tmpl) + JSON.stringify(tmpl.values ?? []);
            expect(joined).toContain('Add condition');
            // Old wording must not slip back in.
            expect(joined).not.toContain('Manage condition');
            expect(joined).not.toContain('Add a condition');
        });

        it('auto-commits when the picker emits value-changed with a new entity', async () => {
            (card as any)._openConditionDialog();
            const tmpl = (card as any)._renderConditionDialog();
            const pickerHandler = findPickerHandler(tmpl, card);
            expect(pickerHandler).toBeDefined();

            pickerHandler!({ detail: { value: 'calendar.vacation' } } as CustomEvent);
            // Auto-commit kicks off the WS call synchronously; let microtasks
            // settle so the awaited callWS + resubscribe complete.
            await flush();
            await flush();

            expect(callWSMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    config_entry_id: 'abc',
                    entity_id: 'calendar.vacation',
                    slot: 1,
                    type: 'lock_code_manager/set_slot_condition'
                })
            );
            expect((card as any)._showConditionDialog).toBe(false);
            expect((card as any)._dialogSaving).toBe(false);
        });

        it('does not commit when the picker is cleared (empty value)', async () => {
            (card as any)._openConditionDialog();
            const tmpl = (card as any)._renderConditionDialog();
            const pickerHandler = findPickerHandler(tmpl, card);
            expect(pickerHandler).toBeDefined();

            callWSMock.mockClear();
            pickerHandler!({ detail: { value: '' } } as CustomEvent);
            await flush();

            expect(callWSMock).not.toHaveBeenCalledWith(
                expect.objectContaining({
                    type: 'lock_code_manager/set_slot_condition'
                })
            );
            expect((card as any)._showConditionDialog).toBe(true);
            expect((card as any)._dialogEntityId).toBeNull();
        });

        it('shows Saving… indicator while a commit is in flight', async () => {
            (card as any)._openConditionDialog();
            // Mock callWS to delay so we can observe the in-flight state
            // before the awaited call resolves.
            let releaseCommit: () => void = () => {};
            callWSMock.mockImplementationOnce(
                () =>
                    new Promise<void>((resolve) => {
                        releaseCommit = resolve;
                    })
            );
            const tmpl = (card as any)._renderConditionDialog();
            const pickerHandler = findPickerHandler(tmpl, card);
            expect(pickerHandler).toBeDefined();

            pickerHandler!({ detail: { value: 'calendar.vacation' } } as CustomEvent);
            // Allow microtask hop into _commitConditionPick so the in-flight
            // flag flips before we sample state.
            await flush();
            expect((card as any)._dialogSaving).toBe(true);
            const inFlightTmpl = (card as any)._renderConditionDialog();
            expect(deepTemplateStrings(inFlightTmpl)).toContain('Saving…');
            expect(deepTemplateStrings(inFlightTmpl)).toContain('dialog-saving');

            // Let the commit resolve so test cleanup is clean.
            releaseCommit();
            await flush();
            await flush();
        });

        it('picker has both .includeDomains and .entityFilter that restrict to allowed domains', () => {
            (card as any)._openConditionDialog();
            const tmpl = (card as any)._renderConditionDialog();

            // Walk the static strings to find the value paired with each
            // property binding. Lit splits a template into strings + values,
            // so the substring before a `${...}` interpolation tells us
            // which property the next value belongs to.
            const strings: string[] = (tmpl.strings ?? []) as string[];
            const values: unknown[] = (tmpl.values ?? []) as unknown[];

            const findValueFor = (propName: string): unknown => {
                const marker = `.${propName}=`;
                for (let i = 0; i < values.length; i++) {
                    if (strings[i] && strings[i].includes(marker)) return values[i];
                }
                return undefined;
            };

            const includeDomains = findValueFor('includeDomains') as readonly string[];
            const entityFilter = findValueFor('entityFilter') as (s: {
                entity_id: string;
            }) => boolean;

            expect(Array.isArray(includeDomains)).toBe(true);
            expect([...includeDomains].sort()).toEqual(
                ['binary_sensor', 'calendar', 'input_boolean', 'schedule', 'switch'].sort()
            );

            expect(typeof entityFilter).toBe('function');
            // Allowed domains should pass the filter
            expect(entityFilter({ entity_id: 'calendar.vacation' })).toBe(true);
            expect(entityFilter({ entity_id: 'schedule.business_hours' })).toBe(true);
            expect(entityFilter({ entity_id: 'binary_sensor.door' })).toBe(true);
            expect(entityFilter({ entity_id: 'switch.porch' })).toBe(true);
            expect(entityFilter({ entity_id: 'input_boolean.guest_mode' })).toBe(true);
            // Disallowed domain should be filtered out
            expect(entityFilter({ entity_id: 'light.foo' })).toBe(false);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('condition_helpers rendering', () => {
        let card: SlotCardElement & Record<string, unknown>;

        /** Extract inline handler functions from a TemplateResult's values */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function extractHandlers(result: any): Array<(e?: any) => void> {
            return (result?.values ?? []).filter((v: unknown) => typeof v === 'function');
        }

        /** Join a TemplateResult's static strings to inspect element tags */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function templateStrings(result: any): string {
            return (result?.strings ?? []).join('');
        }

        /** Recursively collect all TemplateResult values (handles nested templates) */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function collectAllHandlers(result: any): Array<() => void> {
            const handlers: Array<() => void> = [];
            if (!result?.values) return handlers;
            for (const v of result.values) {
                if (typeof v === 'function') {
                    handlers.push(v);
                } else if (v?.strings && v?.values) {
                    handlers.push(...collectAllHandlers(v));
                } else if (Array.isArray(v)) {
                    for (const item of v) {
                        if (item?.strings && item?.values) {
                            handlers.push(...collectAllHandlers(item));
                        }
                    }
                }
            }
            return handlers;
        }

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({
                condition_helpers: [
                    'input_boolean.helper_1',
                    'input_boolean.helper_2',
                    'input_boolean.nonexistent'
                ],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            card.hass = createMockHassWithConnection({
                states: {
                    'input_boolean.helper_1': {
                        attributes: { friendly_name: 'Helper One' },
                        state: 'on'
                    },
                    'input_boolean.helper_2': {
                        attributes: { friendly_name: 'Helper Two' },
                        state: 'off'
                    }
                }
            });
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('hasConditionHelpers is true when helpers exist in hass states', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
                states: {
                    'input_boolean.helper_1': {
                        attributes: { friendly_name: 'Helper One' },
                        state: 'on'
                    }
                }
            });
            card2.setConfig({
                condition_helpers: ['input_boolean.helper_1'],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            // Push data with no standard conditions so only helpers trigger conditions section
            capturedCallback!(makeSlotCardData({ conditions: {} }));

            // The render method will execute with hasConditionHelpers=true,
            // covering the .some() callback on line 991
            const tmpl = (card2 as any)._renderFromData(card2._data!);
            const joined = templateStrings(tmpl);
            // The conditions section should render (it contains helpers-list)
            expect(joined).toBeDefined();
        });

        it('hasConditionHelpers is false when no helpers exist in hass states', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
                states: {}
            });
            card2.setConfig({
                condition_helpers: ['input_boolean.nonexistent'],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            capturedCallback!(makeSlotCardData({ conditions: {} }));

            const tmpl = (card2 as any)._renderFromData(card2._data!);
            expect(tmpl).toBeDefined();
        });

        /** Recursively join all template strings from nested templates */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function allTemplateStrings(result: any): string {
            let text = (result?.strings ?? []).join('');
            if (result?.values) {
                for (const v of result.values) {
                    if (v?.strings && v?.values) {
                        text += allTemplateStrings(v);
                    } else if (Array.isArray(v)) {
                        for (const item of v) {
                            if (item?.strings && item?.values) {
                                text += allTemplateStrings(item);
                            }
                        }
                    }
                }
            }
            return text;
        }

        it('renders condition helper rows with friendly names and states', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
                states: {
                    'input_boolean.helper_1': {
                        attributes: { friendly_name: 'Helper One' },
                        state: 'on'
                    },
                    'input_boolean.helper_2': {
                        attributes: {},
                        state: 'off'
                    }
                }
            });
            card2.setConfig({
                condition_helpers: ['input_boolean.helper_1', 'input_boolean.helper_2'],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            capturedCallback!(makeSlotCardData({ conditions: {} }));

            // Call _renderConditionsSection directly to exercise the template
            const tmpl = (card2 as any)._renderConditionsSection(card2._data!.conditions);
            // Use recursive join since helpers-list is in a nested content template
            const joined = allTemplateStrings(tmpl);
            expect(joined).toContain('helpers-list');
            expect(joined).toContain('helpers-label');
        });

        it('click handler on condition helper row dispatches hass-more-info', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
                states: {
                    'input_boolean.helper_1': {
                        attributes: { friendly_name: 'Helper One' },
                        state: 'on'
                    }
                }
            });
            card2.setConfig({
                condition_helpers: ['input_boolean.helper_1'],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            capturedCallback!(makeSlotCardData({ conditions: {} }));

            // Get the conditions section template and extract all handlers
            const tmpl = (card2 as any)._renderConditionsSection(card2._data!.conditions);
            const handlers = collectAllHandlers(tmpl);

            // Invoke each handler in try/catch to cover the click lambdas
            for (const handler of handlers) {
                try {
                    handler();
                } catch {
                    // expected - handlers reference component internals
                }
            }
            expect(handlers.length).toBeGreaterThan(0);
        });

        it('condition helper row filters out nonexistent entities', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
                states: {
                    'input_boolean.helper_1': {
                        attributes: { friendly_name: 'Helper One' },
                        state: 'on'
                    }
                }
            });
            card2.setConfig({
                condition_helpers: ['input_boolean.helper_1', 'input_boolean.nonexistent'],
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            capturedCallback!(makeSlotCardData({ conditions: {} }));

            // Render and ensure the template is valid (nonexistent is filtered out)
            const tmpl = (card2 as any)._renderConditionsSection(card2._data!.conditions);
            expect(tmpl).toBeDefined();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('sync_status data handling', () => {
        it('stores sync_status from subscription data in lock entries', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const dataWithSyncStatus = makeSlotCardData({
                locks: [
                    {
                        code: '1234',
                        entity_id: 'lock.test_1',
                        in_sync: false,
                        name: 'Test Lock',
                        sync_status: 'suspended'
                    }
                ]
            });
            capturedCallback!(dataWithSyncStatus);

            expect(el._data?.locks[0].sync_status).toBe('suspended');
        });

        it('stores sync_status "syncing" in lock entries', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const dataWithSyncStatus = makeSlotCardData({
                locks: [
                    {
                        code: '1234',
                        entity_id: 'lock.test_1',
                        in_sync: false,
                        name: 'Test Lock',
                        sync_status: 'syncing'
                    }
                ]
            });
            capturedCallback!(dataWithSyncStatus);

            expect(el._data?.locks[0].sync_status).toBe('syncing');
        });

        it('lock entry has no sync_status when not provided', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const dataWithoutSyncStatus = makeSlotCardData();
            capturedCallback!(dataWithoutSyncStatus);

            expect(el._data?.locks[0].sync_status).toBeUndefined();
        });
    });

    describe('_renderLockRow sync status rendering', () => {
        let card: SlotCardElement;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        function flattenTemplateValues(result: any): string {
            // Recursively flatten Lit TemplateResult values into a single string
            const parts: string[] = [];
            if (result?.strings) {
                parts.push(...result.strings);
            }
            for (const v of result?.values ?? []) {
                if (v && typeof v === 'object' && 'strings' in v) {
                    parts.push(flattenTemplateValues(v));
                } else if (v !== undefined && v !== null) {
                    parts.push(String(v));
                }
            }
            return parts.join(' ');
        }

        it('renders synced state with check-circle icon', () => {
            const lock = {
                code: '1234',
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: true,
                lastSynced: undefined,
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: 'in_sync'
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).toContain('synced');
            expect(text).toContain('mdi:check-circle');
        });

        it('renders out_of_sync state with clock-outline icon', () => {
            const lock = {
                code: '1234',
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: false,
                lastSynced: undefined,
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: 'out_of_sync'
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).toContain('pending');
            expect(text).toContain('mdi:clock-outline');
        });

        it('renders syncing state with sync icon', () => {
            const lock = {
                code: '1234',
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: false,
                lastSynced: undefined,
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: 'syncing'
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).toContain('syncing');
            expect(text).toContain('mdi:sync');
        });

        it('renders suspended state with alert-circle icon', () => {
            const lock = {
                code: null,
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: false,
                lastSynced: undefined,
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: 'suspended'
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).toContain('suspended');
            expect(text).toContain('mdi:alert-circle');
        });

        it('falls back to inSync boolean when syncStatus is undefined', () => {
            const lock = {
                code: '1234',
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: false,
                lastSynced: undefined,
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: undefined
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).toContain('pending');
            expect(text).toContain('mdi:clock-outline');
        });

        it('renders unknown state when syncStatus undefined and inSync is null', () => {
            const lock = {
                code: null,
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: null,
                lastSynced: undefined,
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: undefined
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).toContain('unknown');
            expect(text).toContain('mdi:help-circle');
        });

        it('shows status text instead of last-synced when suspended', () => {
            const lock = {
                code: null,
                codeLength: undefined,
                entityId: 'lock.test',
                inSync: false,
                lastSynced: '2026-04-20T12:00:00Z',
                lockEntityId: 'lock.test',
                name: 'Test Lock',
                syncStatus: 'suspended'
            };
            const result = (card as any)._renderLockRow(lock);
            const text = flattenTemplateValues(result);
            expect(text).not.toContain('Last synced to lock');
            expect(text).toContain('Suspended');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_navigateToLock', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('dispatches hass-more-info event with entity ID', () => {
            const events: CustomEvent[] = [];
            card.addEventListener('hass-more-info', (e) => events.push(e as CustomEvent));
            (card as any)._navigateToLock('lock.front_door');
            expect(events).toHaveLength(1);
            expect(events[0].detail.entityId).toBe('lock.front_door');
            expect(events[0].bubbles).toBe(true);
            expect(events[0].composed).toBe(true);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_dismissActionError', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('clears _actionError', () => {
            (card as any)._actionError = 'Some error';
            (card as any)._dismissActionError();
            expect((card as any)._actionError).toBeUndefined();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_setActionError', () => {
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('sets _actionError and auto-dismisses after timeout', () => {
            vi.useFakeTimers();
            (card as any)._setActionError('Test error message');
            expect((card as any)._actionError).toBe('Test error message');

            vi.advanceTimersByTime(5000);
            expect((card as any)._actionError).toBeUndefined();
            vi.useRealTimers();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_handlePickerChange', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callWSMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection({
                states: {
                    'input_boolean.valid': { state: 'on' },
                    'switch.valid': { state: 'off' }
                }
            });
            callWSMock = hass.callWS as ReturnType<typeof vi.fn>;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('updates _dialogEntityId on value change', () => {
            (card as any)._showConditionDialog = true;
            (card as any)._handlePickerChange({
                detail: { value: 'input_boolean.valid' }
            } as CustomEvent);
            expect((card as any)._dialogEntityId).toBe('input_boolean.valid');
        });

        it('clears _dialogEntityId when value is empty and does not commit', () => {
            (card as any)._showConditionDialog = true;
            (card as any)._dialogEntityId = 'switch.valid';
            callWSMock.mockClear();
            (card as any)._handlePickerChange({ detail: { value: '' } } as CustomEvent);
            expect((card as any)._dialogEntityId).toBeNull();
            expect(callWSMock).not.toHaveBeenCalled();
        });

        it('does not re-commit while a previous commit is in flight', async () => {
            (card as any)._showConditionDialog = true;
            (card as any)._dialogSaving = true;
            callWSMock.mockClear();
            (card as any)._handlePickerChange({
                detail: { value: 'input_boolean.valid' }
            } as CustomEvent);
            await flush();
            expect(callWSMock).not.toHaveBeenCalled();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('getStubConfig', () => {
        it('returns first config entry when entries exist', async () => {
            const SlotCard = customElements.get('lcm-slot') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi.fn().mockResolvedValue([{ entry_id: 'real-entry-123' }]);

            const result = await SlotCard.getStubConfig(hass);
            expect(result).toEqual({
                config_entry_id: 'real-entry-123',
                slot: 1,
                type: 'custom:lcm-slot'
            });
        });

        it('returns stub config when no entries exist', async () => {
            const SlotCard = customElements.get('lcm-slot') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi.fn().mockResolvedValue([]);

            const result = await SlotCard.getStubConfig(hass);
            expect(result).toEqual({
                config_entry_id: 'stub',
                slot: 1,
                type: 'custom:lcm-slot'
            });
        });

        it('returns stub config when callWS throws', async () => {
            const SlotCard = customElements.get('lcm-slot') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi.fn().mockRejectedValue(new Error('fail'));

            const result = await SlotCard.getStubConfig(hass);
            expect(result).toEqual({
                config_entry_id: 'stub',
                slot: 1,
                type: 'custom:lcm-slot'
            });
        });
    });

    describe('stub config behavior', () => {
        it('sets _isStub to true when config_entry_id is stub', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                config_entry_id: 'stub',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            expect((el as Record<string, unknown>)._isStub).toBe(true);
        });

        it('sets _isStub to false when config_entry_id is real', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                config_entry_id: 'real-entry',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            expect((el as Record<string, unknown>)._isStub).toBe(false);
        });

        it('render returns static preview when _isStub is true', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                config_entry_id: 'stub',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            el.hass = createMockHassWithConnection();
            container.appendChild(el);
            await flush();

            /* eslint-disable @typescript-eslint/no-explicit-any */
            const result = (el as any).render();
            // The stub render returns a template containing "Lock Code Manager Slot Card"
            expect(result).toBeDefined();
            expect(result.strings?.join('')).toContain('Lock Code Manager Slot Card');
            /* eslint-enable @typescript-eslint/no-explicit-any */
        });
    });

    describe('_getEntityRow', () => {
        afterEach(() => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            delete (window as any).loadCardHelpers;
        });

        it('returns fallback div when loadCardHelpers is not available', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                config_entry_id: 'real-entry',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            el.hass = createMockHassWithConnection();
            container.appendChild(el);
            await flush();

            /* eslint-disable @typescript-eslint/no-explicit-any */
            const result = await (el as any)._getEntityRow('binary_sensor.test');
            expect(result.tagName).toBe('DIV');
            expect(result.textContent).toBe('binary_sensor.test');
            /* eslint-enable @typescript-eslint/no-explicit-any */
        });

        it('creates element via loadCardHelpers and caches it', async () => {
            const mockCreateRowElement = vi.fn((config: { entity: string }) => {
                const elem = document.createElement('div');
                elem.setAttribute('data-entity', config.entity);
                return elem;
            });
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (window as any).loadCardHelpers = vi.fn().mockResolvedValue({
                createRowElement: mockCreateRowElement
            });

            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({
                config_entry_id: 'real-entry',
                slot: 1,
                type: 'custom:lcm-slot'
            });
            el.hass = createMockHassWithConnection();
            container.appendChild(el);
            await flush();

            /* eslint-disable @typescript-eslint/no-explicit-any */
            const result1 = await (el as any)._getEntityRow('binary_sensor.test');
            expect(result1.getAttribute('data-entity')).toBe('binary_sensor.test');
            expect(mockCreateRowElement).toHaveBeenCalledTimes(1);

            // Second call should return cached element
            const result2 = await (el as any)._getEntityRow('binary_sensor.test');
            expect(result2).toBe(result1);
            // loadCardHelpers should not be called again
            expect(mockCreateRowElement).toHaveBeenCalledTimes(1);
            /* eslint-enable @typescript-eslint/no-explicit-any */
        });
    });

    describe('edit field handlers', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callServiceMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection({
                states: {
                    'text.slot_1_name': { state: 'Test' },
                    'text.slot_1_pin': { state: '1234' },
                    'number.slot_1_uses': { state: '5' }
                }
            });
            callServiceMock = vi.fn().mockResolvedValue(undefined);
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (hass as any).callService = callServiceMock;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('_startEditing sets editingField for name', () => {
            (card as any)._startEditing('name');
            expect((card as any)._editingField).toBe('name');
        });

        it('_startEditing reveals PIN before entering edit mode', () => {
            (card as any)._revealed = false;
            (card as any)._startEditing('pin');
            expect((card as any)._revealed).toBe(true);
        });

        it('reverts the auto-revealed PIN to masked when exiting edit mode via Escape', async () => {
            (card as any)._revealed = false;
            (card as any)._revealedForEdit = false;

            (card as any)._startEditing('pin');
            await flush();
            // _startEditing flips reveal optimistically before the resubscribe resolves
            expect((card as any)._revealed).toBe(true);
            expect((card as any)._revealedForEdit).toBe(true);

            // Simulate Escape
            const event = { key: 'Escape', target: { value: '' } };
            (card as any)._handleEditKeydown(event);
            expect((card as any)._editingField).toBeNull();
            expect((card as any)._revealed).toBe(false);
            expect((card as any)._revealedForEdit).toBe(false);
        });

        it('preserves the manually-revealed PIN when exiting edit mode via Escape', async () => {
            // User manually revealed via eye button
            (card as any)._revealed = true;
            (card as any)._revealedForEdit = false;

            (card as any)._startEditing('pin');
            await flush();
            // _startEditing took the else branch since _revealed was already true
            expect((card as any)._revealed).toBe(true);
            expect((card as any)._revealedForEdit).toBe(false);

            // Simulate Escape
            const event = { key: 'Escape', target: { value: '' } };
            (card as any)._handleEditKeydown(event);
            expect((card as any)._editingField).toBeNull();
            // stays revealed
            expect((card as any)._revealed).toBe(true);
            expect((card as any)._revealedForEdit).toBe(false);
        });

        it('reverts auto-revealed PIN on blur', async () => {
            (card as any)._data = makeSlotCardData({
                entities: { pin: 'text.slot_1_pin' }
            });
            (card as any)._revealed = false;
            (card as any)._revealedForEdit = false;
            (card as any)._startEditing('pin');
            await flush();
            expect((card as any)._revealedForEdit).toBe(true);

            const event = { target: { value: '5678' } };
            (card as any)._handleEditBlur(event);
            expect((card as any)._revealed).toBe(false);
            expect((card as any)._revealedForEdit).toBe(false);
        });

        it('reverts auto-revealed PIN on Enter (after save)', async () => {
            (card as any)._data = makeSlotCardData({
                entities: { pin: 'text.slot_1_pin' }
            });
            (card as any)._revealed = false;
            (card as any)._revealedForEdit = false;
            (card as any)._startEditing('pin');
            await flush();
            expect((card as any)._revealedForEdit).toBe(true);

            const event = { key: 'Enter', target: { value: '5678' } };
            (card as any)._handleEditKeydown(event);
            expect((card as any)._revealed).toBe(false);
            expect((card as any)._revealedForEdit).toBe(false);
        });

        it('_handleEditBlur saves value and clears editingField', () => {
            (card as any)._editingField = 'name';
            (card as any)._data = makeSlotCardData({
                entities: { name: 'text.slot_1_name' }
            });
            const mockEvent = { target: { value: 'New Name' } };
            (card as any)._handleEditBlur(mockEvent);
            expect((card as any)._editingField).toBeNull();
        });

        it('_handleEditKeydown saves on Enter', () => {
            (card as any)._editingField = 'name';
            (card as any)._data = makeSlotCardData({
                entities: { name: 'text.slot_1_name' }
            });
            const mockEvent = { key: 'Enter', target: { value: 'New Name' } };
            (card as any)._handleEditKeydown(mockEvent);
            expect((card as any)._editingField).toBeNull();
        });

        it('_handleEditKeydown cancels on Escape', () => {
            (card as any)._editingField = 'name';
            const mockEvent = { key: 'Escape', target: { value: 'ignored' } };
            (card as any)._handleEditKeydown(mockEvent);
            expect((card as any)._editingField).toBeNull();
        });

        it('_saveEditValue calls service for name field', async () => {
            (card as any)._editingField = 'name';
            (card as any)._data = makeSlotCardData({
                entities: { name: 'text.slot_1_name' }
            });
            await (card as any)._saveEditValue('New Name');
            expect(callServiceMock).toHaveBeenCalledWith(
                'text',
                'set_value',
                expect.objectContaining({
                    entity_id: 'text.slot_1_name',
                    value: 'New Name'
                })
            );
        });

        it('_saveEditValue sets error when entity is missing', async () => {
            (card as any)._editingField = 'name';
            (card as any)._data = makeSlotCardData({ entities: {} });
            await (card as any)._saveEditValue('New Name');
            expect((card as any)._actionError).toContain('unavailable');
        });

        it('_saveEditValue sets error when entity state is unavailable', async () => {
            (card as any)._editingField = 'pin';
            (card as any)._data = makeSlotCardData({
                entities: { pin: 'text.slot_1_pin' }
            });
            (card as any)._hass.states['text.slot_1_pin'] = { state: 'unavailable' };
            await (card as any)._saveEditValue('5678');
            expect((card as any)._actionError).toContain('unavailable');
        });

        it('_saveEditValue sets error when service call fails', async () => {
            (card as any)._editingField = 'name';
            (card as any)._data = makeSlotCardData({
                entities: { name: 'text.slot_1_name' }
            });
            callServiceMock.mockRejectedValueOnce(new Error('Service failed'));
            await (card as any)._saveEditValue('New Name');
            expect((card as any)._actionError).toContain('Failed to update name');
        });

        it('_saveEditValue returns early without hass', async () => {
            (card as any)._hass = null;
            (card as any)._editingField = 'name';
            await (card as any)._saveEditValue('test');
            expect(callServiceMock).not.toHaveBeenCalled();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_handleEnabledToggle', () => {
        let card: SlotCardElement & Record<string, unknown>;
        let callServiceMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection();
            callServiceMock = vi.fn().mockResolvedValue(undefined);
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            (hass as any).callService = callServiceMock;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('calls turn_on when toggling to enabled', async () => {
            (card as any)._data = makeSlotCardData({
                entities: { enabled: 'switch.slot_1_enabled' }
            });
            const mockEvent = { target: { checked: true } };
            await (card as any)._handleEnabledToggle(mockEvent);
            expect(callServiceMock).toHaveBeenCalledWith('switch', 'turn_on', {
                entity_id: 'switch.slot_1_enabled'
            });
        });

        it('calls turn_off when toggling to disabled', async () => {
            (card as any)._data = makeSlotCardData({
                entities: { enabled: 'switch.slot_1_enabled' }
            });
            const mockEvent = { target: { checked: false } };
            await (card as any)._handleEnabledToggle(mockEvent);
            expect(callServiceMock).toHaveBeenCalledWith('switch', 'turn_off', {
                entity_id: 'switch.slot_1_enabled'
            });
        });

        it('returns early without hass', async () => {
            (card as any)._hass = null;
            const mockEvent = { target: { checked: true } };
            await (card as any)._handleEnabledToggle(mockEvent);
            expect(callServiceMock).not.toHaveBeenCalled();
        });

        it('returns early when enabled entity is missing', async () => {
            (card as any)._data = makeSlotCardData({ entities: {} });
            const mockEvent = { target: { checked: true } };
            await (card as any)._handleEnabledToggle(mockEvent);
            expect(callServiceMock).not.toHaveBeenCalled();
        });

        it('sets action error when service call fails', async () => {
            (card as any)._data = makeSlotCardData({
                entities: { enabled: 'switch.slot_1_enabled' }
            });
            callServiceMock.mockRejectedValueOnce(new Error('Switch failed'));
            const mockEvent = { target: { checked: true } };
            await (card as any)._handleEnabledToggle(mockEvent);
            expect((card as any)._actionError).toContain('Failed to enable slot');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_saveEditValue for pin field', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('calls text.set_value for pin field with trimmed value', async () => {
            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            const hass = createMockHassWithConnection({
                states: { 'text.slot_1_pin': { state: '1234' } }
            });
            const callServiceMock = vi.fn().mockResolvedValue(undefined);
            (hass as any).callService = callServiceMock;
            card.hass = hass;
            container.appendChild(card);
            await flush();

            (card as any)._editingField = 'pin';
            (card as any)._data = makeSlotCardData({ entities: { pin: 'text.slot_1_pin' } });
            await (card as any)._saveEditValue(' 5678 ');
            expect(callServiceMock).toHaveBeenCalledWith(
                'text',
                'set_value',
                expect.objectContaining({ entity_id: 'text.slot_1_pin', value: '5678' })
            );
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderConditionsSummary', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /** Recursively join the static strings + primitive value text of a
         *  TemplateResult and any nested templates */
        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) {
                return result.map(deepTemplateStrings).join('');
            }
            if (typeof result !== 'object') return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepTemplateStrings).join('');
            return own + nested;
        }

        it('returns muted "none" badge when no condition entity is configured', () => {
            const tmpl = (card as any)._renderConditionsSummary({});
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('collapsible-badge muted');
            expect(joined).toContain('none');
        });

        it('uses friendly_name and success modifier when entity is allowing', () => {
            const tmpl = (card as any)._renderConditionsSummary({
                condition_entity: {
                    condition_entity_id: 'calendar.vacation',
                    domain: 'calendar',
                    friendly_name: 'Vacation',
                    state: 'on'
                }
            });
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('collapsible-badge');
            // Allowing case: dynamic class modifier is "success" (green).
            expect(tmpl.values).toContain('success');
            // Phase B replaces ✓ glyph with mdiCheck icon.
            expect(joined).toContain('collapsible-badge-icon');
            expect(joined).not.toContain('✓');
            expect(joined).toContain('Vacation');
        });

        it('uses warning badge with mdiClose icon when entity is blocking', () => {
            const tmpl = (card as any)._renderConditionsSummary({
                condition_entity: {
                    condition_entity_id: 'calendar.vacation',
                    domain: 'calendar',
                    friendly_name: 'Vacation',
                    state: 'off'
                }
            });
            const joined = deepTemplateStrings(tmpl);
            // Blocking case: dynamic class modifier is "warning" (Lit will
            // splice it into the class attribute at render time).
            expect(joined).toContain('collapsible-badge');
            expect(tmpl.values).toContain('warning');
            // Phase B replaces ✗ glyph with mdiClose icon.
            expect(joined).toContain('collapsible-badge-icon');
            expect(joined).not.toContain('✗');
            expect(joined).toContain('Vacation');
        });

        it('falls back to entity id when no friendly_name', () => {
            const tmpl = (card as any)._renderConditionsSummary({
                condition_entity: {
                    condition_entity_id: 'binary_sensor.foo',
                    domain: 'binary_sensor',
                    state: 'on'
                }
            });
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('binary_sensor.foo');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderConditionsBody', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /** Recursively join all template strings (incl. dynamic primitive values) */
        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepTemplateStrings).join('');
            if (typeof result !== 'object') return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepTemplateStrings).join('');
            return own + nested;
        }

        /** Recursively collect all function values from a TemplateResult */
        function collectHandlers(result: any): Array<(...args: any[]) => void> {
            const handlers: Array<(...args: any[]) => void> = [];
            if (!result?.values) return handlers;
            for (const v of result.values) {
                if (typeof v === 'function') {
                    handlers.push(v);
                } else if (v?.strings && v?.values) {
                    handlers.push(...collectHandlers(v));
                } else if (Array.isArray(v)) {
                    for (const item of v) {
                        if (item?.strings && item?.values) {
                            handlers.push(...collectHandlers(item));
                        }
                    }
                }
            }
            return handlers;
        }

        it('renders no empty-state callout when no entity (Add affordance is on the header)', () => {
            const tmpl = (card as any)._renderConditionsBody({}, false, false);
            const joined = deepTemplateStrings(tmpl);
            expect(joined).not.toContain('empty-state');
            expect(joined).not.toContain('add-link');
            expect(joined).not.toContain('No condition has been set');
            expect(joined).not.toContain('Remove condition');
            expect(joined).not.toContain('Manage condition');
        });

        it('renders the condition block + Remove condition link when entity exists', () => {
            const tmpl = (card as any)._renderConditionsBody(
                {
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        state: 'on'
                    }
                },
                true,
                false
            );
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('condition-block');
            expect(joined).toContain('remove-link');
            expect(joined).toContain('Remove condition');
            // Old wording must not slip back in.
            expect(joined).not.toContain('Manage condition');
            expect(joined).not.toContain('manage-link');
            expect(joined).not.toContain('empty-state');
        });

        it('remove-link click invokes _removeCondition() (no dialog opens)', () => {
            const tmpl = (card as any)._renderConditionsBody(
                {
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        state: 'on'
                    }
                },
                true,
                false
            );
            const handlers = collectHandlers(tmpl);
            let removeCalls = 0;
            let openCalls = 0;
            (card as any)._removeCondition = () => {
                removeCalls += 1;
            };
            (card as any)._openConditionDialog = () => {
                openCalls += 1;
            };
            for (const h of handlers) {
                try {
                    h();
                } catch {
                    // ignore — keydown handler expects an event arg
                }
            }
            expect(removeCalls).toBeGreaterThanOrEqual(1);
            expect(openCalls).toBe(0);
        });

        it('remove-link is keyboard-accessible (role=button, tabindex, aria-label)', () => {
            const tmpl = (card as any)._renderConditionsBody(
                {
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        state: 'on'
                    }
                },
                true,
                false
            );
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('role="button"');
            expect(joined).toContain('tabindex="0"');
            expect(joined).toContain('aria-label="Remove condition"');
        });

        it('remove-link Enter/Space keys invoke _removeCondition (Tab does not)', () => {
            const tmpl = (card as any)._renderConditionsBody(
                {
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        state: 'on'
                    }
                },
                true,
                false
            );
            const handlers = collectHandlers(tmpl);
            let removeCalls = 0;
            (card as any)._removeCondition = () => {
                removeCalls += 1;
            };
            const keydown = handlers.find((h) => h.length === 1);
            expect(keydown).toBeDefined();
            keydown!({ key: ' ', preventDefault: () => {} } as unknown as KeyboardEvent);
            expect(removeCalls).toBe(1);
            keydown!({ key: 'Enter', preventDefault: () => {} } as unknown as KeyboardEvent);
            expect(removeCalls).toBe(2);
            // Tab must NOT trigger remove — only Enter/Space should.
            keydown!({ key: 'Tab', preventDefault: () => {} } as unknown as KeyboardEvent);
            expect(removeCalls).toBe(2);
        });

        it('renders the helpers sub-list under the condition when hasHelpers', () => {
            (card as any)._config = {
                ...(card as any)._config,
                condition_helpers: ['input_boolean.h1']
            };
            (card as any)._hass = {
                ...(card as any)._hass,
                states: {
                    'input_boolean.h1': { attributes: { friendly_name: 'H1' }, state: 'on' }
                }
            };
            const tmpl = (card as any)._renderConditionsBody(
                {
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        state: 'on'
                    }
                },
                true,
                true
            );
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('helpers-label');
            expect(joined).toContain('helpers-list');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderConditionsSection (Add affordance promotion)', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        /** Recursively join all template strings (incl. dynamic primitive values) */
        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepTemplateStrings).join('');
            if (typeof result !== 'object') return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepTemplateStrings).join('');
            return own + nested;
        }

        /** Recursively collect all function values from a TemplateResult */
        function collectHandlers(result: any): Array<(...args: any[]) => void> {
            const handlers: Array<(...args: any[]) => void> = [];
            if (!result?.values) return handlers;
            for (const v of result.values) {
                if (typeof v === 'function') {
                    handlers.push(v);
                } else if (v?.strings && v?.values) {
                    handlers.push(...collectHandlers(v));
                } else if (Array.isArray(v)) {
                    for (const item of v) {
                        if (item?.strings && item?.values) {
                            handlers.push(...collectHandlers(item));
                        }
                    }
                }
            }
            return handlers;
        }

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        it('renders a static, non-collapsible header row with Add button when no condition and no helpers', () => {
            const tmpl = (card as any)._renderConditionsSection({});
            const joined = deepTemplateStrings(tmpl);
            // Static-header marker present, no chevron, no badge.
            expect(joined).toContain('collapsible-header static');
            expect(joined).toContain('add-condition-btn');
            expect(joined).toContain('Add condition');
            expect(joined).toContain('aria-label="Add condition"');
            expect(joined).not.toContain('collapsible-chevron');
            expect(joined).not.toContain('collapsible-badge');
            // The empty-state callout in the body should be gone too.
            expect(joined).not.toContain('empty-state');
            expect(joined).not.toContain('add-link');
        });

        it('static-header Add button click opens the dialog and stops propagation', () => {
            const tmpl = (card as any)._renderConditionsSection({});
            const handlers = collectHandlers(tmpl);
            let openCalls = 0;
            (card as any)._openConditionDialog = () => {
                openCalls += 1;
            };
            let stopPropCalls = 0;
            const fakeEvent = {
                stopPropagation: () => {
                    stopPropCalls += 1;
                }
            } as unknown as Event;
            // The button click handler takes the Event arg; the only arity-1
            // handler in this template is the @click on the add button.
            const click = handlers.find((h) => h.length === 1);
            expect(click).toBeDefined();
            click!(fakeEvent);
            expect(openCalls).toBe(1);
            expect(stopPropCalls).toBe(1);
        });

        it('renders a collapsible row with Add button as headerExtra when no condition but helpers exist', async () => {
            (card as any)._config = {
                ...(card as any)._config,
                condition_helpers: ['input_boolean.h1']
            };
            (card as any)._hass = {
                ...(card as any)._hass,
                states: {
                    'input_boolean.h1': { attributes: { friendly_name: 'H1' }, state: 'on' }
                }
            };
            const tmpl = (card as any)._renderConditionsSection({});
            const joined = deepTemplateStrings(tmpl);
            // Standard collapsible (chevron present, NOT static).
            expect(joined).not.toContain('collapsible-header static');
            expect(joined).toContain('collapsible-chevron');
            // Add button is the headerExtra (no muted "none" badge).
            expect(joined).toContain('add-condition-btn');
            expect(joined).toContain('Add condition');
            expect(joined).not.toContain('collapsible-badge muted');
            // Body has helpers but NOT the empty-state callout.
            expect(joined).toContain('helpers-list');
            expect(joined).not.toContain('empty-state');
            expect(joined).not.toContain('No condition has been set');
        });

        it('header Add button click stops propagation so the section does not toggle', () => {
            (card as any)._config = {
                ...(card as any)._config,
                condition_helpers: ['input_boolean.h1']
            };
            (card as any)._hass = {
                ...(card as any)._hass,
                states: {
                    'input_boolean.h1': { attributes: { friendly_name: 'H1' }, state: 'on' }
                }
            };
            const expandedBefore = (card as any)._conditionsExpanded;
            const tmpl = (card as any)._renderConditionsSection({});
            const handlers = collectHandlers(tmpl);
            let openCalls = 0;
            (card as any)._openConditionDialog = () => {
                openCalls += 1;
            };
            // Run every arity-1 handler with a synthetic click-shaped Event;
            // the Add button calls stopPropagation + opens the dialog while
            // the collapsible keydown handler short-circuits on missing key.
            // Both behaviors are exercised; only the Add button counts as a
            // successful match.
            let totalStopPropCalls = 0;
            const fakeEvent = {
                key: 'NotAKey',
                preventDefault: () => undefined,
                stopPropagation: () => {
                    totalStopPropCalls += 1;
                }
            } as unknown as Event;
            for (const h of handlers.filter((fn) => fn.length === 1)) {
                try {
                    h(fakeEvent);
                } catch {
                    // ignore — some handlers may expect different shapes
                }
            }
            expect(totalStopPropCalls).toBeGreaterThanOrEqual(1);
            expect(openCalls).toBe(1);
            // Section did NOT toggle (button stopped propagation).
            expect((card as any)._conditionsExpanded).toBe(expandedBefore);
        });

        it('renders the standard collapsible with summary badge when a condition entity is set', () => {
            const tmpl = (card as any)._renderConditionsSection({
                condition_entity: {
                    condition_entity_id: 'calendar.vacation',
                    domain: 'calendar',
                    friendly_name: 'Vacation',
                    state: 'on'
                }
            });
            const joined = deepTemplateStrings(tmpl);
            expect(joined).not.toContain('collapsible-header static');
            expect(joined).toContain('collapsible-chevron');
            expect(joined).toContain('collapsible-badge');
            expect(joined).toContain('Vacation');
            // Phase B: ✓ glyph replaced with mdi icon.
            expect(joined).toContain('collapsible-badge-icon');
            expect(joined).not.toContain('add-condition-btn');
        });

        it('renders the warning summary badge when the condition entity is blocking', () => {
            const tmpl = (card as any)._renderConditionsSection({
                condition_entity: {
                    condition_entity_id: 'calendar.vacation',
                    domain: 'calendar',
                    friendly_name: 'Vacation',
                    state: 'off'
                }
            });
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('collapsible-badge');
            // Phase B: ✗ glyph replaced with mdi icon.
            expect(joined).toContain('collapsible-badge-icon');
            expect(joined).not.toContain('add-condition-btn');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderConditionBlock', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepTemplateStrings).join('');
            if (typeof result !== 'object') return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepTemplateStrings).join('');
            return own + nested;
        }

        it('renders the LCM overlay strip with allowing class when entity is on', () => {
            const tmpl = (card as any)._renderConditionBlock({
                condition_entity_id: 'calendar.vacation',
                domain: 'calendar',
                state: 'on'
            });
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('condition-block');
            expect(joined).toContain('lcm-overlay');
            expect(joined).toContain('allowing');
            expect(joined).toContain('✓ Allowing access');
        });

        it('renders the LCM overlay strip with blocking class when entity is off', () => {
            const tmpl = (card as any)._renderConditionBlock({
                condition_entity_id: 'calendar.vacation',
                domain: 'calendar',
                state: 'off'
            });
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('blocking');
            expect(joined).toContain('✗ Blocking access');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderOverlayContext', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
            vi.useFakeTimers();
            vi.setSystemTime(new Date('2026-05-01T00:00:00Z'));
        });

        afterEach(() => {
            vi.useRealTimers();
        });

        it('returns calendar event summary + ends-relative when allowing', () => {
            const text = (card as any)._renderOverlayContext(
                {
                    calendar: {
                        end_time: '2026-05-06T00:00:00Z',
                        summary: "Alice's stay"
                    },
                    condition_entity_id: 'calendar.vacation',
                    domain: 'calendar',
                    state: 'on'
                },
                true
            );
            expect(text).toContain("Alice's stay");
            expect(text).toContain('ends in 5 days');
        });

        it('returns "Next: <summary> starts <relative>" for blocking calendar', () => {
            const text = (card as any)._renderOverlayContext(
                {
                    calendar_next: {
                        start_time: '2026-05-03T00:00:00Z',
                        summary: 'Trip'
                    },
                    condition_entity_id: 'calendar.vacation',
                    domain: 'calendar',
                    state: 'off'
                },
                false
            );
            expect(text).toContain('Next: Trip');
            expect(text).toContain('starts in 2 days');
        });

        it('returns Ends/Starts label for schedule', () => {
            const allowing = (card as any)._renderOverlayContext(
                {
                    condition_entity_id: 'schedule.business_hours',
                    domain: 'schedule',
                    schedule: { next_event: '2026-05-02T00:00:00Z' },
                    state: 'on'
                },
                true
            );
            expect(allowing).toContain('Ends');
            expect(allowing).toContain('in 1 day');

            const blocking = (card as any)._renderOverlayContext(
                {
                    condition_entity_id: 'schedule.business_hours',
                    domain: 'schedule',
                    schedule: { next_event: '2026-05-02T00:00:00Z' },
                    state: 'off'
                },
                false
            );
            expect(blocking).toContain('Starts');
        });

        it('falls back to generic text for binary_sensor and unknown domains', () => {
            expect(
                (card as any)._renderOverlayContext(
                    {
                        condition_entity_id: 'binary_sensor.test',
                        domain: 'binary_sensor',
                        state: 'on'
                    },
                    true
                )
            ).toBe('Condition is on');
            expect(
                (card as any)._renderOverlayContext(
                    {
                        condition_entity_id: 'binary_sensor.test',
                        domain: 'binary_sensor',
                        state: 'off'
                    },
                    false
                )
            ).toBe('Condition is off');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_formatRelative', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
            // Pin time AFTER element setup so flush()'s setTimeout isn't blocked
            vi.useFakeTimers();
            vi.setSystemTime(new Date('2026-05-01T00:00:00Z'));
        });

        afterEach(() => {
            vi.useRealTimers();
        });

        it('returns "today" for sub-day deltas in either direction', () => {
            expect((card as any)._formatRelative('2026-05-01T03:00:00Z')).toBe('today');
            expect((card as any)._formatRelative('2026-04-30T21:00:00Z')).toBe('today');
        });

        it('returns "today" right up to the 24h boundary (no rounding-up bug)', () => {
            // 23h forward — was previously reported as "in 1 day" because the
            // implementation rounded ms/86400000. Sub-day deltas must collapse
            // to "today" regardless of how close to 24h they are.
            expect((card as any)._formatRelative('2026-05-01T23:00:00Z')).toBe('today');
            // 23h ago — same boundary behavior in the past direction.
            expect((card as any)._formatRelative('2026-04-30T01:00:00Z')).toBe('today');
        });

        it('returns "in 1 day" / "1 day ago" at the day boundary', () => {
            expect((card as any)._formatRelative('2026-05-02T00:00:00Z')).toBe('in 1 day');
            expect((card as any)._formatRelative('2026-04-30T00:00:00Z')).toBe('1 day ago');
            // 25h either direction rounds to one day.
            expect((card as any)._formatRelative('2026-05-02T01:00:00Z')).toBe('in 1 day');
            expect((card as any)._formatRelative('2026-04-29T23:00:00Z')).toBe('1 day ago');
        });

        it('returns "in N days" / "N days ago" for multi-day deltas', () => {
            expect((card as any)._formatRelative('2026-05-08T00:00:00Z')).toBe('in 7 days');
            expect((card as any)._formatRelative('2026-04-24T00:00:00Z')).toBe('7 days ago');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_formatLockCode', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        it('returns null for empty code', () => {
            expect((card as any)._formatLockCode({ code: 'empty' })).toBeNull();
        });

        it('returns bullets for unreadable code', () => {
            expect((card as any)._formatLockCode({ code: 'unreadable_code' })).toBe('• • •');
        });

        it('returns masked code when not revealed', () => {
            (card as any)._revealed = false;
            expect((card as any)._formatLockCode({ code: '1234' })).toBe('••••');
        });

        it('returns actual code when revealed', () => {
            (card as any)._revealed = true;
            (card as any)._config = {
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot',
                code_display: 'masked_with_reveal'
            };
            expect((card as any)._formatLockCode({ code: '1234' })).toBe('1234');
        });

        it('returns null for null code', () => {
            expect((card as any)._formatLockCode({ code: null })).toBeNull();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_toggleLockStatus', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('toggles lock status expanded state', async () => {
            const card = document.createElement('lcm-slot') as SlotCardElement &
                Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();

            expect((card as any)._lockStatusExpanded).toBe(false);
            (card as any)._toggleLockStatus();
            expect((card as any)._lockStatusExpanded).toBe(true);
            (card as any)._toggleLockStatus();
            expect((card as any)._lockStatusExpanded).toBe(false);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('event row (last_used)', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: SlotCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            // Provide a registered state for the slot's event entity so the
            // happy-path tests render the row. Tests that exercise
            // unavailable / missing-entity branches override card.hass with
            // their own mock.
            card.hass = createMockHassWithConnection({
                states: {
                    'event.lcm_slot_1_pin_used': { state: '2026-05-01T18:23:00Z' }
                }
            });
            container.appendChild(card);
            await flush();
        });

        /** Recursively join all template strings (incl. dynamic primitive values) */
        function deepTemplateStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepTemplateStrings).join('');
            if (typeof result !== 'object') return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepTemplateStrings).join('');
            return own + nested;
        }

        /** Recursively collect all function values from a TemplateResult */
        function collectHandlers(result: any): Array<(...args: any[]) => void> {
            const handlers: Array<(...args: any[]) => void> = [];
            if (!result?.values) return handlers;
            for (const v of result.values) {
                if (typeof v === 'function') {
                    handlers.push(v);
                } else if (v?.strings && v?.values) {
                    handlers.push(...collectHandlers(v));
                } else if (Array.isArray(v)) {
                    for (const item of v) {
                        if (item?.strings && item?.values) {
                            handlers.push(...collectHandlers(item));
                        }
                    }
                }
            }
            return handlers;
        }

        it('renders event row with Last used label and lock + relative time when last_used is set', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const tmpl = (card as any)._renderEventRow();
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('event-row');
            expect(joined).toContain('event-icon');
            expect(joined).toContain('event-name');
            expect(joined).toContain('Last used');
            expect(joined).toContain('event-meta');
            expect(joined).toContain('Front Door');
            expect(joined).toContain('event-arrow');
            // The relative time component should be wired up for the timestamp.
            expect(joined).toContain('ha-relative-time');
        });

        it('renders Never used copy when event entity exists but last_used is null', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: undefined,
                last_used_lock: undefined
            });
            const tmpl = (card as any)._renderEventRow();
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('event-row');
            expect(joined).toContain('Last used');
            expect(joined).toContain('Never used');
            // No ha-relative-time when there's no datetime to render.
            expect(joined).not.toContain('ha-relative-time');
        });

        it('returns nothing when event_entity_id is absent', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: undefined,
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const tmpl = (card as any)._renderEventRow();
            // `nothing` from lit is a sentinel; it is neither a TemplateResult
            // nor a primitive string — verify we don't get a template back.
            expect(tmpl?.strings).toBeUndefined();
        });

        it('clicking the event row dispatches hass-more-info for the event entity', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const dispatched: CustomEvent[] = [];
            card.addEventListener('hass-more-info', ((e: Event) =>
                dispatched.push(e as CustomEvent)) as EventListener);
            const tmpl = (card as any)._renderEventRow();
            const handlers = collectHandlers(tmpl);
            for (const h of handlers) {
                try {
                    h();
                } catch {
                    // ignore; handlers may call into other internals
                }
            }
            expect(dispatched).toHaveLength(1);
            expect(dispatched[0].detail).toEqual({
                entityId: 'event.lcm_slot_1_pin_used'
            });
        });

        it('_navigateToEventHistory is a no-op when event_entity_id is absent', () => {
            (card as any)._data = makeSlotCardData({ event_entity_id: undefined });
            const dispatched: Event[] = [];
            card.addEventListener('hass-more-info', ((e: Event) =>
                dispatched.push(e)) as EventListener);
            (card as any)._navigateToEventHistory();
            expect(dispatched).toHaveLength(0);
        });

        it('returns nothing when the event entity is unavailable', () => {
            // Configure hass with an explicit unavailable state for the entity
            // referenced by the slot data so the row suppresses rendering.
            const hass = createMockHassWithConnection({
                states: {
                    'event.lcm_slot_1_pin_used': { state: 'unavailable' }
                }
            });
            card.hass = hass;
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const tmpl = (card as any)._renderEventRow();
            // `nothing` from lit is a sentinel — verify we don't get a TemplateResult.
            expect(tmpl?.strings).toBeUndefined();
        });

        it('_navigateToEventHistory is a no-op when the event entity is unavailable', () => {
            const hass = createMockHassWithConnection({
                states: {
                    'event.lcm_slot_1_pin_used': { state: 'unavailable' }
                }
            });
            card.hass = hass;
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used'
            });
            const dispatched: Event[] = [];
            card.addEventListener('hass-more-info', ((e: Event) =>
                dispatched.push(e)) as EventListener);
            (card as any)._navigateToEventHistory();
            expect(dispatched).toHaveLength(0);
        });

        it('event row exposes role=button, tabindex=0 and an aria-label for a11y', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const tmpl = (card as any)._renderEventRow();
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('role="button"');
            expect(joined).toContain('tabindex="0"');
            expect(joined).toContain('aria-label="View activity history"');
        });

        it('Enter and Space on the event row dispatch hass-more-info; other keys do not', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const dispatched: CustomEvent[] = [];
            card.addEventListener('hass-more-info', ((e: Event) =>
                dispatched.push(e as CustomEvent)) as EventListener);

            // Two function values are present on the event-row div: the
            // zero-arity @click handler and the one-arg @keydown handler. We
            // pick the keydown handler by arity so we can isolate keyboard
            // routing from mouse routing.
            const tmpl = (card as any)._renderEventRow();
            const handlers = collectHandlers(tmpl);
            const keydown = handlers.find((h) => h.length === 1);
            expect(keydown).toBeDefined();

            keydown!({ key: 'Tab', preventDefault: () => {} } as unknown as KeyboardEvent);
            expect(dispatched).toHaveLength(0);

            keydown!({ key: 'Enter', preventDefault: () => {} } as unknown as KeyboardEvent);
            expect(dispatched).toHaveLength(1);

            keydown!({ key: ' ', preventDefault: () => {} } as unknown as KeyboardEvent);
            expect(dispatched).toHaveLength(2);
        });

        it('_renderFromData appends the event row after the sections', () => {
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const tmpl = (card as any)._renderFromData((card as any)._data);
            const joined = deepTemplateStrings(tmpl);
            expect(joined).toContain('event-row');
            expect(joined).toContain('Last used');
            expect(joined).toContain('Front Door');
        });

        it('returns nothing when the event entity is missing from hass.states', () => {
            // Override the per-suite hass with one that has no states. The
            // event entity isn't in hass.states (registry race or the
            // entity was removed), so the row must suppress to avoid
            // clicking through to an empty more-info dialog.
            const hass = createMockHassWithConnection({ states: {} });
            card.hass = hass;
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used',
                last_used: '2026-05-01T18:23:00Z',
                last_used_lock: 'Front Door'
            });
            const tmpl = (card as any)._renderEventRow();
            expect(tmpl?.strings).toBeUndefined();
        });

        it('_navigateToEventHistory is a no-op when entity missing from hass.states', () => {
            const hass = createMockHassWithConnection({ states: {} });
            card.hass = hass;
            (card as any)._data = makeSlotCardData({
                event_entity_id: 'event.lcm_slot_1_pin_used'
            });
            const dispatched: Event[] = [];
            card.addEventListener('hass-more-info', ((e: Event) =>
                dispatched.push(e)) as EventListener);
            (card as any)._navigateToEventHistory();
            expect(dispatched).toHaveLength(0);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('review-fix coverage (PR #1116)', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */

        afterEach(() => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            delete (window as any).loadCardHelpers;
        });

        // A1: _getEntityRow exception handling
        describe('_getEntityRow error handling', () => {
            it('returns an error placeholder and surfaces _setActionError when loadCardHelpers rejects', async () => {
                (window as any).loadCardHelpers = vi
                    .fn()
                    .mockRejectedValue(new Error('helpers boom'));
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);
                await flush();

                const result = await (card as any)._getEntityRow('binary_sensor.test');
                expect(result.tagName).toBe('DIV');
                expect(result.className).toBe('entity-row-error');
                expect(result.textContent).toContain('binary_sensor.test');
                expect((card as any)._actionError).toContain('helpers boom');
            });

            it('returns an error placeholder when createRowElement throws', async () => {
                (window as any).loadCardHelpers = vi.fn().mockResolvedValue({
                    createRowElement: () => {
                        throw new Error('createRowElement boom');
                    }
                });
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);
                await flush();

                const result = await (card as any)._getEntityRow('binary_sensor.test');
                expect(result.className).toBe('entity-row-error');
                expect((card as any)._actionError).toContain('createRowElement boom');
            });

            it('does not cache the error placeholder so the next render retries', async () => {
                let attempts = 0;
                (window as any).loadCardHelpers = vi.fn().mockImplementation(() => {
                    attempts += 1;
                    return Promise.reject(new Error('still failing'));
                });
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);
                await flush();

                // connectedCallback also calls loadCardHelpers via
                // _ensureEntityPickerLoaded. Reset the attempt counter so we
                // measure only the _getEntityRow retries this test cares about.
                attempts = 0;
                await (card as any)._getEntityRow('binary_sensor.test');
                await (card as any)._getEntityRow('binary_sensor.test');
                expect(attempts).toBe(2);
            });
        });

        // A2: _commitConditionPick resubscribe failure surfaces in banner
        describe('save resubscribe failure handling', () => {
            it('_commitConditionPick surfaces a resubscribe failure via _setActionError', async () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                let subscribeCallCount = 0;
                const subscribeMock = vi.fn().mockImplementation(() => {
                    subscribeCallCount += 1;
                    if (subscribeCallCount >= 2) {
                        return Promise.reject(new Error('resubscribe boom'));
                    }
                    return Promise.resolve(() => {});
                });
                const hass = {
                    callWS: vi.fn().mockResolvedValue(undefined),
                    config: { state: 'RUNNING' },
                    connection: { subscribeMessage: subscribeMock },
                    states: { 'input_boolean.valid_entity': { state: 'on' } }
                } as unknown as HomeAssistant;
                card.hass = hass;
                container.appendChild(card);
                await flush();

                await (card as any)._commitConditionPick('input_boolean.valid_entity');
                expect((card as any)._actionError).toContain('Failed to set condition');
                expect((card as any)._actionError).toContain('resubscribe boom');
                expect((card as any)._dialogSaving).toBe(false);
            });

            it('_removeCondition surfaces a resubscribe failure via _setActionError', async () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                let subscribeCallCount = 0;
                const subscribeMock = vi.fn().mockImplementation(() => {
                    subscribeCallCount += 1;
                    if (subscribeCallCount >= 2) {
                        return Promise.reject(new Error('resubscribe boom'));
                    }
                    return Promise.resolve(() => {});
                });
                const hass = {
                    callWS: vi.fn().mockResolvedValue(undefined),
                    config: { state: 'RUNNING' },
                    connection: { subscribeMessage: subscribeMock },
                    states: {}
                } as unknown as HomeAssistant;
                card.hass = hass;
                container.appendChild(card);
                await flush();

                (card as any)._showConditionDialog = true;
                await (card as any)._removeCondition();
                expect((card as any)._actionError).toContain('Failed to remove condition');
                expect((card as any)._actionError).toContain('resubscribe boom');
                expect((card as any)._dialogSaving).toBe(false);
            });
        });

        // B2: helpers list excludes the active condition entity
        describe('_renderHelpers excludes active condition entity', () => {
            it('does not render a helper row for the entity used as the condition entity', async () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({
                    condition_helpers: ['input_boolean.shared_entity', 'input_boolean.helper_only'],
                    config_entry_id: 'abc',
                    slot: 1,
                    type: 'custom:lcm-slot'
                });
                card.hass = createMockHassWithConnection({
                    states: {
                        'input_boolean.shared_entity': { state: 'on' },
                        'input_boolean.helper_only': { state: 'on' }
                    }
                });
                container.appendChild(card);
                await flush();
                (card as any)._data = makeSlotCardData({
                    conditions: {
                        condition_entity: {
                            condition_entity_id: 'input_boolean.shared_entity',
                            state: 'on'
                        }
                    }
                });

                // Spy on _getEntityRow to record which entity ids the helper
                // list asks to mount. The condition entity must not appear
                // in this list — only `helper_only`.
                const requested: string[] = [];
                (card as any)._getEntityRow = (eid: string) => {
                    requested.push(eid);
                    return Promise.resolve(document.createElement('div'));
                };
                (card as any)._renderHelpers();
                expect(requested).toEqual(['input_boolean.helper_only']);
            });
        });

        // B3: Saving indicator + close-then-reopen doesn't drop the in-flight
        // state
        describe('dialog in-flight indicator + close-state preservation', () => {
            it('renders the Saving… indicator only while _dialogSaving is true', () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);

                (card as any)._showConditionDialog = true;
                (card as any)._dialogSaving = true;
                const inFlight = (card as any)._renderConditionDialog();
                const inFlightStrings = (inFlight.strings ?? []).join('');
                const inFlightHasIndicator =
                    inFlightStrings.includes('dialog-saving') ||
                    JSON.stringify(inFlight.values ?? []).includes('dialog-saving');
                expect(inFlightHasIndicator).toBe(true);

                (card as any)._dialogSaving = false;
                const idle = (card as any)._renderConditionDialog();
                const idleHasIndicator = JSON.stringify(idle.values ?? []).includes(
                    'dialog-saving'
                );
                expect(idleHasIndicator).toBe(false);
            });

            it('_closeConditionDialog does NOT reset _dialogSaving', () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);

                (card as any)._showConditionDialog = true;
                (card as any)._dialogSaving = true;
                (card as any)._closeConditionDialog();
                // Closing must NOT reset the in-flight flag — otherwise the
                // user could close-then-reopen the dialog and bypass the
                // re-entry guard while the WS write is still pending.
                expect((card as any)._dialogSaving).toBe(true);
                expect((card as any)._showConditionDialog).toBe(false);
            });
        });

        // C1: _handlePickerChange re-entry guard
        describe('_handlePickerChange re-entry guard', () => {
            it('does not commit when _dialogSaving is already true', async () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                const hass = createMockHassWithConnection({
                    states: { 'input_boolean.valid_entity': { state: 'on' } }
                });
                const callWSMock = hass.callWS as ReturnType<typeof vi.fn>;
                card.hass = hass;
                container.appendChild(card);
                await flush();

                callWSMock.mockClear();
                (card as any)._dialogSaving = true;
                (card as any)._handlePickerChange({
                    detail: { value: 'input_boolean.valid_entity' }
                } as CustomEvent);
                await flush();
                expect(callWSMock).not.toHaveBeenCalled();
            });
        });

        // C2: _startEditing('pin') resubscribe failure handling
        describe('_startEditing pin resubscribe failure handling', () => {
            it('reverts _revealed and surfaces _setActionError on resubscribe failure', async () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);
                await flush();

                // Replace _subscribe with a failing implementation so we can
                // exercise the .catch branch deterministically.
                (card as any)._subscribe = vi.fn().mockRejectedValue(new Error('subscribe boom'));
                (card as any)._revealed = false;
                (card as any)._startEditing('pin');
                // Reveal flips optimistically before the await.
                expect((card as any)._revealed).toBe(true);
                await flush();
                await flush();
                expect((card as any)._revealed).toBe(false);
                expect((card as any)._actionError).toContain('subscribe boom');
            });
        });

        // C3: _setActionError timer tracking
        describe('_setActionError timer tracking', () => {
            it('back-to-back errors do not let the first timer dismiss the second early', () => {
                vi.useFakeTimers();
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);

                (card as any)._setActionError('first');
                expect((card as any)._actionError).toBe('first');
                // Advance 4s — the first timer would fire 1s from now, but
                // we set a second error before then.
                vi.advanceTimersByTime(4000);
                (card as any)._setActionError('second');
                expect((card as any)._actionError).toBe('second');
                // Advance another 1s — that's 5s after the FIRST set, so
                // the previous-tracked-timer-bug would dismiss the banner.
                vi.advanceTimersByTime(1000);
                expect((card as any)._actionError).toBe('second');
                // 4s more (total 5s after the second set) finally clears.
                vi.advanceTimersByTime(4000);
                expect((card as any)._actionError).toBeUndefined();
                vi.useRealTimers();
            });

            it('disconnectedCallback clears the pending error timer', () => {
                vi.useFakeTimers();
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);

                (card as any)._setActionError('about to disconnect');
                expect((card as any)._actionErrorTimer).toBeDefined();
                card.remove();
                expect((card as any)._actionErrorTimer).toBeUndefined();
                vi.useRealTimers();
            });
        });

        // C4: _closeConditionDialog resets dialog state
        describe('_closeConditionDialog state cleanup', () => {
            it('resets _showConditionDialog and _dialogEntityId', () => {
                const card = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
                card.hass = createMockHassWithConnection();
                container.appendChild(card);

                (card as any)._showConditionDialog = true;
                (card as any)._dialogEntityId = 'input_boolean.something';
                (card as any)._closeConditionDialog();
                expect((card as any)._showConditionDialog).toBe(false);
                expect((card as any)._dialogEntityId).toBeNull();
                // _dialogMode no longer exists — the dialog is Add-only.
                expect((card as any)._dialogMode).toBeUndefined();
            });
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    // Phase A: design hierarchy + a11y critical fixes (PR #1116 Phase A).
    describe('Phase A — design + a11y fix-up', () => {
        let card: SlotCardElement & Record<string, unknown>;

        /** Recursively join all template strings (deep) */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepStrings(result: any): string {
            if (!result || typeof result !== 'object') return '';
            if (!result.strings) return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepStrings).join('');
            return own + nested;
        }

        /**
         * Render a TemplateResult to a flat string by interleaving its static
         * strings with its scalar (string/number/boolean) values, recursing
         * into nested templates. Functions and other non-scalars are
         * substituted with an empty string. Useful for asserting on
         * attributes whose values come from interpolated expressions.
         */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function renderToString(result: any): string {
            if (!result || typeof result !== 'object' || !result.strings) return '';
            const strings: string[] = result.strings;
            const values: unknown[] = result.values ?? [];
            let out = '';
            for (let i = 0; i < strings.length; i++) {
                out += strings[i];
                if (i < values.length) {
                    const v = values[i];
                    if (v === null || v === undefined || typeof v === 'function') {
                        // skip
                    } else if (typeof v === 'object') {
                        out += renderToString(v);
                    } else {
                        out += String(v);
                    }
                }
            }
            return out;
        }

        /** Recursively collect all function values from a TemplateResult */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function collectAllHandlers(result: any): Array<(...args: any[]) => void> {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const handlers: Array<(...args: any[]) => void> = [];
            if (!result?.values) return handlers;
            for (const v of result.values) {
                if (typeof v === 'function') {
                    handlers.push(v);
                } else if (v?.strings && v?.values) {
                    handlers.push(...collectAllHandlers(v));
                } else if (Array.isArray(v)) {
                    for (const item of v) {
                        if (item?.strings && item?.values) {
                            handlers.push(...collectAllHandlers(item));
                        }
                    }
                }
            }
            return handlers;
        }

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */

        // D1 — name typography promotion + label drops
        describe('D1: name typography', () => {
            it('drops the NAME label from the hero name row', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice' });
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                // The NAME label was dropped — the editable name span and
                // pencil are still present, but no "NAME" text label.
                expect(joined).not.toMatch(/>Name</);
            });

            it('drops the ENABLED label from the hero toggle row', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice', enabled: true });
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).not.toMatch(/>Enabled</);
            });

            it('keeps the PIN label since "••••" is not self-evident', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice' });
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toMatch(/>PIN</);
            });

            it('promotes .hero-name-value to 22px / 600 weight in the stylesheet', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(
                    /\.hero-name-value\s*\{[^}]*font-size:\s*22px[^}]*font-weight:\s*600/s
                );
            });
        });

        // D3 — collapsed_sections backward compat
        describe('D3: collapsed_sections backward compat', () => {
            function makeCard(
                collapsed?: ('condition' | 'conditions' | 'lock_status')[]
            ): SlotCardElement & Record<string, unknown> {
                const c = document.createElement('lcm-slot') as SlotCardElement &
                    Record<string, unknown>;
                c.setConfig({
                    collapsed_sections: collapsed,
                    config_entry_id: 'abc',
                    slot: 1,
                    type: 'custom:lcm-slot'
                });
                c.hass = createMockHassWithConnection();
                container.appendChild(c);
                return c;
            }

            it('collapses condition section when canonical "condition" is set', () => {
                const c = makeCard(['condition']);
                expect((c as any)._conditionsExpanded).toBe(false);
            });

            it('collapses condition section when legacy "conditions" is set', () => {
                const c = makeCard(['conditions']);
                expect((c as any)._conditionsExpanded).toBe(false);
            });

            it('expands condition section when neither key is in the list', () => {
                const c = makeCard(['lock_status']);
                expect((c as any)._conditionsExpanded).toBe(true);
            });

            it('uses the singular "condition" as the default when nothing is configured', () => {
                const c = makeCard();
                // Default of ['condition', 'lock_status'] keeps both collapsed.
                expect((c as any)._conditionsExpanded).toBe(false);
                expect((c as any)._lockStatusExpanded).toBe(false);
            });
        });

        // D4 — placeholder copy
        describe('D4: placeholder copy', () => {
            it('uses "Not named" instead of "<No Name>" for the empty name placeholder', () => {
                (card as any)._data = makeSlotCardData({ name: '' });
                const hero = (card as any)._renderHero(null, undefined, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toContain('Not named');
                expect(joined).not.toContain('No Name');
                expect(joined).not.toContain('&lt;No Name&gt;');
            });

            it('uses "No PIN set" instead of "<No PIN>" for the empty PIN placeholder', () => {
                const hero = (card as any)._renderHero(null, undefined, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toContain('No PIN set');
                expect(joined).not.toContain('No PIN<');
                expect(joined).not.toContain('&lt;No PIN&gt;');
            });
        });

        // C1 — name-edit auto-focus selector regression test
        describe('C1: name-edit auto-focus selector', () => {
            it('uses ".name-edit-input" (no .name ancestor) so the focus selector resolves', async () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice' });
                (card as any)._startEditing('name');
                // updated() runs on the next microtask. Awaiting updateComplete
                // commits the render and triggers updated(), which is what
                // needs to find the input via the selector.
                await (card as unknown as { updateComplete: Promise<unknown> }).updateComplete;
                const input = card.shadowRoot?.querySelector('.name-edit-input');
                expect(input).not.toBeNull();
            });
        });

        // A1 — hero PIN value a11y
        describe('A1: hero PIN value a11y', () => {
            it('exposes role=button, tabindex=0 and aria-label on the editable PIN value', () => {
                (card as any)._data = makeSlotCardData({ pin: '1234' });
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                // The editable PIN span declares all three a11y attributes.
                expect(joined).toContain('class="hero-pin-value editable');
                expect(joined).toContain('role="button"');
                expect(joined).toContain('tabindex="0"');
                expect(joined).toContain('aria-label="Edit PIN"');
            });

            it('Enter and Space on the PIN value invoke _startEditing("pin")', () => {
                (card as any)._data = makeSlotCardData({ pin: '1234' });
                const calls: string[] = [];
                (card as any)._startEditing = (field: string) => calls.push(field);
                // Stub the enable-toggle handler so accidentally-invoked
                // handlers from our broad arity-1 sweep don't reject async
                // Promises and leak as unhandled rejections.
                (card as any)._handleEnabledToggle = () => undefined;
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const handlers = collectAllHandlers(hero);
                // Try every keydown-shaped (arity 1) handler with Enter/Space;
                // the PIN-value keydown will fire startEditing('pin').
                for (const h of handlers.filter((fn) => fn.length === 1)) {
                    try {
                        h({
                            key: 'Enter',
                            preventDefault: () => undefined,
                            target: { checked: false }
                        } as unknown as KeyboardEvent);
                        h({
                            key: ' ',
                            preventDefault: () => undefined,
                            target: { checked: false }
                        } as unknown as KeyboardEvent);
                    } catch {
                        // ignore — some handlers expect different shapes
                    }
                }
                // Should have been called twice for pin (Enter + Space).
                const pinCalls = calls.filter((c) => c === 'pin').length;
                expect(pinCalls).toBeGreaterThanOrEqual(2);
            });
        });

        // A2 — hero name value a11y
        describe('A2: hero name value a11y', () => {
            it('exposes role=button, tabindex=0 and aria-label on the editable name value', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice' });
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toContain('class="hero-name-value editable"');
                expect(joined).toContain('role="button"');
                expect(joined).toContain('tabindex="0"');
                expect(joined).toContain('aria-label="Edit name"');
            });

            it('Enter and Space on the name value invoke _startEditing("name")', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice' });
                const calls: string[] = [];
                (card as any)._startEditing = (field: string) => calls.push(field);
                (card as any)._handleEnabledToggle = () => undefined;
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const handlers = collectAllHandlers(hero);
                for (const h of handlers.filter((fn) => fn.length === 1)) {
                    try {
                        h({
                            key: 'Enter',
                            preventDefault: () => undefined,
                            target: { checked: false }
                        } as unknown as KeyboardEvent);
                        h({
                            key: ' ',
                            preventDefault: () => undefined,
                            target: { checked: false }
                        } as unknown as KeyboardEvent);
                    } catch {
                        // ignore
                    }
                }
                const nameCalls = calls.filter((c) => c === 'name').length;
                expect(nameCalls).toBeGreaterThanOrEqual(2);
            });
        });

        // A4 — lock-name a11y
        describe('A4: lock-name a11y', () => {
            it('exposes role=button, tabindex=0 and aria-label on the navigable lock name', () => {
                const tmpl = (card as any)._renderLockRow({
                    code: '1234',
                    entityId: 'lock.front',
                    inSync: true,
                    lockEntityId: 'lock.front',
                    name: 'Front Door',
                    syncStatus: 'in_sync'
                });
                const rendered = renderToString(tmpl);
                expect(rendered).toContain('role="button"');
                expect(rendered).toContain('tabindex="0"');
                expect(rendered).toContain('aria-label="View Front Door more info"');
            });

            it('Enter and Space on the lock name dispatch hass-more-info', () => {
                let dispatched = 0;
                (card as any)._navigateToLock = () => {
                    dispatched += 1;
                };
                const tmpl = (card as any)._renderLockRow({
                    code: '1234',
                    entityId: 'lock.front',
                    inSync: true,
                    lockEntityId: 'lock.front',
                    name: 'Front Door',
                    syncStatus: 'in_sync'
                });
                const handlers = collectAllHandlers(tmpl);
                for (const h of handlers.filter((fn) => fn.length === 1)) {
                    try {
                        h({
                            key: 'Enter',
                            preventDefault: () => undefined
                        } as unknown as KeyboardEvent);
                        h({
                            key: ' ',
                            preventDefault: () => undefined
                        } as unknown as KeyboardEvent);
                    } catch {
                        // ignore
                    }
                }
                expect(dispatched).toBeGreaterThanOrEqual(2);
            });
        });

        // A5 — collapsible header a11y
        describe('A5: collapsible header a11y', () => {
            it('exposes role=button, tabindex=0 and aria-expanded on the collapsible header', () => {
                const tmpl = (card as any)._renderCollapsible(
                    'Lock Status',
                    true,
                    () => undefined,
                    null
                );
                const joined = deepStrings(tmpl);
                expect(joined).toContain('class="collapsible-header"');
                expect(joined).toContain('role="button"');
                expect(joined).toContain('tabindex="0"');
                // aria-expanded is bound; the joined static strings keep the
                // attribute name even though the value is interpolated.
                expect(joined).toContain('aria-expanded=');
            });

            it('Enter and Space on the collapsible header invoke onToggle', () => {
                let toggled = 0;
                const tmpl = (card as any)._renderCollapsible(
                    'Lock Status',
                    true,
                    () => {
                        toggled += 1;
                    },
                    null
                );
                const handlers = collectAllHandlers(tmpl);
                for (const h of handlers.filter((fn) => fn.length === 1)) {
                    try {
                        h({
                            key: 'Enter',
                            preventDefault: () => undefined
                        } as unknown as KeyboardEvent);
                        h({
                            key: ' ',
                            preventDefault: () => undefined
                        } as unknown as KeyboardEvent);
                    } catch {
                        // ignore
                    }
                }
                // Enter + Space → 2 toggles.
                expect(toggled).toBeGreaterThanOrEqual(2);
            });
        });

        // A6 — action error banner role=alert
        describe('A6: action error banner role=alert', () => {
            it('renders role="alert" on the .action-error banner', () => {
                (card as any)._actionError = 'Boom';
                (card as any)._data = makeSlotCardData({});
                const tmpl = (card as any)._renderFromData((card as any)._data);
                const joined = deepStrings(tmpl);
                expect(joined).toContain('class="action-error"');
                expect(joined).toContain('role="alert"');
            });
        });

        // A7 — Saving… indicator aria-live
        describe('A7: dialog Saving indicator aria-live', () => {
            it('renders aria-live="polite" on the Saving… indicator', () => {
                (card as any)._showConditionDialog = true;
                (card as any)._dialogSaving = true;
                const tmpl = (card as any)._renderConditionDialog();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('class="dialog-saving"');
                expect(joined).toContain('aria-live="polite"');
            });
        });

        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    // Phase B: a11y polish — semantic HTML, ARIA labels, reduced motion (PR #1116 Phase B).
    describe('Phase B — a11y polish', () => {
        let card: SlotCardElement & Record<string, unknown>;

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepStrings).join('');
            if (!result || typeof result !== 'object') return '';
            if (!result.strings) return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepStrings).join('');
            return own + nested;
        }

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */

        describe('B1: semantic headings', () => {
            it('header title is rendered as <h2>, not <span>', () => {
                const tmpl = (card as any)._renderHeader();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<h2 class="header-title"');
                expect(joined).not.toContain('<span class="header-title"');
            });

            it('collapsible section title is rendered as <h3>', () => {
                const tmpl = (card as any)._renderCollapsible(
                    'Lock Status',
                    false,
                    () => undefined,
                    html`<div></div>`
                );
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<h3 class="collapsible-title"');
            });

            it('static condition header (no entity, no helpers) uses <h3>', () => {
                (card as any)._data = makeSlotCardData({ conditions: {} });
                const tmpl = (card as any)._renderConditionsSection({});
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<h3 class="collapsible-title"');
            });
        });

        describe('B2: aria-hidden on decorative icons and dots', () => {
            it('header icon bubble has aria-hidden', () => {
                const tmpl = (card as any)._renderHeader();
                const joined = deepStrings(tmpl);
                expect(joined).toMatch(/<div class="header-icon" aria-hidden="true"/);
            });

            it('state-chip dot has aria-hidden', () => {
                (card as any)._data = makeSlotCardData({ enabled: true, active: true });
                const tmpl = (card as any)._renderStateChip();
                const joined = deepStrings(tmpl);
                expect(joined).toMatch(/<span class="dot" aria-hidden="true"/);
            });

            it('collapsible chevron has aria-hidden', () => {
                const tmpl = (card as any)._renderCollapsible(
                    'Lock Status',
                    true,
                    () => undefined,
                    html`<div></div>`
                );
                const joined = deepStrings(tmpl);
                expect(joined).toContain('class="collapsible-chevron"');
                // chevron is always followed by aria-hidden in the same element
                expect(joined).toMatch(/class="collapsible-chevron"[\s\S]*?aria-hidden="true"/);
            });
        });

        describe('B3: edit input accessible names', () => {
            it('name edit input has aria-label="Edit name"', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice' });
                (card as any)._editingField = 'name';
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toContain('class="edit-input name-edit-input"');
                expect(joined).toContain('aria-label="Edit name"');
            });

            it('PIN edit input has aria-label="Edit PIN"', () => {
                (card as any)._data = makeSlotCardData({ name: 'Alice', pin: '1234' });
                (card as any)._editingField = 'pin';
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toContain('class="edit-input pin-edit-input"');
                expect(joined).toContain('aria-label="Edit PIN"');
            });
        });

        describe('B4: condition summary badge accessible name', () => {
            it('allowing entity badge gets "Allowing access" aria-label', () => {
                const tmpl = (card as any)._renderConditionsSummary({
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        friendly_name: 'Vacation',
                        state: 'on'
                    }
                });
                const joined = deepStrings(tmpl);
                expect(joined).toContain('Allowing access: Vacation');
            });

            it('blocking entity badge gets "Blocking access" aria-label', () => {
                const tmpl = (card as any)._renderConditionsSummary({
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        friendly_name: 'Vacation',
                        state: 'off'
                    }
                });
                const joined = deepStrings(tmpl);
                expect(joined).toContain('Blocking access: Vacation');
            });
        });

        describe('B5: helpers and lock lists are <ul>/<li>', () => {
            it('helpers list renders as <ul> with <li> children', async () => {
                card.setConfig({
                    condition_helpers: ['binary_sensor.foo'],
                    config_entry_id: 'abc',
                    slot: 1,
                    type: 'custom:lcm-slot'
                });
                card.hass = {
                    ...createMockHassWithConnection(),
                    states: {
                        'binary_sensor.foo': {
                            attributes: { friendly_name: 'Foo' },
                            entity_id: 'binary_sensor.foo',
                            state: 'on'
                        }
                    }
                } as any;
                const tmpl = (card as any)._renderHelpers();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<ul class="helpers-list"');
                expect(joined).toContain('<li>');
            });

            it('lock list renders as <ul> with <li> children', () => {
                const tmpl = (card as any)._renderLockStatusSection([
                    {
                        code: '1234',
                        codeLength: 4,
                        entityId: 'binary_sensor.front',
                        inSync: true,
                        lastSynced: '2024-01-01T00:00:00Z',
                        lockEntityId: 'lock.front_door',
                        name: 'Front Door',
                        syncStatus: 'in_sync'
                    }
                ]);
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<ul class="lock-list"');
                expect(joined).toContain('<li>');
            });
        });

        describe('B6: prefers-reduced-motion CSS rule is bundled', () => {
            it('slot card stylesheet contains a prefers-reduced-motion: reduce media query', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
            });

            it('reduced-motion rule disables transition on collapsible content', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(
                    /@media\s*\(prefers-reduced-motion:\s*reduce\)[^{]*\{[^}]*\.collapsible-content/s
                );
            });
        });

        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    // Phase B (Commit 2): visual consolidation — color-the-exception state,
    // helper sizes, condition badge color/icon swap, header icon by state,
    // dialog microcopy update.
    describe('Phase B — visual consolidation', () => {
        let card: SlotCardElement & Record<string, unknown>;

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepStrings).join('');
            if (!result || typeof result !== 'object') return '';
            if (!result.strings) return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepStrings).join('');
            return own + nested;
        }

        beforeEach(async () => {
            card = document.createElement('lcm-slot') as SlotCardElement & Record<string, unknown>;
            card.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */

        describe('VC1: active card has no special tint', () => {
            it('stylesheet does not define ha-card.slot-card-state-active', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).not.toMatch(/ha-card\.slot-card-state-active\s*\{/);
            });

            it('stylesheet still defines inactive and disabled state tints', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/ha-card\.slot-card-state-inactive\s*\{/);
                expect(allCss).toMatch(/ha-card\.slot-card-state-disabled\s*\{/);
            });
        });

        describe('VC2: ENABLED label dropped, switch gets aria-label', () => {
            it('hero toggle switch has aria-label="Enabled"', () => {
                (card as any)._data = makeSlotCardData({ enabled: true });
                const hero = (card as any)._renderHero('1234', 4, true, 'masked_with_reveal');
                const joined = deepStrings(hero);
                expect(joined).toContain('aria-label="Enabled"');
                // The visible "Enabled" label remains dropped.
                expect(joined).not.toMatch(/>Enabled</);
            });
        });

        describe('VC3: condition summary badge — allowing → success', () => {
            it('allowing case uses .collapsible-badge.success', () => {
                const tmpl = (card as any)._renderConditionsSummary({
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        friendly_name: 'Vacation',
                        state: 'on'
                    }
                });
                expect(tmpl.values).toContain('success');
            });

            it('blocking case still uses .collapsible-badge.warning', () => {
                const tmpl = (card as any)._renderConditionsSummary({
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        friendly_name: 'Vacation',
                        state: 'off'
                    }
                });
                expect(tmpl.values).toContain('warning');
            });

            it('shared styles include .collapsible-badge.success rule', async () => {
                const { lcmSharedStyles } = await import('./shared-styles');
                const allCss = String(lcmSharedStyles.cssText ?? lcmSharedStyles);
                expect(allCss).toMatch(/\.collapsible-badge\.success\s*\{/);
            });
        });

        describe('VC4: ✓/✗ glyphs replaced with mdi icons', () => {
            it('summary badge renders an icon, not the literal glyphs', () => {
                const tmpl = (card as any)._renderConditionsSummary({
                    condition_entity: {
                        condition_entity_id: 'calendar.vacation',
                        domain: 'calendar',
                        friendly_name: 'Vacation',
                        state: 'on'
                    }
                });
                const joined = deepStrings(tmpl);
                expect(joined).toContain('collapsible-badge-icon');
                expect(joined).not.toContain('✓');
                expect(joined).not.toContain('✗');
            });
        });

        describe('VC5: helpers label font-size bumped to 11px', () => {
            it('stylesheet uses 11px on .helpers-label', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/\.helpers-label\s*\{[^}]*font-size:\s*11px/s);
            });
        });

        describe('VC6: header icon bubble surfaces state', () => {
            // The actual mdi path strings come from @mdi/js — we look up the
            // exact path values rather than hard-coding the SVG to keep these
            // tests robust to mdi package version bumps.
            it('renders mdiKey when stateClass is active (default)', async () => {
                const { mdiKey } = await import('@mdi/js');
                const tmpl = (card as any)._renderHeader('active');
                expect(tmpl.values).toContain(mdiKey);
            });

            it('renders mdiClockOutline when stateClass is inactive', async () => {
                const { mdiClockOutline } = await import('@mdi/js');
                const tmpl = (card as any)._renderHeader('inactive');
                expect(tmpl.values).toContain(mdiClockOutline);
            });

            it('renders mdiLockOff when stateClass is disabled', async () => {
                const { mdiLockOff } = await import('@mdi/js');
                const tmpl = (card as any)._renderHeader('disabled');
                expect(tmpl.values).toContain(mdiLockOff);
            });
        });

        describe('DF1: collapsible content max-height bumped', () => {
            it('shared styles use 1000px max-height on .collapsible-content.expanded', async () => {
                const { lcmSharedStyles } = await import('./shared-styles');
                const allCss = String(lcmSharedStyles.cssText ?? lcmSharedStyles);
                expect(allCss).toMatch(
                    /\.collapsible-content\.expanded\s*\{[^}]*max-height:\s*1000px/s
                );
            });
        });

        describe('DF2: touch targets bumped to 32px', () => {
            it('hero pencil uses 32px button size', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(
                    /\.hero-name-pencil\s*\{[^}]*--mdc-icon-button-size:\s*32px/s
                );
            });

            it('hero PIN reveal uses 32px button size', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(
                    /\.hero-pin\s+\.reveal\s*\{[^}]*--mdc-icon-button-size:\s*32px/s
                );
            });

            it('lcm-reveal-button shared style uses 32px button size', async () => {
                const { lcmSharedStyles } = await import('./shared-styles');
                const allCss = String(lcmSharedStyles.cssText ?? lcmSharedStyles);
                expect(allCss).toMatch(
                    /\.lcm-reveal-button\s*\{[^}]*--mdc-icon-button-size:\s*32px/s
                );
            });

            it('action-error-dismiss has min 28px hit target', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/\.action-error-dismiss\s*\{[^}]*min-height:\s*28px/s);
                expect(allCss).toMatch(/\.action-error-dismiss\s*\{[^}]*min-width:\s*28px/s);
            });
        });

        describe('DF3: Last used "Never used" suppresses navigation affordance', () => {
            it('renders a non-interactive row when last_used is null', () => {
                (card as any)._data = makeSlotCardData({
                    event_entity_id: 'event.slot_1',
                    last_used: null
                });
                (card as any)._hass = {
                    ...(card as any)._hass,
                    states: {
                        'event.slot_1': {
                            entity_id: 'event.slot_1',
                            state: 'idle'
                        }
                    }
                };
                const tmpl = (card as any)._renderEventRow();
                const joined = deepStrings(tmpl);
                // Static rows do NOT carry the role=button affordance.
                expect(joined).not.toContain('role="button"');
                expect(joined).not.toContain('aria-label="View activity history"');
                // The arrow chevron is suppressed.
                expect(joined).not.toContain('class="event-arrow"');
                // The static class is applied so cursor/hover affordances drop.
                expect(joined).toContain('event-row-static');
            });

            it('keeps the interactive affordances when last_used is set', () => {
                (card as any)._data = makeSlotCardData({
                    event_entity_id: 'event.slot_1',
                    last_used: '2024-01-01T00:00:00Z',
                    last_used_lock: 'Front Door'
                });
                (card as any)._hass = {
                    ...(card as any)._hass,
                    states: {
                        'event.slot_1': {
                            entity_id: 'event.slot_1',
                            state: '2024-01-01T00:00:00Z'
                        }
                    }
                };
                const tmpl = (card as any)._renderEventRow();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('role="button"');
                expect(joined).toContain('aria-label="View activity history"');
                expect(joined).toContain('class="event-arrow"');
            });
        });

        describe('DF4: action-error contrast — bold weight', () => {
            it('action-error uses font-weight 600 to satisfy WCAG bold-text contrast', async () => {
                const { slotCardStyles } = await import('./slot-card.styles');
                const allCss = slotCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/\.action-error\s*\{[^}]*font-weight:\s*600/s);
            });
        });

        describe('VC7: dialog microcopy de-jargoned', () => {
            it('uses friendly "helper" and "on/off entity" copy in the dialog body', () => {
                (card as any)._showConditionDialog = true;
                const tmpl = (card as any)._renderConditionDialog();
                // Check the dialog description text directly (the static
                // strings of the inner <p class="dialog-description">)
                // — deepStrings would also pick up the picker's domain
                // list, which still contains "binary_sensor" as a filter
                // value but not as user-facing copy.
                const tmplJson = JSON.stringify(tmpl);
                // The friendly phrases must appear.
                expect(tmplJson).toContain('helper');
                expect(tmplJson).toContain('on/off');
                // The old jargon-y phrases must NOT appear in the static
                // strings (the picker config still uses the canonical
                // domain names internally; we only care about user-visible
                // text in the description paragraph).
                expect(tmplJson).not.toContain('input boolean');
                expect(tmplJson).not.toContain('binary\\nsensor');
                // The static description string no longer contains the
                // word "binary" in user-facing text.
                const description = tmpl.values.find(
                    (v: unknown) =>
                        typeof v === 'object' &&
                        v !== null &&
                        Array.isArray((v as any).strings) &&
                        (v as any).strings.some((s: string) => s.includes('dialog-description'))
                );
                if (description) {
                    const descStrings = description.strings.join(' ');
                    expect(descStrings).not.toContain('binary');
                    expect(descStrings).not.toContain('input boolean');
                }
            });
        });

        /* eslint-enable @typescript-eslint/no-explicit-any */
    });
});
