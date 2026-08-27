"""Test event platform."""

import logging
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.event import ATTR_EVENT_TYPE, ATTR_EVENT_TYPES
from homeassistant.const import ATTR_NAME, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_CODE_SLOT,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFIG_ENTRY_TITLE,
    ATTR_CREDENTIAL_TYPE,
    ATTR_OPERATION,
    ATTR_SLOT_FIELD,
    ATTR_SOURCE,
    ATTR_TARGET,
    DOMAIN,
    EVENT_CREDENTIAL_USED,
    SERVICE_USE_CREDENTIAL,
)
from custom_components.lock_code_manager.domain.credentials import (
    MANAGED_CREDENTIAL_TYPES,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.domain.events import (
    BUS_EVENT_CREDENTIAL_USED,
    CredentialOperation,
)
from custom_components.lock_code_manager.providers import BaseLock

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_EVENT_ENTITY,
    SLOT_2_EVENT_ENTITY,
    MockLCMLock,
)

_LOGGER = logging.getLogger(__name__)

# A keypad and a gate Lock Code Manager manages neither of. A use entered on
# the one and acting on the other is a use no lock in the entry observed, and
# neither entity is something the entry could ever have named.
EXTERNAL_KEYPAD = "sensor.side_gate_keypad"
EXTERNAL_TARGET = "cover.side_gate"

# Every attribute the lock-shaped payload used to put on the entity. None of
# them are the entity's to publish any more, and since 6.0 the lock-shaped
# bus event that carried them is gone too -- so nothing publishes them.
RETIRED_ATTRIBUTES = (
    "code_slot_name",
    "action_text",
    "notification_source",
    "from",
    "to",
    "state",
    "entity_id",
    "device_id",
    "extra_data",
    "lock_code_manager_config_entry_id",
    "unsupported_locks",
)


async def _use_credential(
    hass: HomeAssistant, config_entry, code: str, target: str
) -> None:
    """Report a use through the action, which is the only non-lock path in."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_USE_CREDENTIAL,
        {
            "config_entry_id": config_entry.entry_id,
            ATTR_CODE: code,
            ATTR_SOURCE: EXTERNAL_KEYPAD,
            ATTR_TARGET: target,
        },
        blocking=True,
    )


async def test_the_recorded_attributes_are_the_unified_payload(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A recorded use publishes the unified payload and nothing else.

    The attribute set is this entity's contract with every template and
    blueprint reading it, so it is asserted whole rather than key by key --
    one silently added or dropped is somebody's notification text breaking.
    """
    state = hass.states.get(SLOT_2_EVENT_ENTITY)
    assert state
    assert state.state == STATE_UNKNOWN

    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.async_fire_code_slot_event(2, False)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN
    assert {
        key: value
        for key, value in state.attributes.items()
        if key not in (ATTR_EVENT_TYPES, "friendly_name")
    } == {
        # The event type is the kind of credential, not a place.
        ATTR_EVENT_TYPE: CredentialType.PIN,
        ATTR_NAME: "test2",
        ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
        ATTR_CONFIG_ENTRY_TITLE: lock_code_manager_config_entry.title,
        # A lock that observed the use is both ends of it.
        ATTR_SOURCE: LOCK_1_ENTITY_ID,
        ATTR_TARGET: LOCK_1_ENTITY_ID,
        ATTR_CREDENTIAL_TYPE: CredentialType.PIN,
        ATTR_OPERATION: CredentialOperation.UNLOCK,
        ATTR_CODE_SLOT: 2,
        ATTR_SLOT_FIELD: EVENT_CREDENTIAL_USED,
    }
    for retired in RETIRED_ATTRIBUTES:
        assert retired not in state.attributes


class MockLCMLockWithCredentialTypes(MockLCMLock):
    """A lock that answers the capability probe, advertising RFID as well."""

    @property
    def supports_native_users(self) -> bool:
        """Take the setup path that probes capabilities."""
        return True

    async def async_get_capabilities(self) -> LockCapabilities:
        """Advertise a reader Lock Code Manager cannot write to."""
        return LockCapabilities(
            supports_user_management=True,
            max_users=10,
            max_user_name_length=16,
            credential_types={
                credential_type: CredentialTypeCapability(
                    num_slots=10, min_length=4, max_length=8, supports_learn=False
                )
                for credential_type in (CredentialType.PIN, CredentialType.RFID)
            },
        )


