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
    _condition: string;
    _enabled: boolean;
    _error?: string;
    _generatePin: () => Promise<void>;
    _name: string;
    _pickerReady: boolean;
    _pin: string;
    _pinLength: number;
    _pinVisible: boolean;
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

        const pinField = () => card.shadowRoot!.querySelector<HTMLInputElement>('#add-user-pin')!;
        const revealButton = () =>
            card.shadowRoot!.querySelector<HTMLElement & { label: string; path: string }>(
                '.lcm-reveal-button'
            )!;

        it('hides the PIN until asked', () => {
            expect(pinField().getAttribute('type')).toBe('password');
            expect(revealButton().label).toBe('Show PIN');
        });

        it('shows the PIN when the button is pressed', async () => {
            revealButton().click();
            await flush();

            expect(card._pinVisible).toBe(true);
            expect(pinField().getAttribute('type')).toBe('text');
            expect(revealButton().label).toBe('Hide PIN');
        });

        it('hides it again on a second press', async () => {
            revealButton().click();
            await flush();
            revealButton().click();
            await flush();

            expect(pinField().getAttribute('type')).toBe('password');
        });

        it('changes the icon with the state', async () => {
            const hidden = revealButton().path;
            revealButton().click();
            await flush();

            expect(revealButton().path).not.toBe(hidden);
        });

        it('reopens hidden, whatever it was left as', async () => {
            revealButton().click();
            await flush();
            expect(card._pinVisible).toBe(true);

            card._showDialog = false;
            card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
            await flush();

            expect(card._pinVisible).toBe(false);
            expect(pinField().getAttribute('type')).toBe('password');
        });

        describe('generating one', () => {
            const generateButton = () =>
                card.shadowRoot!.querySelector<HTMLButtonElement>('.generate-button')!;

            beforeEach(() => {
                card.hass.callService = (
                    domain: string,
                    service: string,
                    data: Record<string, unknown>
                ) => {
                    calls.push({ data, domain, service });
                    return Promise.resolve({ response: { pin: '739284' } });
                };
            });

            it('asks the action for the PIN rather than inventing one', async () => {
                await card._generatePin();

                expect(calls).toEqual([
                    {
                        data: { length: 4 },
                        domain: 'lock_code_manager',
                        service: 'generate_pin'
                    }
                ]);
                expect(card._pin).toBe('739284');
            });

            it('reveals what it generated', async () => {
                expect(card._pinVisible).toBe(false);
                await card._generatePin();

                // A code you cannot read is one you cannot pass on.
                expect(card._pinVisible).toBe(true);
                expect(pinField().getAttribute('type')).toBe('text');
            });

            it('generates at the length asked for', async () => {
                const length =
                    card.shadowRoot!.querySelector<HTMLInputElement>('#add-user-pin-length')!;
                length.value = '8';
                length.dispatchEvent(new Event('input'));
                await flush();

                await card._generatePin();

                expect(calls[0].data).toEqual({ length: 8 });
            });

            it('generates from the button', async () => {
                generateButton().click();
                await flush();

                expect(calls).toHaveLength(1);
            });

            it('refuses a length the action would reject', async () => {
                card._pinLength = 99;
                await card._generatePin();

                expect(calls).toHaveLength(0);
                expect(card._error).toContain('between 4 and 12');
            });

            it('refuses a length that is not a whole number', async () => {
                card._pinLength = Number.NaN;
                await card._generatePin();

                expect(calls).toHaveLength(0);
                expect(card._error).toContain('between 4 and 12');
            });

            it('says so when the action gives back no PIN', async () => {
                card.hass.callService = () => Promise.resolve({ response: {} });
                await card._generatePin();

                expect(card._error).toContain('No PIN came back');
                expect(card._pin).toBe('');
            });

            it('says so when the action fails', async () => {
                card.hass.callService = () => Promise.reject(new Error('nope'));
                await card._generatePin();

                expect(card._error).toContain('nope');
            });

            it('shows a rejection that is not an Error', async () => {
                // HA's websocket rejects with a plain object, not an Error.
                // eslint-disable-next-line prefer-promise-reject-errors
                card.hass.callService = () => Promise.reject('length rejected');
                await card._generatePin();

                expect(card._error).toContain('length rejected');
            });

            it('generates once when the button is hit twice', async () => {
                await Promise.all([card._generatePin(), card._generatePin()]);

                expect(calls).toHaveLength(1);
            });

            it('reopens back at the default length', async () => {
                card._pinLength = 10;
                card._showDialog = false;
                card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
                await flush();

                expect(card._pinLength).toBe(4);
            });
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
                '.dialog-actions button:first-of-type'
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
                '.dialog-actions button:last-of-type'
            )!.click();
            await flush();

            expect(calls).toHaveLength(1);
        });
    });

    describe('the condition field', () => {
        beforeEach(async () => {
            card.shadowRoot!.querySelector<HTMLElement>('ha-card')!.click();
            await flush();
        });

        it('is left out when Home Assistant gave us no picker', () => {
            expect(card._pickerReady).toBe(false);
            expect(card.shadowRoot!.querySelector('ha-entity-picker')).toBeNull();
            // Not replaced by a free-text entity id: a mistyped one is worse
            // than setting the condition from the user card afterwards.
            const labels = [...card.shadowRoot!.querySelectorAll('.field-label')].map((element) =>
                element.textContent?.trim()
            );
            expect(labels).toEqual(['Name', 'PIN']);
        });

        it('appears once the picker is registered', async () => {
            card._pickerReady = true;
            await flush();

            expect(card.shadowRoot!.querySelector('ha-entity-picker')).toBeTruthy();
        });

        it('takes the entity the picker reports', async () => {
            card._pickerReady = true;
            await flush();

            card.shadowRoot!.querySelector('ha-entity-picker')!.dispatchEvent(
                new CustomEvent('value-changed', { detail: { value: 'calendar.guests' } })
            );
            await flush();

            expect(card._condition).toBe('calendar.guests');
        });

        it('clears back to no condition', async () => {
            card._pickerReady = true;
            card._condition = 'calendar.guests';
            await flush();

            card.shadowRoot!.querySelector('ha-entity-picker')!.dispatchEvent(
                new CustomEvent('value-changed', { detail: { value: null } })
            );
            await flush();

            expect(card._condition).toBe('');
        });

        it('only offers entities that can gate a PIN', async () => {
            card._pickerReady = true;
            await flush();

            const picker = card.shadowRoot!.querySelector('ha-entity-picker')! as unknown as {
                entityFilter: (state: { entity_id: string }) => boolean;
                includeDomains: readonly string[];
            };

            expect(picker.includeDomains).toContain('calendar');
            expect(picker.entityFilter({ entity_id: 'calendar.guests' })).toBe(true);
            expect(picker.entityFilter({ entity_id: 'light.kitchen' })).toBe(false);
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

        it('sends the condition when one was picked', async () => {
            card._name = 'Raman';
            card._condition = 'calendar.guests';
            await card._commit();

            expect(calls[0].data).toMatchObject({ condition: 'calendar.guests' });
        });

        it('omits the condition when none was picked', async () => {
            card._name = 'Raman';
            await card._commit();

            expect(calls[0].data).not.toHaveProperty('condition');
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
