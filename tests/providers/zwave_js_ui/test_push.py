"""Tests for the zwave-js-ui node subscription, push updates, and keypad events."""

from __future__ import annotations

import json
from logging import ERROR
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js_ui import ZWaveJSUILock

from .conftest import (
    ZUI_NODE_TOPIC,
    ZWaveJSUIApiResponder,
    async_discover_zui_lock,
    build_zui_lock,
)

MODULE = "custom_components.lock_code_manager.providers.zwave_js_ui"

# The two topic shapes a gateway publishes the same value under: VALUEID
# spells the command class and endpoint numerically, NAMED spells both out.
USER_CODE_VALUEID = "99/0/userCode"
USER_CODE_NAMED = "user_code/endpoint_0/userCode"
USER_ID_STATUS_VALUEID = "99/0/userIdStatus"
USER_ID_STATUS_NAMED = "user_code/endpoint_0/userIdStatus"
KEYPAD_UNLOCK_VALUEID = "113/0/Access_Control/Keypad_unlock_operation"
KEYPAD_UNLOCK_NAMED = "notification/endpoint_0/Access_Control/Keypad_unlock_operation"
KEYPAD_LOCK_NAMED = "notification/endpoint_0/Access_Control/Keypad_lock_operation"


def fire_node_value(hass: HomeAssistant, suffix: str, payload: str) -> None:
    """Publish a value under this lock's node topic the way the gateway does."""
    async_fire_mqtt_message(hass, f"{ZUI_NODE_TOPIC}/{suffix}", payload)


def wrapped(value: Any) -> str:
    """Wrap a value in the gateway's ``{time, value}`` payload envelope."""
    return json.dumps({"time": 1700000000000, "value": value})


def keypad_payload(user_id: Any) -> str:
    """Build a keypad notification payload carrying an embedded User Code Report."""
    return wrapped({"userId": user_id})