async def test_event_types_names_credential_kinds_and_never_locks(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    The vocabulary says what kind of credential, never where it was used.

    Home Assistant refuses an event type an entity did not declare, so a
    vocabulary naming the entry's locks makes a use against anything else
    impossible to record -- which is what this replaced. The kind is the
    axis that actually distinguishes one recorded use from another; where
    it happened rides in ``target``.

    These locks answer no capability probe, so this is also the floor:
    ``MANAGED_CREDENTIAL_TYPES`` alone, which is what keeps the vocabulary
    from being empty while a probe has not run.
    """
    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state

    assert state.attributes[ATTR_EVENT_TYPES] == sorted(MANAGED_CREDENTIAL_TYPES)
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        assert lock_entity_id not in state.attributes[ATTR_EVENT_TYPES]


async def test_event_types_unions_what_the_entrys_locks_advertise(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    A kind only the lock can report is still one this entity can record.

    What Lock Code Manager can write and what a lock can report are
    different sets. A lock with its own card reader reports RFID uses that
    nothing here ever wrote, and refusing to name them would make them
    unrecordable -- so the managed set is a floor under the union, not a
    cap on it.
    """
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithCredentialTypes},
    ):
        config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Credential Types"
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert state
        assert state.attributes[ATTR_EVENT_TYPES] == [
            CredentialType.PIN,
            CredentialType.RFID,
        ]

        await hass.config_entries.async_unload(config_entry.entry_id)


