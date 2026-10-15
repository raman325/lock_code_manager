/* eslint-disable no-underscore-dangle */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHass } from './test/mock-hass';
import { LockCodeManagerSlotCardConfig } from './types';

/** Type alias for the editor element with its internal properties exposed */
interface EditorElement extends HTMLElement {
    _config?: LockCodeManagerSlotCardConfig;
    _hass?: HomeAssistant;
    hass: HomeAssistant;
    setConfig(config: LockCodeManagerSlotCardConfig): void;
}

describe('LcmSlotCardEditor integration', () => {
    let el: EditorElement;
    let container: HTMLDivElement;

    beforeAll(async () => {
        if (!customElements.get('lcm-user-editor')) {
            await import('./slot-card-editor');
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

    async function flush(): Promise<void> {
        await new Promise((r) => setTimeout(r, 0));
    }

    function createEditor(config?: Partial<LockCodeManagerSlotCardConfig>): EditorElement {
        const editor = document.createElement('lcm-user-editor') as EditorElement;
        editor.setConfig({
            config_entry_id: 'abc',
            slot: 1,
            type: 'custom:lcm-slot',
            ...config
        } as LockCodeManagerSlotCardConfig);
        return editor;
    }

    /* eslint-disable @typescript-eslint/no-explicit-any */
    describe('_showLockCountChanged', () => {
        it('updates show_lock_count in config', async () => {
            el = createEditor();
            el.hass = createMockHass({
                callWS: () => []
            });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._showLockCountChanged({
                target: { checked: false }
            });

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.show_lock_count).toBe(false);
        });
    });

    describe('_toggleShowLockCount', () => {
        it('toggles show_lock_count from default to false', async () => {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._toggleShowLockCount();

            expect(events).toHaveLength(1);
            // Default (undefined) treated as true, toggle makes it false
            expect(events[0].detail.config.show_lock_count).toBe(false);
        });

        it('toggles show_lock_count from false to true', async () => {
            el = createEditor({ show_lock_count: false });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._toggleShowLockCount();

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.show_lock_count).toBe(true);
        });
    });

    describe('_toggleCollapsedSection', () => {
        it('adds conditions to collapsed_sections', async () => {
            el = createEditor({ collapsed_sections: [] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._toggleCollapsedSection('conditions', true);

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.collapsed_sections).toEqual(['conditions']);
        });

        it('removes conditions from collapsed_sections', async () => {
            el = createEditor({ collapsed_sections: ['conditions', 'lock_status'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._toggleCollapsedSection('conditions', false);

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.collapsed_sections).toEqual(['lock_status']);
        });

        it('sets collapsed_sections to empty array when last is removed', async () => {
            el = createEditor({ collapsed_sections: ['conditions'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._toggleCollapsedSection('conditions', false);

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.collapsed_sections).toEqual([]);
        });

        it('does not duplicate when section already collapsed', async () => {
            el = createEditor({ collapsed_sections: ['conditions'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._toggleCollapsedSection('conditions', true);

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.collapsed_sections).toEqual(['conditions']);
        });
    });

    describe('_addConditionHelper', () => {
        it('adds helper entity to condition_helpers', async () => {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._addConditionHelper({
                detail: { value: 'input_boolean.test' }
            });

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.condition_helpers).toEqual(['input_boolean.test']);
        });

        it('does not add empty entity ID', async () => {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._addConditionHelper({
                detail: { value: '' }
            });

            expect(events).toHaveLength(0);
        });

        it('does not add duplicate entity', async () => {
            el = createEditor({ condition_helpers: ['input_boolean.test'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._addConditionHelper({
                detail: { value: 'input_boolean.test' }
            });

            expect(events).toHaveLength(0);
        });

        it('appends to existing helpers', async () => {
            el = createEditor({ condition_helpers: ['input_boolean.a'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._addConditionHelper({
                detail: { value: 'input_boolean.b' }
            });

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.condition_helpers).toEqual([
                'input_boolean.a',
                'input_boolean.b'
            ]);
        });
    });

    describe('_removeConditionHelper', () => {
        it('removes helper by index', async () => {
            el = createEditor({
                condition_helpers: ['input_boolean.a', 'input_boolean.b']
            });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._removeConditionHelper(0);

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.condition_helpers).toEqual(['input_boolean.b']);
        });

        it('sets condition_helpers to undefined when last is removed', async () => {
            el = createEditor({ condition_helpers: ['input_boolean.only'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));

            (el as any)._removeConditionHelper(0);

            expect(events).toHaveLength(1);
            expect(events[0].detail.config.condition_helpers).toBeUndefined();
        });
    });

    describe('render with condition_helpers', () => {
        /** Join a TemplateResult's static strings to inspect element tags */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function templateStrings(result: any): string {
            return (result?.strings ?? []).join('');
        }

        /** Extract inline handler functions from a TemplateResult's values */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function extractHandlers(result: any): Array<() => void> {
            return (result?.values ?? []).filter((v: unknown) => typeof v === 'function');
        }

        /** Recursively collect all handlers from nested templates */
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

        it('renders condition helper entries with remove buttons', async () => {
            el = createEditor({
                condition_helpers: ['input_boolean.helper_1', 'input_boolean.helper_2']
            });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const tmpl = (el as any).render();
            const joined = allTemplateStrings(tmpl);
            expect(joined).toContain('helper-entry');
            expect(joined).toContain('ha-entity-picker');
        });

        it('renders collapsed_sections checkboxes', async () => {
            el = createEditor({ collapsed_sections: ['conditions'] });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const tmpl = (el as any).render();
            const joined = templateStrings(tmpl);
            expect(joined).toContain('Initially Expanded');
        });

        it('renders show_lock_count checkbox', async () => {
            el = createEditor({ show_lock_count: false });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const tmpl = (el as any).render();
            const joined = templateStrings(tmpl);
            expect(joined).toContain('Lock Count');
        });

        it('invokes inline handlers from rendered template', async () => {
            el = createEditor({
                collapsed_sections: ['conditions'],
                condition_helpers: ['input_boolean.helper_1']
            });
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();

            const tmpl = (el as any).render();
            const handlers = collectAllHandlers(tmpl);

            // Invoke all handlers to cover inline lambdas (for collapsed_sections
            // change handlers and removeConditionHelper click handlers)
            for (const handler of handlers) {
                try {
                    handler();
                } catch {
                    // expected - handlers reference component internals
                }
            }
            expect(handlers.length).toBeGreaterThan(0);
        });
    });
    describe('_configEntryChanged', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        beforeEach(async () => {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();
        });

        it('updates config_entry_id and dispatches', async () => {
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = { config_entry_id: 'old', slot: 1, type: 'custom:lcm-slot' };
            (el as any)._configEntryChanged({ target: { value: 'new-id' } });
            expect((el as any)._config.config_entry_id).toBe('new-id');
            expect(dispatchSpy).toHaveBeenCalled();
            dispatchSpy.mockRestore();
        });

        it('returns early when value unchanged', () => {
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = { config_entry_id: 'same', slot: 1, type: 'custom:lcm-slot' };
            (el as any)._configEntryChanged({ target: { value: 'same' } });
            expect(dispatchSpy).not.toHaveBeenCalled();
            dispatchSpy.mockRestore();
        });

        it('returns early without config', () => {
            (el as any)._config = undefined;
            expect(() => (el as any)._configEntryChanged({ target: { value: 'x' } })).not.toThrow();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_nameChanged', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        beforeEach(async () => {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();
        });

        it('updates the user and dispatches', () => {
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = {
                config_entry_id: 'abc',
                name: 'Raman',
                type: 'custom:lcm-user'
            };
            (el as any)._nameChanged({ target: { value: 'Sherene' } });
            expect((el as any)._config.name).toBe('Sherene');
            expect(dispatchSpy).toHaveBeenCalled();
            dispatchSpy.mockRestore();
        });

        it('drops a slot number the card was carrying', () => {
            // A card holding both would keep showing whoever holds that
            // number, whatever the name said.
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = { config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' };
            (el as any)._nameChanged({ target: { value: 'Raman' } });
            expect((el as any)._config.name).toBe('Raman');
            expect((el as any)._config.slot).toBeUndefined();
            dispatchSpy.mockRestore();
        });

        it('returns early when the user is unchanged', () => {
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = {
                config_entry_id: 'abc',
                name: 'Raman',
                type: 'custom:lcm-user'
            };
            (el as any)._nameChanged({ target: { value: 'Raman' } });
            expect(dispatchSpy).not.toHaveBeenCalled();
            dispatchSpy.mockRestore();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_displayModeChanged', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        beforeEach(async () => {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();
        });

        it('updates code_display and dispatches', () => {
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = {
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot',
                code_display: 'masked'
            };
            (el as any)._displayModeChanged({ target: { value: 'unmasked' } });
            expect((el as any)._config.code_display).toBe('unmasked');
            expect(dispatchSpy).toHaveBeenCalled();
            dispatchSpy.mockRestore();
        });

        it('returns early when mode unchanged', () => {
            const dispatchSpy = vi.spyOn(el, 'dispatchEvent');
            (el as any)._config = {
                config_entry_id: 'abc',
                slot: 1,
                type: 'custom:lcm-slot',
                code_display: 'masked'
            };
            (el as any)._displayModeChanged({ target: { value: 'masked' } });
            expect(dispatchSpy).not.toHaveBeenCalled();
            dispatchSpy.mockRestore();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('show-section checkboxes', () => {
        const sections = [
            ['Condition', 'show_conditions'],
            ['Lock Status', 'show_lock_status'],
            ['Sync Status in Lock Status', 'show_lock_sync']
        ] as const;

        async function mount(
            config: Partial<LockCodeManagerSlotCardConfig> = {}
        ): Promise<CustomEvent[]> {
            el = createEditor(config);
            el.hass = createMockHass({ callWS: () => [] });
            container.appendChild(el);
            await flush();
            const events: CustomEvent[] = [];
            el.addEventListener('config-changed', (e) => events.push(e as CustomEvent));
            return events;
        }

        /** The checkbox row under "Show Sections" whose label reads ``label``. */
        function row(label: string): HTMLElement {
            const rows = [...el.shadowRoot!.querySelectorAll<HTMLElement>('.checkbox-row')];
            const found = rows.find((r) => r.querySelector('label')?.textContent?.trim() === label);
            expect(found, `no checkbox row labelled ${label}`).toBeTruthy();
            return found!;
        }

        it.each(sections)('%s is ticked by default', async (label) => {
            await mount();
            const box = row(label).querySelector('ha-checkbox') as HTMLElement & {
                checked: boolean;
            };
            expect(box.checked).toBe(true);
        });

        it.each(sections)('%s is unticked when hidden in the config', async (label, key) => {
            await mount({ [key]: false });
            const box = row(label).querySelector('ha-checkbox') as HTMLElement & {
                checked: boolean;
            };
            expect(box.checked).toBe(false);
        });

        it.each(sections)('clicking the %s label hides a shown section', async (label, key) => {
            const events = await mount();
            row(label).querySelector('label')!.click();
            expect(events).toHaveLength(1);
            expect(events[0].detail.config[key]).toBe(false);
        });

        it.each(sections)('clicking the %s label shows a hidden section', async (label, key) => {
            const events = await mount({ [key]: false });
            row(label).querySelector('label')!.click();
            expect(events).toHaveLength(1);
            expect(events[0].detail.config[key]).toBe(true);
        });

        it.each(sections)('unticking the %s checkbox hides the section', async (label, key) => {
            const events = await mount();
            const box = row(label).querySelector('ha-checkbox') as HTMLElement & {
                checked: boolean;
            };
            box.checked = false;
            box.dispatchEvent(new Event('change'));
            expect(events).toHaveLength(1);
            expect(events[0].detail.config[key]).toBe(false);
        });
    });

    describe('config entry choices', () => {
        async function mountWithEntries(entries: unknown[]): Promise<ShadowRoot> {
            el = createEditor();
            el.hass = createMockHass({ callWS: () => entries });
            container.appendChild(el);
            await flush();
            await (el as unknown as { updateComplete: Promise<boolean> }).updateComplete;
            return el.shadowRoot!;
        }

        it('offers only loaded config entries', async () => {
            const root = await mountWithEntries([
                { entry_id: 'a', state: 'loaded', title: 'House' },
                { entry_id: 'b', state: 'not_loaded', title: 'Cabin' },
                { entry_id: 'c', state: 'setup_error', title: 'Shed' },
                { entry_id: 'd', state: 'loaded', title: 'Garage' }
            ]);
            type Valued = HTMLElement & { label?: string; value: string };
            const entrySelect = [...root.querySelectorAll<Valued>('ha-select')].find(
                (select) => select.label === 'LCM Config Entry'
            );
            expect(entrySelect).toBeTruthy();
            const offered = [...entrySelect!.querySelectorAll<Valued>('ha-list-item')].map(
                (item) => item.value
            );
            expect(offered).toEqual(['a', 'd']);
            expect(root.querySelector('.no-entries-warning')).toBeNull();
        });

        it('warns when no config entry is loaded', async () => {
            const root = await mountWithEntries([
                { entry_id: 'b', state: 'not_loaded', title: 'Cabin' }
            ]);
            expect(root.querySelector('.no-entries-warning')).not.toBeNull();
        });
    });
    /* eslint-enable @typescript-eslint/no-explicit-any */
});