@pytest.fixture
async def zui_lock_subscribed(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> ZWaveJSUILock:
    """
    A provider subscribed to its node topic through the real setup path.

    ``desired_credential`` is seeded empty because that is what a lock with no
    Lock Code Manager configuration wants everywhere, and a bare MagicMock
    would answer every ``is_present`` truthily -- silently arming the
    stale-AVAILABLE guard in every test that never mentions it.
    """
    await zui_lock_provider.async_setup(MagicMock())
    await hass.async_block_till_done()
    zui_lock_provider.coordinator = MagicMock()
    zui_lock_provider.coordinator.desired_credential.return_value = (
        SlotCredential.empty()
    )
    return zui_lock_provider


async def test_push_capabilities_follow_the_node_topic(
    hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
) -> None:
    """
    Both capabilities are exactly "is there a node topic to subscribe to".

    They ride the same subscription -- code slot events are Notification
    Command Class publications under the node topic -- so they cannot
    disagree, and neither can be a constant: a MANUAL gateway pointed at a
    custom state topic gives us nothing to subscribe to, and advertising push
    anyway would make the coordinator disable polling for a lock that has no
    push either.
    """
    lock = zui_lock_provider
    assert lock.supports_push is True
    assert lock.supports_code_slot_events is True

    with patch.object(ZWaveJSUILock, "_prefix_and_node_topic", return_value=None):
        assert lock.supports_push is False
        assert lock.supports_code_slot_events is False


class TestUserCodeValues:
    """User Code Command Class value publications on the node topic."""

    @pytest.mark.parametrize(
        "property_path",
        [USER_CODE_VALUEID, USER_CODE_NAMED],
        ids=["valueid", "named"],
    )
    @pytest.mark.parametrize(
        "payload",
        ["1234", wrapped("1234")],
        ids=["raw", "time_value"],
    )
    async def test_a_published_code_confirms_the_slot(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        property_path: str,
        payload: str,
    ) -> None:
        """
        Both gateway spellings and both payload shapes reach the coordinator.

        The gateway's topic style and payload type are per-install settings
        nobody changes for us, so a provider that understands only one of
        each silently never pushes on half the installs out there.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{property_path}/3", payload)
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known("1234")}
        )

    @pytest.mark.parametrize(
        ("payload", "expected_code"),
        [
            # A raw-payload gateway publishes the bare code, so an all-digit
            # one is JSON's idea of a number by the time it is unwrapped.
            pytest.param("1234", "1234", id="bare_number"),
            pytest.param(wrapped(1234), "1234", id="wrapped_number"),
            # No JSON number form has a leading zero, so this arrives as the
            # text it was published as -- and the zero has to survive.
            pytest.param("0123", "0123", id="leading_zero"),
            pytest.param(wrapped("0123"), "0123", id="wrapped_leading_zero"),
        ],
    )
    async def test_a_numeric_code_keeps_the_digits_it_was_published_with(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        payload: str,
        expected_code: str,
    ) -> None:
        """
        A code read back as a number must compare equal to the one written.

        Sync compares the coordinator's value against the configured PIN, so
        a code that round-trips as ``1234`` when ``"1234"`` was written --
        or loses a leading zero -- makes sync rewrite the slot forever.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", payload)
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known(expected_code)}
        )

    @pytest.mark.parametrize(
        "property_path",
        [USER_ID_STATUS_VALUEID, USER_ID_STATUS_NAMED],
        ids=["valueid", "named"],
    )
    @pytest.mark.parametrize("payload", ["0", wrapped(0)], ids=["raw", "time_value"])
    async def test_an_available_status_confirms_the_slot_empty(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        property_path: str,
        payload: str,
    ) -> None:
        """Available is the one status that says the slot holds nothing."""
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{property_path}/4", payload)
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {4: SlotCredential.empty()}
        )

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("1", id="enabled"),
            pytest.param(wrapped(2), id="disabled"),
            # ``True == 1`` in Python, so an unguarded comparison against
            # Available (0) is safe but the Enabled path is not -- this is the
            # same bool trap the api projection guards, kept consistent here.
            pytest.param(wrapped(True), id="boolean_true"),
            pytest.param(wrapped(False), id="boolean_false"),
        ],
    )
    async def test_a_status_that_is_not_available_confirms_nothing(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        payload: str,
    ) -> None:
        """
        A status alone cannot say what the code is, so it pushes nothing.

        ``False`` matters on its own: it equals Available (0) in Python, and
        taking it for one would report an occupied slot as confirmed-empty.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/5", payload)
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_not_called()

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("", id="retained_clear"),
            pytest.param(wrapped(None), id="null"),
            pytest.param(wrapped("   "), id="blank"),
            pytest.param(wrapped({"data": [1, 2]}), id="buffer_object"),
            # ``True`` is an int in Python, so an unguarded coercion would
            # push a Personal Identification Number of ``"True"``.
            pytest.param(wrapped(True), id="boolean"),
            pytest.param(wrapped(12.5), id="float"),
            # A lock withholding its codes publishes one asterisk per digit.
            # Confirmed as the code, it never matches the configured PIN, so
            # sync reprograms the slot on every tick forever.
            pytest.param(wrapped("****"), id="masked"),
            pytest.param("****", id="masked_raw"),
        ],
    )
    async def test_a_code_that_is_not_a_usable_string_confirms_nothing(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        payload: str,
    ) -> None:
        """
        An empty userCode cannot tell a withheld code from a cleared slot.

        Reading it as empty would tell sync the code is gone and allocation
        the index is free, so a lock that withholds codes would be
        reprogrammed on every publication.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_CODE_VALUEID}/6", payload)
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_not_called()


