/**
 * Bombadil harness: mounts lcm-slot and lcm-lock-codes with a scripted mock
 * hass. Plain JavaScript on purpose — no build step; served statically next
 * to the built bundle. The model below is the single source of truth; chaos
 * buttons mutate it and re-push through the subscription callbacks, the same
 * path the real websocket API uses.
 */

const CONFIG_ENTRY_ID = 'pbt_entry';
const LOCK_ENTITY_ID = 'lock.pbt_front_door';

const model = {
    revision: 0,
    suspended: false,
    slots: {
        1: { active: true, enabled: true, inSync: true, name: 'Alice', pin: '4921' },
        2: { active: false, enabled: false, inSync: true, name: 'Bob', pin: '8375' },
        3: { active: true, enabled: true, inSync: false, name: 'Carol', pin: '6104' }
    }
};

// Read-only window surface for spec extractors. Secret PINs deliberately
// live here (JavaScript only), never in the DOM: the no-leak property greps
// the rendered DOM for them.
window.__lcmHarness = {
    model,
    secretPins: () => Object.values(model.slots).map((slot) => slot.pin)
};

/** Subscriptions keyed by an opaque id -> {message, callback}. */
const subscriptions = new Map();
let nextSubscriptionId = 1;

function slotCardData(slotNum) {
    const slot = model.slots[slotNum];
    return {
        active: slot.active,
        conditions: {},
        config_entry_id: CONFIG_ENTRY_ID,
        config_entry_title: 'PBT Lock Manager',
        enabled: slot.enabled,
        entities: {
            active: `binary_sensor.slot_${slotNum}_active`,
            enabled: `switch.slot_${slotNum}_enabled`,
            name: `text.slot_${slotNum}_name`,
            pin: `text.slot_${slotNum}_pin`
        },
        locks: [
            {
                code: null,
                code_length: slot.pin.length,
                entity_id: LOCK_ENTITY_ID,
                in_sync: slot.inSync,
                name: 'Front Door',
                sync_status: slot.inSync ? 'in_sync' : 'out_of_sync'
            }
        ],
        name: slot.name,
        // Harness cards run code_display: 'masked' — the PIN value never
        // leaves the model; only its length does.
        pin: null,
        pin_length: slot.pin.length,
        slot_num: slotNum
    };
}

function lockCoordinatorData() {
    return {
        lock_entity_id: LOCK_ENTITY_ID,
        lock_name: 'Front Door',
        ...(model.suspended ? { sync_status: 'suspended' } : {}),
        slots: Object.entries(model.slots).map(([slotNum, slot]) => ({
            active: slot.active,
            code: null,
            code_length: slot.pin.length,
            config_entry_id: CONFIG_ENTRY_ID,
            config_entry_title: 'PBT Lock Manager',
            in_sync: slot.inSync,
            // ts/types.ts LockCoordinatorSlotData names this `managed`
            // (lock-codes-card.ts reads `slot.managed` throughout); the
            // original draft used `is_managed`, which the card would have
            // silently ignored.
            managed: true,
            name: slot.name,
            slot: Number(slotNum)
        }))
    };
}

function payloadFor(message) {
    if (message.type === 'lock_code_manager/subscribe_code_slot') {
        return model.slots[message.slot] ? slotCardData(message.slot) : null;
    }
    if (message.type === 'lock_code_manager/subscribe_lock_codes') {
        return lockCoordinatorData();
    }
    return null;
}

function pushAll() {
    model.revision += 1;
    for (const { callback, message } of subscriptions.values()) {
        const payload = payloadFor(message);
        if (payload !== null) {
            callback(payload);
        }
    }
}

const mockHass = {
    callService: (domain, service, data) => {
        // Card-initiated edits loop back through the model like the real
        // backend would.
        pushAll();
        return Promise.resolve({ domain, data, service });
    },
    callWS: () => Promise.resolve({}),
    connection: {
        subscribeMessage: (callback, message) => {
            const id = nextSubscriptionId;
            nextSubscriptionId += 1;
            subscriptions.set(id, { callback, message });
            const payload = payloadFor(message);
            if (payload !== null) {
                callback(payload);
            }
            return Promise.resolve(() => subscriptions.delete(id));
        }
    },
    states: {
        [LOCK_ENTITY_ID]: {
            attributes: { friendly_name: 'Front Door' },
            entity_id: LOCK_ENTITY_ID,
            state: 'locked'
        }
    }
};

function mountCards() {
    for (const slotNum of Object.keys(model.slots)) {
        const card = document.createElement('lcm-slot');
        card.setConfig({
            code_display: 'masked',
            config_entry_id: CONFIG_ENTRY_ID,
            slot: Number(slotNum),
            type: 'custom:lcm-slot'
        });
        card.hass = mockHass;
        document.getElementById('masked-zone').appendChild(card);
    }
    const lockCodes = document.createElement('lcm-lock-codes');
    lockCodes.setConfig({
        code_display: 'masked',
        lock_entity_id: LOCK_ENTITY_ID,
        type: 'custom:lcm-lock-codes'
    });
    lockCodes.hass = mockHass;
    document.getElementById('lock-codes-zone').appendChild(lockCodes);
}

function randomPin() {
    return String(Math.floor(1000 + Math.random() * 9000));
}

function randomSlotNum() {
    const nums = Object.keys(model.slots);
    return Number(nums[Math.floor(Math.random() * nums.length)]);
}

const chaosHandlers = {
    'chaos-add-slot': () => {
        const next = Math.max(0, ...Object.keys(model.slots).map(Number)) + 1;
        if (next <= 6) {
            model.slots[next] = {
                active: true,
                enabled: true,
                inSync: false,
                name: `User ${next}`,
                pin: randomPin()
            };
        }
    },
    'chaos-clear-slot': () => {
        const nums = Object.keys(model.slots);
        if (nums.length > 1) {
            delete model.slots[randomSlotNum()];
        }
    },
    'chaos-external-pin': () => {
        model.slots[randomSlotNum()].pin = randomPin();
    },
    'chaos-rename': () => {
        const slot = model.slots[randomSlotNum()];
        slot.name = `Renamed ${model.revision}`;
    },
    'chaos-toggle-active': () => {
        const slot = model.slots[randomSlotNum()];
        slot.active = !slot.active;
    },
    'chaos-toggle-enabled': () => {
        const slot = model.slots[randomSlotNum()];
        slot.enabled = !slot.enabled;
    },
    'chaos-toggle-suspended': () => {
        model.suspended = !model.suspended;
    },
    'chaos-toggle-sync': () => {
        const slot = model.slots[randomSlotNum()];
        slot.inSync = !slot.inSync;
    }
};

for (const [id, handler] of Object.entries(chaosHandlers)) {
    document.getElementById(id).addEventListener('click', () => {
        handler();
        pushAll();
    });
}

mountCards();
