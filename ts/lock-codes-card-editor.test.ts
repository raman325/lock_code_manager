import { afterEach, beforeAll, describe, expect, it } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHass } from './test/mock-hass';
import { CodeDisplayMode, LockCodesCardConfig } from './types';

/**
 * The lock-codes card editor, mounted: config goes in through setConfig,
 * locks come from the websocket, and assertions read its shadow DOM and the
 * config-changed events it dispatches.
 */

interface LockCodesEditorElement extends HTMLElement {
    hass: HomeAssistant;
    setConfig: (config: LockCodesCardConfig) => void;
    updateComplete: Promise<boolean>;
}

type Valued<T = string> = HTMLElement & { value: T };

type CallWS = (msg: { [key: string]: unknown; type: string }) => unknown;

/** Answers the editor's websocket calls: entries, then each entry's locks. */
function locksByEntry(entries: Record<string, string[] | Error>): CallWS {
    return (msg) => {
        if (msg.type === 'config_entries/get') {
            return Object.keys(entries).map((entry_id) => {
                return { entry_id };
            });
        }
        const locks = entries[msg.config_entry_id as string];
        if (locks instanceof Error) {
            return Promise.reject(locks);
        }
        return {
            locks: locks.map((entity_id) => {
                return { entity_id, name: entity_id };
            })
        };
    };
}