class TestStaleAvailable:
    """The AVAILABLE status a lock re-sends after a code was written."""

    async def test_available_is_ignored_when_a_pin_is_expected(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """
        A slot Lock Code Manager wants a code on never reads empty from a push.

        Some locks re-announce AVAILABLE after a successful write. Taken at
        face value it tells sync the slot was cleared, sync rewrites it, the
        lock re-announces, and the loop never ends. Same hardware signal, same
        guard as the zwave_js provider applies.
        """
        lock = zui_lock_subscribed
        lock.coordinator.desired_credential.return_value = SlotCredential.known("1234")

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/4", wrapped(0))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_not_called()

    async def test_available_confirms_empty_when_no_pin_is_expected(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """The guard is about a contradicted clear, not about clears at all."""
        lock = zui_lock_subscribed
        lock.coordinator.desired_credential.return_value = SlotCredential.empty()

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/4", wrapped(0))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {4: SlotCredential.empty()}
        )

    async def test_available_confirms_empty_without_a_coordinator(
        self, hass: HomeAssistant, zui_lock_provider: ZWaveJSUILock
    ) -> None:
        """
        With nothing to ask, the observation stands rather than being dropped.

        A provider answering queries outside an entry has no coordinator, so a
        guard that treated "cannot ask" as "a PIN is expected" would suppress
        every clear it ever saw.
        """
        lock = zui_lock_provider
        await lock.async_setup(MagicMock())
        await hass.async_block_till_done()
        assert lock.coordinator is None

        with patch.object(ZWaveJSUILock, "_confirm_slot") as confirm:
            fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/4", wrapped(0))
            await hass.async_block_till_done()

        confirm.assert_called_once_with(4, SlotCredential.empty())


class TestStatusGatedCodes:
    """A published code is only a confirmation when the slot is enabled."""

    async def test_a_code_after_a_disabled_status_confirms_nothing(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """
        A Disabled slot keeps its digits, and they are not an active code.

        The poll projection already requires ENABLED before it reports a code,
        so a push that confirmed one regardless would make the same slot read
        in-sync or unreadable depending on which transport spoke last.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped(2))
        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", wrapped("1234"))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_not_called()

    async def test_a_code_before_any_status_confirms_the_slot(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """
        Never having heard a status is not the same as having heard a bad one.

        A gateway that publishes a slot's code and never its status would
        otherwise go permanently unconfirmed, so the unknown case keeps the
        pre-existing behaviour and the poll corrects it if it was wrong.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", wrapped("1234"))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known("1234")}
        )

    async def test_a_disabled_slot_does_not_gate_another_slot(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """Statuses are per slot; one disabled user must not mute the rest."""
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped(2))
        fire_node_value(hass, f"{USER_CODE_VALUEID}/4", wrapped("5678"))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {4: SlotCredential.known("5678")}
        )

    async def test_re_enabling_a_slot_re_admits_its_code(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """The gate follows the latest status, not the first one seen."""
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped(2))
        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped(1))
        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", wrapped("1234"))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known("1234")}
        )

    async def test_an_uninterpretable_status_does_not_gate(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """
        A status nobody can read tells us nothing, including "not enabled".

        Recording it would mute the slot's codes until the gateway happened to
        republish a real status, which for a retained topic may be never.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped(True))
        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped("nonsense"))
        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", wrapped("1234"))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known("1234")}
        )

    async def test_teardown_forgets_the_tracked_statuses(
        self, hass: HomeAssistant, zui_lock_subscribed: ZWaveJSUILock
    ) -> None:
        """
        A released subscription's statuses describe a node we stopped watching.

        Keeping them across a teardown would let a status seen before an
        unload gate codes published after the resubscribe, with nothing in
        between to correct it.
        """
        lock = zui_lock_subscribed

        fire_node_value(hass, f"{USER_ID_STATUS_VALUEID}/3", wrapped(2))
        await hass.async_block_till_done()
        lock.teardown_push_subscription()
        await lock._async_ensure_node_subscription()

        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", wrapped("1234"))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known("1234")}
        )


class TestKeypadEvents:
    """Notification Command Class keypad operations on the node topic."""

    @pytest.mark.parametrize(
        ("property_path", "expected_to_locked"),
        [
            pytest.param(KEYPAD_UNLOCK_NAMED, False, id="named_unlock"),
            pytest.param(KEYPAD_LOCK_NAMED, True, id="named_lock"),
            pytest.param(KEYPAD_UNLOCK_VALUEID, False, id="valueid_unlock"),
        ],
    )
    async def test_a_keypad_operation_fires_a_code_slot_event(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        property_path: str,
        expected_to_locked: bool,
    ) -> None:
        """
        The event names the slot that operated the lock and which way it went.

        ``to_locked`` is what the blueprint automations branch on, so a lock
        operation reported as an unlock (or the reverse) is worse than no
        event at all.
        """
        fired = MagicMock()
        zui_lock_subscribed.async_fire_code_slot_event = fired

        fire_node_value(hass, property_path, keypad_payload(3))
        await hass.async_block_till_done()

        fired.assert_called_once_with(code_slot=3, to_locked=expected_to_locked)

    @pytest.mark.parametrize(
        "payload",
        [
            # Raw-buffer notification parameters serialize to a hex string
            # rather than a parsed User Code Report.
            pytest.param(wrapped("0x0103"), id="hex_string"),
            pytest.param(wrapped({"eventType": 5}), id="dict_without_user_id"),
            pytest.param(wrapped({"userId": "front door"}), id="non_numeric_user_id"),
            pytest.param(wrapped({"userId": True}), id="boolean_user_id"),
            pytest.param(wrapped(None), id="null"),
            pytest.param("", id="retained_clear"),
        ],
    )
    async def test_a_keypad_payload_without_a_slot_fires_nothing(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        payload: str,
    ) -> None:
        """An event nobody can attribute to a slot is dropped, not guessed at."""
        fired = MagicMock()
        zui_lock_subscribed.async_fire_code_slot_event = fired

        fire_node_value(hass, KEYPAD_UNLOCK_NAMED, payload)
        await hass.async_block_till_done()

        fired.assert_not_called()

    async def test_a_non_keypad_notification_fires_nothing(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
    ) -> None:
        """
        Access Control carries far more than keypad operations.

        Manual operations, RF operations, and lock jams all publish under the
        same label; only the two keypad events name a code slot.
        """
        fired = MagicMock()
        zui_lock_subscribed.async_fire_code_slot_event = fired

        for suffix in (
            "notification/endpoint_0/Access_Control/Manual_lock_operation",
            "notification/endpoint_0/Home_Security/Intrusion",
            # Both halves have to match: an event label alone does not say
            # which notification type published it.
            "notification/endpoint_0/Home_Security/Keypad_unlock_operation",
        ):
            fire_node_value(hass, suffix, keypad_payload(3))
        await hass.async_block_till_done()

        fired.assert_not_called()


class TestForeignNodeTraffic:
    """The node topic carries every command class the node publishes."""

    @pytest.mark.parametrize(
        "suffix",
        [
            pytest.param("128/0/level", id="battery_value"),
            pytest.param("battery/endpoint_0/level", id="battery_named"),
            pytest.param("99/0/userCode/3/extra", id="too_many_segments"),
            pytest.param("99/1/userCode/3", id="wrong_endpoint_valueid"),
            pytest.param("user_code/endpoint_1/userCode/3", id="wrong_endpoint_named"),
            pytest.param("99/0/userCode/nope", id="non_numeric_slot"),
            pytest.param("99/0/somethingElse/3", id="unknown_property"),
            pytest.param(
                "113/1/Access_Control/Keypad_unlock_operation", id="notif_ep1"
            ),
        ],
    )
    async def test_traffic_that_is_not_ours_is_ignored(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        caplog: pytest.LogCaptureFixture,
        suffix: str,
    ) -> None:
        """
        Anything that is not a slot value or a keypad event passes silently.

        The wildcard subscription sees the node's whole value tree, so a
        classifier that is loose about the shape would push credential state
        derived from a battery level. Silently is the whole contract: this
        runs on every message the node emits, and a shape it merely fails to
        destructure would raise inside the MQTT callback rather than be
        ignored -- invisible to the coordinator, and a traceback per message.
        """
        lock = zui_lock_subscribed
        fired = MagicMock()
        zui_lock_subscribed.async_fire_code_slot_event = fired

        fire_node_value(hass, suffix, keypad_payload(3))
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_not_called()
        fired.assert_not_called()
        assert [record for record in caplog.records if record.levelno >= ERROR] == []

    async def test_a_message_outside_the_subscribed_node_is_ignored(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
    ) -> None:
        """
        Only topics under this node's own topic are ours to classify.

        The handler is reachable from any subscription Home Assistant routes
        to it, and a sibling node's topic shares every segment after the node
        topic -- classifying one would push another lock's codes into this
        lock's coordinator.

        ``nodeID_200`` is the trap: it starts with ``nodeID_20`` and, read at
        this node's offset, its value path lines up segment for segment. Only
        matching the separator too tells the two apart.
        """
        lock = zui_lock_subscribed

        lock._process_node_message("zwave/nodeID_21/99/0/userCode/3", b"1234")
        lock._process_node_message("zwave/nodeID_200/99/0/userCode/3", b"1234")
        await hass.async_block_till_done()

        lock.coordinator.push_update.assert_not_called()

    async def test_nothing_is_classified_before_the_subscription_exists(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """Without a resolved node topic there is no relative path to read."""
        lock = zui_lock_provider
        lock.coordinator = MagicMock()

        lock._process_node_message(f"{ZUI_NODE_TOPIC}/99/0/userCode/3", b"1234")

        lock.coordinator.push_update.assert_not_called()


class TestNodeSubscriptionLifecycle:
    """Subscribe, drift, and teardown of the node-topic wildcard subscription."""

    async def test_setup_subscribes_to_the_node_wildcard(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """Setup subscribes before the coordinator's first poll can run."""
        lock = zui_lock_provider
        with patch(f"{MODULE}.async_subscribe", new_callable=AsyncMock) as subscribe:
            await lock.async_setup(MagicMock())

        assert subscribe.call_args.args[1] == f"{ZUI_NODE_TOPIC}/#"
        assert lock._subscribed_node_topic == ZUI_NODE_TOPIC
        assert len(lock._push_unsubs) == 1

    async def test_re_ensuring_the_same_topic_does_not_resubscribe(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
    ) -> None:
        """
        The poll and the reconnect path both re-ensure; neither may pile up.

        A second subscription would deliver every message twice, and the
        first one's unsub handle would be dropped on the floor.
        """
        lock = zui_lock_subscribed
        with patch(f"{MODULE}.async_subscribe", new_callable=AsyncMock) as subscribe:
            await lock._async_ensure_node_subscription()
            lock.setup_push_subscription()
            await hass.async_block_till_done()

        subscribe.assert_not_called()
        assert len(lock._push_unsubs) == 1

    async def test_a_renamed_node_topic_moves_the_subscription(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
    ) -> None:
        """
        A gateway rename republishes discovery, and the subscription follows.

        Nothing disconnects when a node is renamed in zwave-js-ui, so without
        this the provider stays subscribed to a topic that has gone quiet and
        push silently stops working.
        """
        lock = zui_lock_subscribed
        renamed_node_topic = "zwave/hallway/front_door"
        await async_discover_zui_lock(hass, node_segment="hallway/front_door")

        await lock._async_ensure_node_subscription()

        assert lock._subscribed_node_topic == renamed_node_topic
        assert len(lock._push_unsubs) == 1

        async_fire_mqtt_message(hass, f"{renamed_node_topic}/99/0/userCode/3", "1234")
        await hass.async_block_till_done()
        lock.coordinator.push_update.assert_called_once_with(
            {3: SlotCredential.known("1234")}
        )

    async def test_a_transiently_unresolvable_topic_keeps_the_subscription(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
    ) -> None:
        """
        Discovery data can go missing while the broker is still fine.

        Tearing a working subscription down over it would turn a blip into a
        provider that never pushes again until the next reconnect.
        """
        lock = zui_lock_subscribed
        with patch.object(ZWaveJSUILock, "_resolve_state_topic", return_value=None):
            await lock._async_ensure_node_subscription()

        assert lock._subscribed_node_topic == ZUI_NODE_TOPIC
        assert len(lock._push_unsubs) == 1

    async def test_an_unresolvable_topic_with_no_subscription_disconnects(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """With nothing to fall back on, an unresolvable topic is a disconnect."""
        lock = zui_lock_provider
        with (
            patch.object(ZWaveJSUILock, "_resolve_state_topic", return_value=None),
            pytest.raises(LockDisconnected, match="node topic not resolvable"),
        ):
            await lock._async_ensure_node_subscription()

    async def test_mqtt_disabled_disconnects(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """Subscribing while MQTT is disabled is a disconnect, not a failure."""
        lock = zui_lock_provider
        with (
            patch(f"{MODULE}.mqtt_config_entry_enabled", return_value=False),
            pytest.raises(LockDisconnected, match="MQTT component not available"),
        ):
            await lock._async_ensure_node_subscription()

    async def test_setup_push_defers_while_mqtt_is_disabled(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """
        The reconnect path cannot raise, so a disabled MQTT is a deferral.

        ``setup_push_subscription`` runs synchronously from a state-change
        callback; raising there would surface as an unhandled error rather
        than a retry on the next transition.
        """
        lock = zui_lock_provider
        with (
            patch(f"{MODULE}.mqtt_config_entry_enabled", return_value=False),
            patch(f"{MODULE}.async_subscribe", new_callable=AsyncMock) as subscribe,
        ):
            lock.setup_push_subscription()
            await hass.async_block_till_done()

        subscribe.assert_not_called()
        assert not lock._push_unsubs

    async def test_setup_push_raises_without_a_node_topic(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """A lock that maps to no node topic reports it rather than deferring."""
        lock = zui_lock_provider
        with (
            patch.object(ZWaveJSUILock, "_resolve_state_topic", return_value=None),
            pytest.raises(LockDisconnected, match="no node topic"),
        ):
            lock.setup_push_subscription()

    async def test_setup_push_subscribes_from_the_reconnect_path(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
    ) -> None:
        """The background task establishes the subscription the primary path missed."""
        lock = zui_lock_provider

        lock.setup_push_subscription()
        await hass.async_block_till_done()

        assert lock._subscribed_node_topic == ZUI_NODE_TOPIC
        assert len(lock._push_unsubs) == 1

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(HomeAssistantError("denied"), id="home_assistant_error"),
            pytest.param(RuntimeError("boom"), id="unexpected_error"),
        ],
    )
    async def test_a_failed_background_subscribe_records_nothing(
        self,
        hass: HomeAssistant,
        zui_lock_provider: ZWaveJSUILock,
        error: Exception,
    ) -> None:
        """
        A subscribe that fails must not leave a handle behind.

        The background task swallows both kinds -- a HomeAssistantError is
        the expected "MQTT is unloading" refusal, anything else is a bug
        worth logging -- and neither may leave the provider believing it is
        subscribed.
        """
        lock = zui_lock_provider
        with patch(f"{MODULE}.async_subscribe", new=AsyncMock(side_effect=error)):
            lock.setup_push_subscription()
            await hass.async_block_till_done()

        assert not lock._push_unsubs
        assert lock._subscribed_node_topic is None

    async def test_teardown_drops_the_node_subscription_and_the_api_transport(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        One connection-down transition releases everything MQTT-side.

        The api response subscription is not a push subscription, but it is
        gone in exactly the same circumstances, so teardown owns both.
        """
        lock = zui_lock_subscribed
        lock._api_response_unsub = MagicMock()
        lock._api_response_topic = "zwave/_CLIENTS/+/api/+"
        lock._api_base = "zwave/_CLIENTS/ZWAVE_GATEWAY-zui"

        lock.teardown_push_subscription()

        assert not lock._push_unsubs
        assert lock._subscribed_node_topic is None
        assert lock._api_response_unsub is None
        assert lock._api_response_topic is None
        assert lock._api_base is None

        # Nothing arrives after teardown, and a second call is a no-op.
        fire_node_value(hass, f"{USER_CODE_VALUEID}/3", "1234")
        await hass.async_block_till_done()
        lock.coordinator.push_update.assert_not_called()
        lock.teardown_push_subscription()

    async def test_unload_releases_the_node_subscription_once(
        self,
        hass: HomeAssistant,
        zui_lock_subscribed: ZWaveJSUILock,
    ) -> None:
        """
        Unload goes through the base's push teardown, exactly once.

        ``supports_push`` being true is what routes unload here at all;
        a second release must not call a dropped unsub again, which Home
        Assistant answers with an exception.
        """
        lock = zui_lock_subscribed
        unsub = MagicMock()
        lock._push_unsubs.clear()
        lock._push_unsubs.append(unsub)

        await lock.async_unload(False)

        unsub.assert_called_once()
        assert not lock._push_unsubs
        assert lock._subscribed_node_topic is None


async def test_a_scheduled_read_re_ensures_the_subscription(
    hass: HomeAssistant,
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    A scheduled read is what heals a subscription that drifted silently.

    A rename produces no disconnect, so nothing else would notice. With push
    supported the coordinator schedules no poll, which leaves the hourly hard
    refresh -- routed through this same read -- as the only recurring visit.
    """
    lock = zui_gateway_resolved
    zui_api_responder.set_result("sendCommand", {"userIdStatus": 0})

    with patch(
        "custom_components.lock_code_manager.providers._base.get_managed_slots",
        return_value={1},
    ):
        await lock.async_get_users()

    assert lock._subscribed_node_topic == ZUI_NODE_TOPIC


async def test_a_poll_continues_when_the_subscription_cannot_be_refreshed(
    hass: HomeAssistant,
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """
    Reading is worth doing even when resubscribing is not currently possible.

    Failing the poll over a subscription refresh would turn a push-channel
    problem into no data at all, which is strictly worse than polled-only.
    """
    lock = zui_gateway_resolved
    zui_api_responder.set_result("sendCommand", {"userIdStatus": 0})

    with (
        patch.object(
            ZWaveJSUILock,
            "_async_ensure_node_subscription",
            AsyncMock(side_effect=LockDisconnected("no topic")),
        ),
        patch(
            "custom_components.lock_code_manager.providers._base.get_managed_slots",
            return_value={1},
        ),
    ):
        users = await lock.async_get_users()

    assert [user.user_id for user in users] == [1]


async def test_a_second_lock_on_the_same_node_is_addressed_separately(
    hass: HomeAssistant,
    zui_lock_subscribed: ZWaveJSUILock,
    mqtt_mock,
) -> None:
    """
    Two providers on one broker each classify only their own node's traffic.

    Home Assistant routes a message to every matching subscription, so the
    per-instance node-topic guard is the only thing keeping one lock's slot
    state out of the other's coordinator.
    """
    other_entity = await async_discover_zui_lock(hass, home_hex="0x1", node_id=21)
    other_lock = build_zui_lock(hass, other_entity)
    await other_lock.async_setup(MagicMock())
    await hass.async_block_till_done()
    other_lock.coordinator = MagicMock()

    fire_node_value(hass, f"{USER_CODE_VALUEID}/3", "1234")
    await hass.async_block_till_done()

    zui_lock_subscribed.coordinator.push_update.assert_called_once_with(
        {3: SlotCredential.known("1234")}
    )
    other_lock.coordinator.push_update.assert_not_called()
