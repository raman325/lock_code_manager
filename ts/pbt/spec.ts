/**
 * Bombadil specification for the Lock Code Manager card harness.
 *
 * Run via `yarn test:pbt`. Extractors run inside the browser; they must
 * walk shadow roots explicitly because Lit renders into (open) shadow DOM,
 * which document-level queries and innerText do not pierce.
 */
/* eslint-disable no-underscore-dangle -- window.__lcmHarness is the harness's fixed extractor surface */
import { always, eventually, now } from '@antithesishq/bombadil';
import { actions, extract } from '@antithesishq/bombadil/browser';

export * from '@antithesishq/bombadil/browser/defaults';

/** Recursively collect visible text across light and shadow DOM. */
function deepText(root: Element | ShadowRoot): string {
    let text = '';
    for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
            // Join with a separator so text nodes straddling a shadow
            // boundary can't fuse into a digit run that isn't really there.
            text += `${deepText(el.shadowRoot)}\n`;
        }
    }
    text += root.textContent ?? '';
    return text;
}

/** Recursively collect elements matching a selector across shadow roots. */
function deepQueryAll(root: Element | Document | ShadowRoot, selector: string): Element[] {
    const found = [...root.querySelectorAll(selector)];
    for (const el of root.querySelectorAll('*')) {
        if (el.shadowRoot) {
            found.push(...deepQueryAll(el.shadowRoot, selector));
        }
    }
    return found;
}

declare global {
    interface Window {
        __lcmHarness?: {
            model: {
                revision: number;
                slots: Record<
                    string,
                    {
                        active: boolean;
                        enabled: boolean;
                        inSync: boolean;
                        name: string;
                        pin: string;
                    }
                >;
                suspended: boolean;
            };
            secretPins: () => string[];
        };
    }
}

const maskedZoneText = extract((state) => {
    const zone = state.document.querySelector('#masked-zone');
    return zone ? deepText(zone) : '';
});

const lockCodesChipCount = extract((state) => {
    const zone = state.document.querySelector('#lock-codes-zone');
    return zone ? deepQueryAll(zone, '.slot-chip').length : -1;
});

const secretPins = extract((state) => state.window.__lcmHarness?.secretPins() ?? []);

const modelSlotCount = extract(
    (state) => Object.keys(state.window.__lcmHarness?.model.slots ?? {}).length
);

const modelNames = extract((state) =>
    Object.values(state.window.__lcmHarness?.model.slots ?? {}).map((slot) => slot.name)
);

const cardText = extract((state) => deepText(state.document.body));

/** Masked cards must never render a secret PIN as cleartext. */
export const maskedPinNeverLeaks = always(() =>
    secretPins.current.every(
        (pin) => !new RegExp(`(?<!\\d)${pin}(?!\\d)`).test(maskedZoneText.current)
    )
);

/** The lock-codes card renders exactly one chip per model slot. */
export const chipCountMatchesModel = always(() =>
    lockCodesChipCount.current === -1 ? true : lockCodesChipCount.current === modelSlotCount.current
);

/** Subscription liveness: pushed names eventually appear in the DOM. */
export const namesEventuallyRendered = always(() =>
    eventually(() => modelNames.current.every((name) => cardText.current.includes(name))).within(
        10,
        'seconds'
    )
);

const modelSuspended = extract((state) => state.window.__lcmHarness?.model.suspended ?? false);

const suspendedBannerVisible = extract((state) => {
    const zone = state.document.querySelector('#lock-codes-zone');
    return zone ? deepQueryAll(zone, '.suspended-banner').length > 0 : false;
});

/** Closest drivable "unavailable treatment": suspended state shows its banner. */
export const suspendedStateEventuallyShowsBanner = always(() =>
    now(() => modelSuspended.current).implies(
        eventually(() => suspendedBannerVisible.current || !modelSuspended.current).within(
            10,
            'seconds'
        )
    )
);

/** Click chaos-panel buttons and card-internal (shadow DOM) buttons. */
const clickablePoints = extract((state) => {
    const points: { name: string; x: number; y: number }[] = [];
    for (const el of deepQueryAll(state.document, 'button')) {
        const rect = el.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) {
            points.push({
                name: el.id || el.textContent?.trim().slice(0, 24) || 'button',
                x: rect.left + rect.width / 2,
                y: rect.top + rect.height / 2
            });
        }
    }
    return points;
});

export const clickButtons = actions(() =>
    clickablePoints.current.map(({ name, x, y }) => {
        return { Click: { name, point: { x, y } } };
    })
);
