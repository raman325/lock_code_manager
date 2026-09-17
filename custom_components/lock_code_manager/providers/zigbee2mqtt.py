"""Module for Zigbee2MQTT locks."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
import json
from typing import Any, ClassVar, Literal, NoReturn

from homeassistant.components.mqtt import (
    async_publish,
    async_subscribe,
)
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import mqtt_config_entry_enabled
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ..domain.credentials import Credential, CredentialRef, User, WriteResult
from ..domain.exceptions import LockDisconnected, LockOperationFailed
from ..domain.models import SlotCredential
from ._mqtt import BaseMqttLock
from ._util import is_masked_code, parse_slot_num, resolve_discovery_payload
from .const import LOGGER

# Device registry identifier prefix Zigbee2MQTT uses in its HA discovery
# payloads; also consumed by providers.resolve_provider_class for dispatch.
Z2M_IDENTIFIER_PREFIX = "zigbee2mqtt_"

# Zigbee2MQTT action values for lock/unlock events triggered by PIN entry.
# These come from the DoorLock cluster's OperatingEventNotification and
# ProgrammingEventNotification via zigbee-herdsman-converters.
_Z2M_LOCK_ACTIONS_LOCKED = frozenset(
    {
        "lock",
        "keypad_lock",
        "manual_lock",
        "rf_lock",
    }
)
_Z2M_LOCK_ACTIONS_UNLOCKED = frozenset(
    {
        "unlock",
        "keypad_unlock",
        "manual_unlock",
        "rf_unlock",
    }
)
_Z2M_LOCK_ACTIONS = _Z2M_LOCK_ACTIONS_LOCKED | _Z2M_LOCK_ACTIONS_UNLOCKED


def _mqtt_payload_pin_has_code_value(pin_raw: Any) -> bool:
    """
    Return True when MQTT exposes a usable PIN value (including numeric zero).

    Plain truthiness is unsafe: ``0`` is a valid digit and must not be treated as
    absent. Boolean JSON values are ignored because they are not PIN payloads.
    """
    if pin_raw is None:
        return False
    if isinstance(pin_raw, bool):
        return False
    if isinstance(pin_raw, str):
        return pin_raw.strip() != ""
    return str(pin_raw) != ""


def _project_z2m_user_state(user_info: dict[str, Any]) -> SlotCredential:
    """
    Project one Zigbee2MQTT ``users`` entry to a SlotCredential.

    The status vocabulary comes from zigbee-herdsman-converters'
    ``lockUserStatus`` map (available/enabled/disabled); statuses outside
    it are published as ``not_supported_<n>``. Mapping traps:

    - ``enabled`` without a usable PIN value is occupied-but-withheld
      (``expose_pin`` off hides the code entirely), so it projects to
      unreadable -- treating it as empty would make sync reprogram a slot
      that already holds the right code.
    - The one exception: an explicit ``pin_code: null`` on an enabled user
      means the broker exposes the field and the device reports no code,
      so that projects to empty.
    - A lock that masks its codes publishes a usable-looking value that is
      all asterisks. That is the withheld state wearing a code's shape, so
      it lands in the same place -- see ``is_masked_code``.
    - ``available`` is the only status that means the slot holds nothing.
      ``disabled`` is a user the lock is refusing, and unrecognized statuses
      (``not_supported_*``) say nothing at all, so both project to
      unreadable for the same reprogramming-storm reason -- and so that
      allocation does not read either as a free credential index.
    """
    status = user_info.get("status")
    pin_raw = user_info.get("pin_code")
    if status == "enabled":
        if _mqtt_payload_pin_has_code_value(pin_raw):
            code = str(pin_raw)
            return (
                SlotCredential.unreadable()
                if is_masked_code(code)
                else SlotCredential.known(code)
            )
        if "pin_code" in user_info:
            return SlotCredential.empty()
        return SlotCredential.unreadable()
    if status == "available":
        return SlotCredential.empty()
    # ``disabled`` is a user the lock is holding and not accepting, not a
    # free slot. Reporting it empty tells allocation the index is available
    # and tells sync the slot is confirmed cleared.
    return SlotCredential.unreadable()


def _z2m_status_says_nothing(user_info: dict[str, Any]) -> bool:
    """
    Return whether a user entry is the lock declining to say anything.

    The converter reports any user status outside its map as
    ``not_supported_<n>``. Only 0xFF is the Zigbee Door Lock cluster's "not
    supported", the lock saying it cannot be read, so only that counts as
    silence; any other number is a status the lock did report.
    """
    return user_info.get("status") == "not_supported_255"


# How far Zigbee2MQTT's clock may run behind Home Assistant's before a reply
# dated by ``last_seen`` looks older than the request it answers. Only used
# while nothing earlier from the device can date it instead.
_CLOCK_SLACK = timedelta(seconds=5)


def _z2m_last_seen(payload: dict[str, Any]) -> datetime | None:
    """
    Return when Zigbee2MQTT last heard from the device, if the payload says.

    Sent only when ``last_seen`` is turned on, as an ISO 8601 string or as
    milliseconds since the epoch; anything else reads as not said.
    """
    value = payload.get("last_seen")
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str) and (parsed := dt_util.parse_datetime(value)):
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


@dataclass(repr=False, eq=False)
class Zigbee2MQTTLock(BaseMqttLock):
    """Class to represent Zigbee2MQTT lock."""

    # Zigbee door locks may implement PIN Set without PIN Get, and this
    # provider asks the device itself whether it is there.
    code_reads_may_be_unsupported: ClassVar[bool] = True
    # How long `_async_read_slot` waits for one slot before calling it silent.
    slot_read_timeout: ClassVar[float] = 10.0
    # Above the wait for one slot, so that bound always speaks first.
    per_exchange_budget: ClassVar[float | None] = 15.0

    _pending_codes: dict[int, asyncio.Future[SlotCredential | None]] = field(
        init=False, default_factory=dict
    )
    # Slots whose last read gave up waiting. A reply for one arriving later
    # still shows the lock answers, only slowly.
    _late_reads: set[int] = field(default_factory=set, init=False)
    # The last entry the device's topic carried for each slot, by the form it
    # came in, and the forms it has carried at all. Zigbee2MQTT republishes
    # its cached state with every message, so only an entry that changed, or
    # that an earlier message did not have, can be a late reply.
    _seen_entries: dict[tuple[str, int], Any] = field(default_factory=dict, init=False)
    _seen_forms: set[str] = field(default_factory=set, init=False)
    # Waiting for the device to say anything, with what dates an answer as
    # newer than the question: the last ``last_seen`` the device had sent, or
    # failing that, when it was asked. See _async_device_responds.
    _heard_from_device: list[tuple[datetime | None, datetime, asyncio.Future[None]]] = (
        field(init=False, default_factory=list)
    )
    # The last ``last_seen`` the device sent, in Zigbee2MQTT's own clock.
    _last_seen: datetime | None = field(init=False, default=None)
    # Last projected state per slot from the most recent users payload;
    # the delta gate in _process_z2m_device_payload compares against this
    # so full-cached-state republications don't repush stale entries.
    _last_users_states: dict[int, SlotCredential] = field(
        init=False, default_factory=dict
    )
    _subscribed_topic: str | None = field(init=False, default=None)

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    @property
    def supports_code_slot_events(self) -> bool:
        """Return whether this lock supports code slot events."""
        return True

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Subscribe to the device topic before the coordinator runs its first poll."""
        await self._async_ensure_device_subscription()

    def _is_z2m_device(self) -> bool:
        """Return whether the device registry marks this as a Zigbee2MQTT device."""
        if not self.device_entry:
            return False
        return any(
            len(identifier) >= 2
            and str(identifier[1]).startswith(Z2M_IDENTIFIER_PREFIX)
            for identifier in self.device_entry.identifiers
        )

    def _resolve_device_topic(self) -> str | None:
        """
        Resolve this lock's Zigbee2MQTT device topic from its MQTT discovery data.

        The discovery payload Zigbee2MQTT publishes carries the exact topics
        (``state_topic`` is ``<base_topic>/<friendly_name>``), so custom,
        multi-level, and per-bridge base topics all work verbatim with no
        reconstruction. Returns None whenever the topic cannot be determined —
        callers must treat that as disconnected rather than guess a topic.
        """
        if not self._is_z2m_device():
            return None
        payload = resolve_discovery_payload(self.hass, self.lock)
        if payload is None:
            return None
        state_topic = payload.get("state_topic")
        if isinstance(state_topic, str) and state_topic:
            return state_topic
        # Zigbee2MQTT's command topic is the device topic plus ``/set``, so
        # stripping the suffix names the same device the state topic would.
        command_topic = payload.get("command_topic")
        if isinstance(command_topic, str) and command_topic.endswith("/set"):
            return command_topic.removesuffix("/set")
        LOGGER.debug(
            "Discovery payload for %s has no usable topic",
            self.lock.entity_id,
        )
        return None

    def _get_topic(self, suffix: str = "") -> str | None:
        """Get the MQTT topic for this device, resolved from discovery data."""
        device_topic = self._resolve_device_topic()
        if not device_topic:
            return None
        if suffix:
            return f"{device_topic}/{suffix}"
        return device_topic

    def _maybe_raise_wrong_bridge_disconnect(self) -> None:
        """Raise when MQTT works but this entity cannot map to a Zigbee2MQTT topic."""
        if self.device_entry is None:
            return
        if self._is_z2m_device():
            return
        raise LockDisconnected(
            "This entity is not a Zigbee2MQTT lock (device registry lacks a "
            "zigbee2mqtt_* identifier)."
        )

    def _raise_not_connected(self) -> NoReturn:
        """Name the wrong-bridge misconfiguration before the generic reason."""
        self._maybe_raise_wrong_bridge_disconnect()
        super()._raise_not_connected()

    def _is_news(self, key: tuple[str, int], entry: Any, replayed: bool) -> bool:
        """
        Record what the device's topic said for a slot; return whether it is new.

        New is an entry that changed, or that an earlier message in the same
        form did not have. The first message in a form is not news: it may be
        Zigbee2MQTT's cache, which it resends with every message, and taking
        a stale entry for a late reply would record a lock that cannot report
        its codes as one that does, for good. A replayed message says nothing
        new and is not recorded.
        """
        if replayed:
            return False
        form = key[0]
        known = form in self._seen_forms
        prior = self._seen_entries.get(key)
        self._seen_entries[key] = entry
        return known and prior != entry

    async def async_is_integration_connected(self) -> bool:
        """Return whether MQTT is usable and this lock maps to a Z2M device topic."""
        if not mqtt_config_entry_enabled(self.hass):
            return False
        return bool(self._resolve_device_topic())

    @callback
    def _process_z2m_device_payload(
        self, payload: dict[str, Any], replayed: bool = False
    ) -> None:
        """
        Apply device-topic JSON on the Home Assistant event loop.

        ``replayed`` is a retained message the broker handed over again, which
        says nothing about the device now; neither does a payload whose
        ``last_seen`` is older than the question.
        """
        last_seen = None if replayed else _z2m_last_seen(payload)
        for seen_before, asked_at, waiter in self._heard_from_device:
            if (
                not waiter.done()
                and not replayed
                and (
                    last_seen is None
                    or (
                        last_seen > seen_before
                        if seen_before is not None
                        else last_seen >= asked_at - _CLOCK_SLACK
                    )
                )
            ):
                waiter.set_result(None)
        if last_seen is not None:
            self._last_seen = last_seen
        action = payload.get("action")

        # Handle lock/unlock actions with user identification (keypad PIN usage)
        if isinstance(action, str) and action in _Z2M_LOCK_ACTIONS:
            action_user = payload.get("action_user")
            if action_user is not None and not isinstance(action_user, bool):
                code_slot = parse_slot_num(action_user)
                if code_slot is None:
                    LOGGER.debug(
                        "Ignoring %s with non-numeric action_user %r for %s",
                        action,
                        action_user,
                        self.lock.entity_id,
                    )
                    return
                to_locked = action in _Z2M_LOCK_ACTIONS_LOCKED
                self.async_fire_code_slot_event(
                    code_slot=code_slot,
                    to_locked=to_locked,
                )
            return

        # Handle pin_code added / deleted (Z2M action events, not the users object)
        if action in ("pin_code_added", "pin_code_deleted"):
            action_user = payload.get("action_user")
            if action_user is not None:
                LOGGER.debug(
                    "Lock %s received %s for user %s",
                    self.lock.entity_id,
                    action,
                    action_user,
                )
                if self.coordinator:
                    self.hass.async_create_task(
                        self.coordinator.async_request_refresh()
                    )
            return

        users_data = payload.get("users")
        if users_data and isinstance(users_data, dict):
            states: dict[int, SlotCredential] = {}
            unanswered: set[int] = set()
            news: set[int] = set()
            for user_id_str, user_info in users_data.items():
                user_id = parse_slot_num(user_id_str)
                if user_id is None:
                    LOGGER.warning(
                        "Skipping non-numeric Zigbee2MQTT user key %r for %s",
                        user_id_str,
                        self.lock.entity_id,
                    )
                    continue

                if not isinstance(user_info, dict):
                    LOGGER.debug(
                        "Skipping unexpected user_info type %s for slot %s on %s",
                        type(user_info).__name__,
                        user_id_str,
                        self.lock.entity_id,
                    )
                    continue

                states[user_id] = _project_z2m_user_state(user_info)
                if _z2m_status_says_nothing(user_info):
                    unanswered.add(user_id)
                if self._is_news(("users", user_id), user_info, replayed):
                    news.add(user_id)
            if not replayed:
                self._seen_forms.add("users")

            # The converter answers GetPinCode through the users object
            # (fz.lock_pin_code_response), not through a pin_code response
            # payload -- resolve the pending read here or every slot read
            # times out (issue #1335). At most one read is pending at a
            # time (async_get_users queries slots sequentially), so cached
            # entries for other slots cannot satisfy a future they don't
            # belong to.
            for user_id, state in states.items():
                if (
                    future := self._pending_codes.pop(user_id, None)
                ) is not None and not future.done():
                    future.set_result(None if user_id in unanswered else state)
                elif (
                    user_id in self._late_reads
                    and user_id not in unanswered
                    and user_id in news
                ):
                    self._late_reads.discard(user_id)
                    self._note_answered()

            # Zigbee2MQTT republishes its full cached state on every
            # attribute change, so most users payloads restate old entries
            # rather than report changes. Applying them verbatim lets a
            # stale cache entry overwrite the optimistic push from a write
            # that the device already accepted -- the slot flips back to
            # its pre-write state and sync reprograms it forever (issue
            # #1335). Gate on the previous payload so only entries that
            # actually changed reach the coordinator.
            #
            # The gate only records payloads once a coordinator is
            # attached: retained/live messages can arrive between
            # async_setup's subscription and coordinator attach, and a
            # pre-attach snapshot would gate out the first post-attach
            # republication that should seed the initial state.
            if self.coordinator is not None:
                changed = {
                    user_id: state
                    for user_id, state in states.items()
                    if self._last_users_states.get(user_id) != state
                }
                self._last_users_states.update(states)
                if changed:
                    LOGGER.debug(
                        "Lock %s received push update for slots: %s",
                        self.lock.entity_id,
                        list(changed),
                    )
                    for user_id, state in changed.items():
                        self._confirm_slot(user_id, state)

        pin_code_data = payload.get("pin_code")
        if pin_code_data and isinstance(pin_code_data, dict):
            raw_user = pin_code_data.get("user")
            if raw_user is None:
                LOGGER.debug(
                    "Ignoring pin_code payload without user field for %s",
                    self.lock.entity_id,
                )
                return

            user_id = parse_slot_num(raw_user)
            if user_id is None:
                LOGGER.warning(
                    "Ignoring pin_code payload with non-numeric user for %s",
                    self.lock.entity_id,
                )
                return

            is_news = self._is_news(("pin_code", user_id), pin_code_data, replayed)
            if not replayed:
                self._seen_forms.add("pin_code")
            if user_id not in self._pending_codes:
                # An answer in this form is one however late, if it is new.
                if user_id in self._late_reads and is_news:
                    self._late_reads.discard(user_id)
                    self._note_answered()
            else:
                future = self._pending_codes.pop(user_id)
                if not future.done():
                    user_enabled = pin_code_data.get("user_enabled", False)
                    pin_code = pin_code_data.get("pin_code")
                    if _mqtt_payload_pin_has_code_value(pin_code):
                        # A code is plainly here. Whether the lock is
                        # currently accepting it, and whether what it sent is
                        # the digits or a mask standing in for them, decide
                        # only whether the value can be compared -- not
                        # whether the index is taken. A disabled user whose
                        # code is also masked is doubly incomparable and
                        # lands in the same place.
                        code = str(pin_code)
                        future.set_result(
                            SlotCredential.known(code)
                            if user_enabled and not is_masked_code(code)
                            else SlotCredential.unreadable()
                        )
                    elif user_enabled and "pin_code" not in pin_code_data:
                        # Enabled, and the code withheld rather than reported
                        # -- ``expose_pin`` off. The same state the users
                        # object reports, and the same answer it gives.
                        future.set_result(SlotCredential.unreadable())
                    else:
                        # Either the lock says nothing is enabled here, or it
                        # answered the code explicitly with nothing.
                        future.set_result(SlotCredential.empty())

    async def _async_ensure_device_subscription(self) -> None:
        """Subscribe to the Z2M device topic; idempotent and drift-aware."""
        if not mqtt_config_entry_enabled(self.hass):
            raise LockDisconnected("MQTT component not available")

        topic = self._get_topic()
        if not topic:
            if self._push_unsubs:
                # Resolution is transiently unavailable; keep the existing
                # subscription rather than tearing down a working one.
                return
            raise LockDisconnected(
                f"Cannot subscribe for {self.lock.entity_id} — "
                "device topic not resolvable from MQTT discovery data"
            )

        if self._push_unsubs and self._subscribed_topic == topic:
            return

        # Topic changed (rename / bridge migration) or first subscribe.
        self._clear_push_unsubs()
        self._subscribed_topic = None

        def message_received(msg: ReceiveMessage) -> None:
            """Handle incoming MQTT messages (may run off the event loop)."""
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, TypeError) as err:
                LOGGER.debug(
                    "Ignoring invalid MQTT JSON for %s: %s",
                    self.lock.entity_id,
                    err,
                )
                return

            self.hass.add_job(self._process_z2m_device_payload, payload, msg.retain)

        try:
            unsub = await async_subscribe(self.hass, topic, message_received)
        except HomeAssistantError as err:
            LOGGER.error(
                "Failed to subscribe to MQTT for %s: %s",
                self.lock.entity_id,
                err,
            )
            raise LockDisconnected(
                f"Failed to subscribe to MQTT for {self.lock.entity_id}"
            ) from err
        self._register_push_unsub(unsub)
        self._subscribed_topic = topic
        LOGGER.debug("Subscribed to MQTT topic %s for %s", topic, self.lock.entity_id)

    @callback
    def setup_push_subscription(self) -> None:
        """
        Subscribe via background task when still unsubscribed (e.g. reconnect).

        Primary subscribe is ``await`` in ``async_setup``.
        """
        self._schedule_push_subscription(
            self._get_topic(), self._async_ensure_device_subscription, "topic"
        )

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from MQTT updates."""
        had_subscription = bool(self._push_unsubs)
        self._clear_push_unsubs()
        self._subscribed_topic = None
        if had_subscription:
            LOGGER.debug("Unsubscribed from MQTT for %s", self.lock.entity_id)

        # Cancel any pending futures
        for future in self._pending_codes.values():
            if not future.done():
                future.cancel()
        self._pending_codes.clear()

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> WriteResult:
        """
        Set a Personal Identification Number credential on a code slot.

        Publishes a Zigbee2MQTT ``set`` payload and immediately pushes an
        optimistic coordinator update (MQTT QoS 0 gives no delivery
        guarantee; hard-refresh mitigates drift). ``user_id`` is ignored;
        slot-only providers address the credential by ``credential.slot``.
        """
        code_slot = credential.slot

        await self._async_ensure_operational(require_device=False)

        set_topic = self._get_topic("set")
        if not set_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # Zigbee2MQTT set_pin_code payload format
        payload = json.dumps(
            {
                "pin_code": {
                    "user": code_slot,
                    "user_type": "unrestricted",
                    "pin_code": pin,
                    "user_enabled": True,
                }
            }
        )

        try:
            await async_publish(self.hass, set_topic, payload)
        except OSError as err:
            # Network-level publish failure (broker unreachable). Route to
            # disconnect so the reconnect path runs instead of breaking
            # per-slot.
            LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to set PIN: {err}") from err
        except HomeAssistantError as err:
            LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockOperationFailed(f"Failed to set PIN: {err}") from err

        LOGGER.debug(
            "Published set_pin_code for %s slot %s",
            self.lock.entity_id,
            code_slot,
        )
        # Optimistic coordinator update after publish (MQTT QoS 0); hard_refresh mitigates drift.
        self._push_credential_update(code_slot, SlotCredential.known(pin))
        return WriteResult.CONFIRMED

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Clear a Personal Identification Number from a code slot.

        Publishes a Zigbee2MQTT ``set`` payload with ``user_enabled=false``
        and ``pin_code=null`` (many locks require both to fully clear the
        slot) and immediately pushes an optimistic coordinator update.
        See ``async_set_credential`` for the OSError-versus-HomeAssistantError
        routing rationale.
        """
        code_slot = ref.slot

        await self._async_ensure_operational(require_device=False)

        set_topic = self._get_topic("set")
        if not set_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # Z2M: many locks need user_enabled false and pin_code null to clear the slot
        # (user_enabled only is not always enough on the device).
        payload = json.dumps(
            {
                "pin_code": {
                    "user": code_slot,
                    "user_type": "unrestricted",
                    "user_enabled": False,
                    "pin_code": None,
                }
            }
        )

        try:
            await async_publish(self.hass, set_topic, payload)
        except OSError as err:
            # See ``async_set_credential`` for the OSError split rationale.
            LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err
        except HomeAssistantError as err:
            LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockOperationFailed(f"Failed to clear PIN: {err}") from err

        LOGGER.debug(
            "Published clear_pin_code for %s slot %s",
            self.lock.entity_id,
            code_slot,
        )
        # Same optimistic push as ``async_set_credential``.
        self._push_credential_update(code_slot, SlotCredential.empty())
        return True

    async def async_get_users(self, slots: Collection[int] | None = None) -> list[User]:
        """
        Read Personal Identification Number codes one index at a time.

        What sequencing the reads buys, and why a read that reached nothing
        raises rather than reporting a lock full of unreadable slots, is
        ``BaseMqttLock._async_read_slots``. A broker that has stopped
        carrying traffic gets that far because every gate below answers from
        Home Assistant's configuration rather than from the wire.
        """
        await self._async_ensure_operational()

        # Renames/bridge migrations with no disconnect self-heal here: the
        # hourly hard refresh lands on this read and re-checks that the push
        # subscription still matches the currently resolved topic.
        try:
            await self._async_ensure_device_subscription()
        except LockDisconnected as err:
            LOGGER.debug(
                "Lock %s: could not refresh push subscription before poll: %s",
                self.lock.entity_id,
                err,
            )

        get_topic = self._get_topic("get")
        if not get_topic:
            raise LockDisconnected("Could not determine MQTT topic")

        # One request per index, so the caller's scope bounds the work. The
        # topic is resolved once for the whole read rather than per slot: it
        # comes from a discovery-data walk, and one answer for one poll is
        # also what keeps a mid-read rename from splitting it across topics.
        return await self._async_read_slots(
            self.managed_slots if slots is None else slots,
            partial(self._async_read_slot, get_topic=get_topic),
            transport_failure="failed to reach the lock",
        )

    async def _async_read_slot(
        self, slot_num: int, get_topic: str
    ) -> SlotCredential | None:
        """
        Ask the lock for one slot and wait for the answer; None means silence.

        A request that never left and a request nothing came back for are
        both silence: a slot the bridge described as withheld, disabled, or
        masked is data, and a slot that produced no reply at all is not.
        That is the distinction ``BaseMqttLock._async_read_slots`` needs to
        tell a poll from a read that reached nothing.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_codes[slot_num] = future
        payload = json.dumps({"pin_code": {"user": slot_num}})
        try:
            await async_publish(self.hass, get_topic, payload)
        except (HomeAssistantError, OSError) as err:
            LOGGER.debug(
                "MQTT publish failed for PIN get %s slot %s: %s",
                self.lock.entity_id,
                slot_num,
                err,
            )
            self._pending_codes.pop(slot_num, None)
            return None

        try:
            result = await asyncio.wait_for(future, timeout=self.slot_read_timeout)
        except TimeoutError:
            LOGGER.debug(
                "Timeout waiting for PIN code response for %s slot %s",
                self.lock.entity_id,
                slot_num,
            )
            self._late_reads.add(slot_num)
            credential = None
        except Exception as err:
            # Broad catch is intentional: the future is resolved by the MQTT
            # callback, and any exception from resolution (InvalidStateError,
            # data processing errors) should not crash the entire refresh.
            # CancelledError is BaseException in Python 3.11+ and propagates.
            # Only an arriving reply can resolve the future, so this is a
            # reply that could not be made sense of rather than silence: it
            # stays a credential and keeps the poll alive.
            LOGGER.warning(
                "Unexpected error getting PIN for %s slot %s: %s",
                self.lock.entity_id,
                slot_num,
                err,
            )
            credential = SlotCredential.unreadable()
        else:
            credential = result
            self._late_reads.discard(slot_num)
        finally:
            self._pending_codes.pop(slot_num, None)
        return credential

    async def _async_device_responds(self) -> bool:
        """
        Ask the lock for its lock state and say whether anything came back.

        Zigbee2MQTT keeps a lock's entity available while the device is out
        of range unless availability tracking is turned on, which it is not
        by default, so the entity cannot tell a lock that will not report its
        codes from one that is gone. Every lock reports whether it is locked.

        Zigbee2MQTT also republishes a device's cached state without asking
        it: retained replays are told apart, and so is any payload dated by
        ``last_seen``: a fresh answer carries a later one than the device last
        sent. Until the device has sent one, Home Assistant's clock stands in,
        and a Zigbee2MQTT clock running behind can make one answer look stale.
        With ``last_seen`` off, the republish Zigbee2MQTT makes when Home
        Assistant comes online cannot be told apart, and lands here only if it
        falls in this wait.
        """
        get_topic = self._get_topic("get")
        if not get_topic:
            return False
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (self._last_seen, dt_util.utcnow(), waiter)
        self._heard_from_device.append(entry)
        try:
            await async_publish(self.hass, get_topic, json.dumps({"state": ""}))
            await asyncio.wait_for(waiter, timeout=self.slot_read_timeout)
        except HomeAssistantError, OSError, TimeoutError:
            return False
        finally:
            self._heard_from_device.remove(entry)
        return True

    async def async_get_max_slot(self) -> int | None:
        """
        Report no opinion: the bridge is not asked.

        Zigbee2MQTT publishes device definitions on its bridge topic, and a
        lock's ``pin_code`` expose can carry the user range, but this
        provider subscribes only to the device's own topic. Reading the
        definition is worth doing -- this is a lock that answers one index
        per round trip, so the limit is what the search costs -- and wants a
        real bridge payload to work from rather than a guessed shape.
        """
        return None