async def test_a_use_of_an_unnamed_kind_widens_the_vocabulary(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A kind that arrives anyway is recorded, not dropped.

    ``_trigger_event`` raises on an undeclared type and this runs on a bus
    callback whose exceptions Home Assistant swallows, so a use whose kind
    the capability probe has not admitted yet would vanish without a trace.
    A use that happened is better evidence of what is possible here than a
    probe that has not finished, so it widens the vocabulary instead.
    """
    before = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert before
    assert CredentialType.FINGERPRINT not in before.attributes[ATTR_EVENT_TYPES]

    hass.bus.async_fire(
        BUS_EVENT_CREDENTIAL_USED,
        event_data={
            ATTR_NAME: "test1",
            ATTR_CONFIG_ENTRY_ID: lock_code_manager_config_entry.entry_id,
            ATTR_CONFIG_ENTRY_TITLE: lock_code_manager_config_entry.title,
            ATTR_SOURCE: EXTERNAL_KEYPAD,
            ATTR_TARGET: EXTERNAL_TARGET,
            ATTR_CREDENTIAL_TYPE: CredentialType.FINGERPRINT,
            ATTR_OPERATION: CredentialOperation.UNKNOWN,
        },
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPE] == CredentialType.FINGERPRINT
    assert CredentialType.FINGERPRINT in state.attributes[ATTR_EVENT_TYPES]


async def test_a_lock_observed_use_is_recorded_once(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    One use the lock observed writes one state.

    Two bus events fire for it -- the deprecated lock-shaped one and the
    unified one. Recording both would fire every automation on this entity
    twice, which is why only one of them is subscribed.
    """
    states: list[State] = []

    @callback
    def _collect(event: Event[EventStateChangedData]) -> None:
        if (new_state := event.data["new_state"]) is not None:
            states.append(new_state)

    unsub = async_track_state_change_event(hass, [SLOT_2_EVENT_ENTITY], _collect)

    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.async_fire_code_slot_event(2, False)
    await hass.async_block_till_done()
    unsub()

    assert len(states) == 1
    assert states[0].attributes[ATTR_TARGET] == LOCK_1_ENTITY_ID
    assert states[0].attributes[ATTR_CODE_SLOT] == 2


async def test_a_use_against_something_that_is_not_a_lock_is_recorded(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    A use acting on a cover records on the user's entity, target and all.

    This is the whole point of the fixed vocabulary. The target is a
    credential's own business -- a gate, an alarm panel, a door controller
    with no integration -- and none of them can ever be a lock this entry
    manages, so a lock-shaped vocabulary dropped every one of them.
    """
    before = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert before
    assert before.state == STATE_UNKNOWN

    await _use_credential(hass, lock_code_manager_config_entry, "1234", EXTERNAL_TARGET)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN
    assert state.attributes[ATTR_EVENT_TYPE] == CredentialType.PIN
    assert state.attributes[ATTR_TARGET] == EXTERNAL_TARGET
    assert state.attributes[ATTR_SOURCE] == EXTERNAL_KEYPAD
    assert state.attributes[ATTR_NAME] == "test1"


async def test_a_use_is_recorded_for_the_user_it_belongs_to_alone(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Dropping the target check does not let another user's use through.

    The unified event names the person rather than the slot they occupy, so
    the name is the entity's only means of telling its own uses apart, and
    it is now the only check left besides the config entry.
    """
    before = hass.states.get(SLOT_2_EVENT_ENTITY)
    assert before

    await _use_credential(hass, lock_code_manager_config_entry, "1234", EXTERNAL_TARGET)
    await hass.async_block_till_done()

    assert hass.states.get(SLOT_2_EVENT_ENTITY) == before
    assert hass.states.get(SLOT_1_EVENT_ENTITY).state != STATE_UNKNOWN


@pytest.mark.parametrize(
    ("to_locked", "operation"),
    [
        pytest.param(False, CredentialOperation.UNLOCK, id="unlocked-by-code"),
        pytest.param(True, CredentialOperation.LOCK, id="locked-by-code"),
        pytest.param(None, CredentialOperation.UNKNOWN, id="unclassified"),
    ],
)
async def test_uses_that_are_not_an_unlock_are_recorded_and_say_so(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    to_locked: bool | None,
    operation: CredentialOperation,
):
    """
    Every direction a lock can report is recorded, and named.

    All three are credential uses and only one is an unlock. The entity used
    to take these off the deprecated event through a filter that demanded a
    transition to ``unlocked``, so a lock-by-code and every notification a
    provider passes through with ``to_locked=None`` -- Matter, ZHA and
    Z-Wave JS all have one -- were recorded nowhere at all.

    Recording them all is only half the answer: a consumer that must not
    treat locking up as an entry needs to be able to tell, which is what
    ``operation`` is for. ``unknown`` is a real answer, not a gap -- the
    provider saw a use it could not classify.
    """
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.async_fire_code_slot_event(1, to_locked)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN
    assert state.attributes[ATTR_TARGET] == LOCK_1_ENTITY_ID
    assert state.attributes[ATTR_NAME] == "test1"
    assert state.attributes[ATTR_OPERATION] == operation


async def test_a_reported_use_never_claims_to_know_what_the_device_did(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    ``use_credential`` reports ``unknown``, and must not guess.

    The caller said a credential was used, not what happened next. Lock Code
    Manager never actuates a lock, so it has no basis to claim an unlock --
    stating one would put a fact in the payload nobody observed, and a
    limiter counting entries would spend a use on it.
    """
    await _use_credential(hass, lock_code_manager_config_entry, "1234", EXTERNAL_TARGET)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_1_EVENT_ENTITY)
    assert state
    assert state.attributes[ATTR_OPERATION] == CredentialOperation.UNKNOWN
    assert state.attributes[ATTR_CREDENTIAL_TYPE] == CredentialType.PIN


class MockLCMLockNoEvents(MockLCMLock):
    """Mock lock that doesn't support code slot events."""

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return False


async def test_available_when_no_lock_reports_code_slot_events(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    An entry of locks that report nothing still has a usable event entity.

    Availability is now the shared per-slot rule -- at least one of the
    entry's locks is reachable -- because nothing about this entity is
    lock-capability shaped any more. Gating on the capability is what forced
    providers with nothing to report to claim otherwise just to keep the
    entity alive.
    """
    with patch(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockNoEvents},
    ):
        config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title No Events 2"
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        state = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert state
        assert state.state != STATE_UNAVAILABLE
        assert state.attributes[ATTR_EVENT_TYPES] == sorted(MANAGED_CREDENTIAL_TYPES)

        lock: BaseLock = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        lock.async_fire_code_slot_event(1, False)
        await hass.async_block_till_done()

        recorded = hass.states.get(SLOT_1_EVENT_ENTITY)
        assert recorded.state != STATE_UNKNOWN
        assert recorded.attributes[ATTR_TARGET] == LOCK_1_ENTITY_ID

        await hass.config_entries.async_unload(config_entry.entry_id)


async def test_available_even_when_every_lock_is_not(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    This entity does NOT follow its entry's locks, unlike every other slot entity.

    A use recorded here need not have come from a lock: ``use_credential``
    carries uses this integration cannot observe and refuses nothing while
    the locks are unreachable, so following them would blank the entity
    exactly when it is holding a use nothing else can report.

    This used to go ``unavailable``, and the shipped blueprints grew guards
    against the ``unavailable`` -> timestamp transition that produced. Those
    guards stay -- an entity can still be blanked on reload or removal --
    but the transition they were written for no longer happens at all. That
    a recovering lock now notifies nobody is pinned by
    ``test_lock_recovery_notifies_nobody`` rather than argued from here.
    """
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        hass.states.async_set(lock_entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert hass.states.get(SLOT_1_EVENT_ENTITY).state != STATE_UNAVAILABLE


async def test_the_event_says_which_kind_of_credential_was_used(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Only PIN is exercised today, so the point is that it is stated at all.

    A consumer that reads the kind now keeps working when a second kind
    arrives, rather than having assumed there was only ever one.
    """
    events: list[Event] = []
    hass.bus.async_listen(BUS_EVENT_CREDENTIAL_USED, events.append)

    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.async_fire_code_slot_event(1, False)
    await hass.async_block_till_done()

    assert events
    assert events[0].data[ATTR_CREDENTIAL_TYPE] == CredentialType.PIN


async def test_event_entity_is_named(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    entity_registry: er.EntityRegistry,
) -> None:
    """
    The event says what it is, not just whose it is.

    It used to set ``_attr_name = None``, which with ``has_entity_name``
    means "this entity IS the device" -- so it was called "Mock Title
    test1" and nothing more, indistinguishable from the device it sits on.
    That made sense when the device was a slot and the event was the only
    thing on it; the device is a user now, and the event is one of several
    things about them.

    Named for the credential rather than the PIN because the entity id is
    derived from the name, and a PIN is one credential among the several
    this is growing to cover. Renaming it later would move the id.
    """
    entity = entity_registry.async_get(SLOT_1_EVENT_ENTITY)

    assert entity is not None
    assert entity.original_name == "Credential used"