describe('lcm-lock-codes-editor', () => {
    let el: LockCodesEditorElement | undefined;
    let events: CustomEvent[];

    beforeAll(async () => {
        if (!customElements.get('lcm-lock-codes-editor')) {
            await import('./lock-codes-card-editor');
        }
    });

    afterEach(() => {
        el?.remove();
        el = undefined;
    });

    async function mountEditor(
        config: Partial<LockCodesCardConfig> = {},
        callWS: CallWS = () => []
    ): Promise<LockCodesEditorElement> {
        const editor = document.createElement('lcm-lock-codes-editor') as LockCodesEditorElement;
        editor.setConfig({
            lock_entity_id: 'lock.front_door',
            type: 'custom:lcm-lock-codes',
            ...config
        });
        editor.hass = createMockHass({ callWS });
        document.body.appendChild(editor);
        await new Promise((r) => setTimeout(r, 0));
        await editor.updateComplete;
        events = [];
        editor.addEventListener('config-changed', (e) => events.push(e as CustomEvent));
        return editor;
    }

    function root(): ShadowRoot {
        return el!.shadowRoot!;
    }

    function codeDisplaySelect(): Valued<CodeDisplayMode> {
        const select = root().querySelector<Valued<CodeDisplayMode>>('ha-select');
        expect(select).not.toBeNull();
        return select!;
    }

    describe('code display', () => {
        it('shows the card default (masked with reveal) when no code_display is configured', async () => {
            el = await mountEditor();
            expect(codeDisplaySelect().value).toBe('masked_with_reveal');
        });

        it('shows the configured code_display', async () => {
            el = await mountEditor({ code_display: 'unmasked' });
            expect(codeDisplaySelect().value).toBe('unmasked');
        });

        it('offers all three modes', async () => {
            el = await mountEditor();
            expect(
                [...codeDisplaySelect().querySelectorAll<Valued>('ha-list-item')].map(
                    (item) => item.value
                )
            ).toEqual(['masked_with_reveal', 'masked', 'unmasked']);
        });

        it('dispatches the chosen mode', async () => {
            el = await mountEditor({ code_display: 'masked', title: 'Front' });
            const select = codeDisplaySelect();
            select.value = 'unmasked';
            select.dispatchEvent(new CustomEvent('selected'));
            expect(events).toHaveLength(1);
            expect(events[0].detail.config).toEqual({
                code_display: 'unmasked',
                lock_entity_id: 'lock.front_door',
                title: 'Front',
                type: 'custom:lcm-lock-codes'
            });
        });

        it('does not dispatch when the mode is unchanged', async () => {
            el = await mountEditor({ code_display: 'masked' });
            codeDisplaySelect().dispatchEvent(new CustomEvent('selected'));
            expect(events).toHaveLength(0);
        });
    });

    describe('lock picker', () => {
        function picker(): HTMLElement & { includeEntities: string[]; value: string } {
            const found = root().querySelector('ha-entity-picker');
            expect(found).not.toBeNull();
            return found as HTMLElement & { includeEntities: string[]; value: string };
        }

        it('says it is loading until the locks arrive', async () => {
            el = await mountEditor({}, () => new Promise(() => {}));
            expect(root().textContent).toContain('Loading locks...');
            expect(root().querySelector('.no-locks-warning')).toBeNull();
        });

        it('warns when no lock is managed', async () => {
            el = await mountEditor({}, locksByEntry({}));
            expect(root().textContent).not.toContain('Loading locks...');
            expect(root().querySelector('.no-locks-warning')).not.toBeNull();
        });

        it("offers every entry's locks once, skipping an entry that fails", async () => {
            el = await mountEditor(
                {},
                locksByEntry({
                    a: ['lock.front_door', 'lock.back_door'],
                    b: new Error('not loaded'),
                    c: ['lock.back_door', 'lock.garage']
                })
            );
            expect(root().textContent).not.toContain('Loading locks...');
            expect(root().querySelector('.no-locks-warning')).toBeNull();
            expect(picker().includeEntities).toEqual([
                'lock.front_door',
                'lock.back_door',
                'lock.garage'
            ]);
            expect(picker().value).toBe('lock.front_door');
        });

        it('dispatches the chosen lock, keeping the rest of the config', async () => {
            el = await mountEditor({ code_display: 'masked', title: 'Front' });
            picker().dispatchEvent(
                new CustomEvent('value-changed', { detail: { value: 'lock.back_door' } })
            );
            expect(events).toHaveLength(1);
            expect(events[0].detail.config).toEqual({
                code_display: 'masked',
                lock_entity_id: 'lock.back_door',
                title: 'Front',
                type: 'custom:lcm-lock-codes'
            });
        });

        it('does not dispatch when the same lock is picked', async () => {
            el = await mountEditor();
            picker().dispatchEvent(
                new CustomEvent('value-changed', { detail: { value: 'lock.front_door' } })
            );
            expect(events).toHaveLength(0);
        });
    });

    describe('title', () => {
        function titleInput(): Valued {
            const found = root().querySelector<Valued>('ha-input');
            expect(found).not.toBeNull();
            return found!;
        }

        async function type(value: string): Promise<void> {
            const input = titleInput();
            input.value = value;
            input.dispatchEvent(new Event('input'));
            await el!.updateComplete;
        }

        it('shows an empty field when no title is configured', async () => {
            el = await mountEditor();
            expect(titleInput().value).toBe('');
        });

        it('dispatches a typed title', async () => {
            el = await mountEditor();
            await type('Front');
            expect(events).toHaveLength(1);
            expect(events[0].detail.config.title).toBe('Front');
        });

        it('clears the title to undefined, so the card falls back to its own', async () => {
            el = await mountEditor({ title: 'Front' });
            await type('');
            expect(events).toHaveLength(1);
            expect(events[0].detail.config.title).toBeUndefined();
        });

        it('keeps whitespace as typed', async () => {
            el = await mountEditor();
            await type('   ');
            expect(events[0].detail.config.title).toBe('   ');
        });

        it('does not dispatch when the title is unchanged', async () => {
            el = await mountEditor({ title: 'Front' });
            await type('Front');
            expect(events).toHaveLength(0);
        });
    });

    it('renders nothing before it has hass and a config', async () => {
        el = document.createElement('lcm-lock-codes-editor') as LockCodesEditorElement;
        document.body.appendChild(el);
        await el.updateComplete;
        expect(root().querySelector('ha-select')).toBeNull();
        expect(root().querySelector('ha-entity-picker')).toBeNull();
    });

    it('dispatches config-changed so it bubbles out of the shadow root', async () => {
        el = await mountEditor();
        const outer: CustomEvent[] = [];
        const onOuter = (e: Event): void => {
            outer.push(e as CustomEvent);
        };
        document.body.addEventListener('config-changed', onOuter);
        try {
            const select = codeDisplaySelect();
            select.value = 'masked';
            select.dispatchEvent(new CustomEvent('selected'));
        } finally {
            document.body.removeEventListener('config-changed', onOuter);
        }
        expect(outer).toHaveLength(1);
        expect(outer[0].bubbles).toBe(true);
        expect(outer[0].composed).toBe(true);
    });
});
