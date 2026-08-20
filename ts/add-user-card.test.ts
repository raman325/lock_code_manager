/* eslint-disable no-underscore-dangle */
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHassWithConnection } from './test/mock-hass';

/**
 * Tests for the add-user card (lcm-add-user).
 *
 * The card's whole job is to turn a name and a PIN into one `add_user`
 * call and then get out of the way, so these check what reaches the
 * service and when the page is allowed to reload.
 */

interface AddUserCardElement extends HTMLElement {
    _commit: () => Promise<void>;
    _enabled: boolean;
    _error?: string;
    _name: string;
    _pin: string;
    _reload: () => void;
    _showDialog: boolean;
    hass: HomeAssistant;
    setConfig: (config: Record<string, unknown>) => void;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('lcm-add-user', () => {
    let card: AddUserCardElement;
    let container: HTMLDivElement;
    let calls: Array<{ data: Record<string, unknown>; domain: string; service: string }>;
    let reloads: number;

    beforeAll(async () => {
        if (!customElements.get('lcm-add-user')) {
            await import('./add-user-card');
        }
    });

    beforeEach(async () => {
        calls = [];
        reloads = 0;
        container = document.createElement('div');
        document.body.appendChild(container);

        card = document.createElement('lcm-add-user') as unknown as AddUserCardElement;
        card.setConfig({ config_entry_id: 'entry-1', type: 'custom:lcm-add-user' });
        const hass = createMockHassWithConnection();
        hass.callService = (
            domain: string,
            service: string,
            data: Record<string, unknown>
        ): Promise<void> => {
            calls.push({ data, domain, service });
            return Promise.resolve();
        };
        card.hass = hass;
        // Reloading would take the test runner's page with it.
        card._reload = () => {
            reloads += 1;
        };
        container.appendChild(card);
        await flush();
    });

    describe('configuration', () => {
        it('refuses a config that names no config entry', () => {
            const bare = document.createElement('lcm-add-user') as unknown as AddUserCardElement;
            expect(() => bare.setConfig({ type: 'custom:lcm-add-user' })).toThrow(
                /config_entry_id or config_entry_title/
            );
        });

        it('accepts a config entry title in place of an id', () => {
            const byTitle = document.createElement('lcm-add-user') as unknown as AddUserCardElement;
            expect(() =>
                byTitle.setConfig({ config_entry_title: 'All Locks', type: 'custom:lcm-add-user' })
            ).not.toThrow();
        });
    });

    describe('the card API Home Assistant calls', () => {
        it('offers a stub config the card editor accepts', () => {
            const stub = (
                customElements.get('lcm-add-user') as unknown as {
                    getStubConfig: () => Record<string, unknown>;
                }
            ).getStubConfig();

            expect(stub.type).toBe('custom:lcm-add-user');
        });

        it('claims one row of the masonry grid', () => {
            expect((card as unknown as { getCardSize: () => number }).getCardSize()).toBe(1);
        });

        it('will not add a user before it has been configured', async () => {
            const unconfigured = document.createElement(
                'lcm-add-user'
            ) as unknown as AddUserCardElement;
            unconfigured._name = 'Raman';

            await unconfigured._commit();

            expect(calls).toHaveLength(0);
            expect(unconfigured._error).toContain('not initialized');
        });
    });

    describe('opening', () => {
        it('offers a button, not a form', () => {
            const button = card.shadowRoot!.querySelector('ha-card')!;
            expect(button.getAttribute('aria-label')).toBe('Add user');
            expect(card.shadowRoot!.querySelector('ha-dialog')).toBeNull();
        });

        it('opens the dialog on click', async () => {
            card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
            await flush();

            expect(card._showDialog).toBe(true);
            expect(card.shadowRoot!.querySelector('ha-dialog')).toBeTruthy();
        });

        it('opens the dialog from the keyboard', async () => {
            const button = card.shadowRoot!.querySelector<HTMLElement>('ha-card')!;
            button.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
            await flush();

            expect(card._showDialog).toBe(true);
        });

        it('ignores keys that are not activation keys', async () => {
            const button = card.shadowRoot!.querySelector<HTMLElement>('ha-card')!;
            button.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
            await flush();

            expect(card._showDialog).toBe(false);
        });

        it('starts from a blank form every time', async () => {
            card._showDialog = true;
            card._name = 'Leftover';
            card._pin = '9999';
            card._enabled = false;
            card._error = 'stale';
            card._showDialog = false;

            card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
            await flush();

            expect(card._name).toBe('');
            expect(card._pin).toBe('');
            expect(card._enabled).toBe(true);
            expect(card._error).toBeUndefined();
        });
    });

    describe('the form', () => {
        beforeEach(async () => {
            card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
            await flush();
        });

        const fields = () => [
            ...card.shadowRoot!.querySelectorAll<HTMLInputElement>('.field-input')
        ];

        it('takes the name and the PIN from what is typed', async () => {
            const [name, pin] = fields();
            name.value = 'Raman';
            name.dispatchEvent(new Event('input'));
            pin.value = '4321';
            pin.dispatchEvent(new Event('input'));
            await flush();

            expect(card._name).toBe('Raman');
            expect(card._pin).toBe('4321');
        });

        it('does not show the PIN while it is typed', () => {
            const [, pin] = fields();
            expect(pin.getAttribute('type')).toBe('password');
        });

        it('takes Enabled from the checkbox', async () => {
            const checkbox =
                card.shadowRoot!.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
            checkbox.checked = false;
            checkbox.dispatchEvent(new Event('change'));
            await flush();

            expect(card._enabled).toBe(false);
        });

        it('closes on cancel without adding anybody', async () => {
            card.shadowRoot!.querySelector<HTMLButtonElement>(
                'button.dialog-action[slot="secondaryAction"]'
            )!.click();
            await flush();

            expect(card._showDialog).toBe(false);
            expect(calls).toHaveLength(0);
        });

        it('closes when dismissed with escape or the scrim', async () => {
            card.shadowRoot!.querySelector('ha-dialog')!.dispatchEvent(new Event('closed'));
            await flush();

            expect(card._showDialog).toBe(false);
            expect(calls).toHaveLength(0);
        });

        it('adds from the Add button', async () => {
            card._name = 'Raman';
            card.shadowRoot!.querySelector<HTMLButtonElement>(
                'button.dialog-action[slot="primaryAction"]'
            )!.click();
            await flush();

            expect(calls).toHaveLength(1);
        });
    });

    describe('adding', () => {
        beforeEach(async () => {
            card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
            await flush();
        });

        it('adds the user and reloads so the strategy runs again', async () => {
            card._name = 'Raman';
            card._pin = '1234';
            await card._commit();

            expect(calls).toEqual([
                {
                    data: {
                        config_entry_id: 'entry-1',
                        enabled: true,
                        name: 'Raman',
                        pin: '1234'
                    },
                    domain: 'lock_code_manager',
                    service: 'add_user'
                }
            ]);
            expect(reloads).toBe(1);
        });

        it('omits a blank PIN rather than sending an empty one', async () => {
            card._name = 'Raman';
            await card._commit();

            expect(calls[0].data).not.toHaveProperty('pin');
        });

        it('trims the name', async () => {
            card._name = '  Raman  ';
            await card._commit();

            expect(calls[0].data.name).toBe('Raman');
        });

        it('passes an unticked Enabled through', async () => {
            card._name = 'Raman';
            card._enabled = false;
            await card._commit();

            expect(calls[0].data.enabled).toBe(false);
        });

        it('addresses the entry by title when configured that way', async () => {
            card.setConfig({ config_entry_title: 'All Locks', type: 'custom:lcm-add-user' });
            card._name = 'Raman';
            await card._commit();

            expect(calls[0].data).toMatchObject({ config_entry_title: 'All Locks' });
            expect(calls[0].data).not.toHaveProperty('config_entry_id');
        });

        it('sends one way of naming the entry, never both', async () => {
            card.setConfig({
                config_entry_id: 'entry-1',
                config_entry_title: 'All Locks',
                type: 'custom:lcm-add-user'
            });
            card._name = 'Raman';
            await card._commit();

            // The action declares these mutually exclusive and rejects a
            // call carrying the pair, so the id wins.
            expect(calls[0].data).toMatchObject({ config_entry_id: 'entry-1' });
            expect(calls[0].data).not.toHaveProperty('config_entry_title');
        });

        it('asks for a name instead of adding a blank user', async () => {
            card._name = '   ';
            await card._commit();

            expect(calls).toHaveLength(0);
            expect(card._error).toContain('name');
            // Still open, so the name can be filled in.
            expect(card._showDialog).toBe(true);
        });

        it('keeps the form up when the service refuses', async () => {
            card.hass.callService = () => Promise.reject(new Error('no free slots'));
            card._name = 'Raman';
            await card._commit();

            expect(card._error).toBe('no free slots');
            expect(card._showDialog).toBe(true);
            // A reload here would wipe the form and show nothing new.
            expect(reloads).toBe(0);
        });

        it('reloads the actual page on success', async () => {
            const real = document.createElement('lcm-add-user') as unknown as AddUserCardElement;
            const reload = vi.fn();
            Object.defineProperty(window, 'location', {
                configurable: true,
                value: { ...window.location, reload }
            });

            real._reload();

            expect(reload).toHaveBeenCalled();
        });

        it('shows a rejection that is not an Error', async () => {
            // HA's websocket rejects with a plain object, not an Error.
            // eslint-disable-next-line prefer-promise-reject-errors
            card.hass.callService = () => Promise.reject('slot 1 is taken');
            card._name = 'Raman';
            await card._commit();

            expect(card._error).toContain('slot 1 is taken');
            expect(reloads).toBe(0);
        });

        it('adds once when Add is hit twice', async () => {
            card._name = 'Raman';
            await Promise.all([card._commit(), card._commit()]);

            expect(calls).toHaveLength(1);
        });
    });
});
