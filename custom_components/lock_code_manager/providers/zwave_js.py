"""
Z-Wave JS lock provider.

Handles push updates, duplicate code detection, and rate-limited set/clear operations.
See ARCHITECTURE.md for the provider's role in the data flow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any, Literal

from zwave_js_server.client import Client
from zwave_js_server.const import CommandClass, NodeStatus
from zwave_js_server.const.command_class.access_control import UserCredentialType
from zwave_js_server.const.command_class.lock import (
    ATTR_IN_USE,
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import get_usercode

from homeassistant.components.zwave_js import lock_helpers
from homeassistant.components.zwave_js.const import (
    ATTR_EVENT,
    ATTR_EVENT_LABEL,
    ATTR_HOME_ID,
    ATTR_NODE_ID,
    ATTR_PARAMETERS,
    ATTR_TYPE,
    DOMAIN as ZWAVE_JS_DOMAIN,
    ZWAVE_JS_NOTIFICATION_EVENT,
)
from homeassistant.components.zwave_js.helpers import async_get_node_from_entity_id
from homeassistant.components.zwave_js.models import ZwaveJSData
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError

from ..domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SetUserResult,
    User,
)
from ..domain.exceptions import CodeRejectedError, DuplicateCodeError, LockDisconnected
from ..domain.models import SlotCredential
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)

# String key used by lock_helpers for Personal Identification Number credentials
# in the supported_credential_types dict returned by async_get_credential_capabilities.
_PIN_TYPE_STR = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]

# All known Access Control Notification CC events that indicate the lock is locked
# or unlocked
ACCESS_CONTROL_NOTIFICATION_TO_LOCKED = {
    True: (
        AccessControlNotificationEvent.AUTO_LOCK_LOCKED_OPERATION,
        AccessControlNotificationEvent.KEYPAD_LOCK_OPERATION,
        AccessControlNotificationEvent.LOCK_OPERATION_WITH_USER_CODE,
        AccessControlNotificationEvent.LOCKED_BY_RF_WITH_INVALID_USER_CODE,
        AccessControlNotificationEvent.MANUAL_LOCK_OPERATION,
        AccessControlNotificationEvent.RF_LOCK_OPERATION,
    ),
    False: (
        AccessControlNotificationEvent.KEYPAD_UNLOCK_OPERATION,
        AccessControlNotificationEvent.MANUAL_UNLOCK_OPERATION,
        AccessControlNotificationEvent.RF_UNLOCK_OPERATION,
        AccessControlNotificationEvent.UNLOCK_BY_RF_WITH_INVALID_USER_CODE,
        AccessControlNotificationEvent.UNLOCK_OPERATION_WITH_USER_CODE,
    ),
}


@dataclass(repr=False, eq=False)
class ZWaveJSLock(BaseLock):
    """Class to represent ZWave JS lock."""

    lock_config_entry: ConfigEntry = field(repr=False)
    # Home Assistant event-bus listeners (separate lifecycle from push
    # subscriptions: registered in ``async_setup``, released in
    # ``async_unload``).
    _listeners: list[Callable[[], None]] = field(init=False, default_factory=list)
    _set_in_progress_code_slot: int | None = field(init=False, default=None)

    @property
    def node(self) -> Node:
        """Return ZWave JS node."""
        return async_get_node_from_entity_id(
            self.hass, self.lock.entity_id, self.ent_reg
        )

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    @property
    def connection_check_interval(self) -> timedelta | None:
        """Z-Wave JS exposes config entry state changes, so skip polling."""
        return None

    @property
    def supports_native_users(self) -> bool:
        """Return True: this provider implements the credential primitives."""
        return True

    def _pin_state(self, data: str | bytes | None) -> SlotCredential:
        """Project Z-Wave credential data to a SlotCredential (readable when present)."""
        if not data:
            return SlotCredential.unreadable()
        # CredentialData.data is str | bytes; decode bytes so a Personal
        # Identification Number is the digit string, not "b'1234'".
        return SlotCredential.known(data if isinstance(data, str) else data.decode())

    async def async_get_users(self) -> list[User]:
        """Read users and their PIN credentials (with values) from the lock."""
        users = await self.node.access_control.get_users_cached()
        credentials = await self.node.access_control.get_all_credentials_cached()
        pins_by_user: dict[int, list[Credential]] = {}
        for cred in credentials:
            if cred.type is not UserCredentialType.PIN_CODE:
                continue
            pins_by_user.setdefault(cred.user_id, []).append(
                Credential(
                    type=CredentialType.PIN,
                    slot=cred.slot,
                    state=self._pin_state(cred.data),
                )
            )
        return [
            User(
                user_id=user.user_id,
                name=user.user_name,
                active=user.active,
                credentials=pins_by_user.get(user.user_id, []),
            )
            for user in users
        ]

    async def async_get_capabilities(self) -> LockCapabilities:
        """Report the lock's user/credential capabilities."""
        caps = await lock_helpers.async_get_credential_capabilities(self.node)
        pin = caps["supported_credential_types"].get(_PIN_TYPE_STR)
        credential_types = (
            {
                CredentialType.PIN: CredentialTypeCapability(
                    num_slots=pin["num_slots"],
                    min_length=pin["min_length"],
                    max_length=pin["max_length"],
                    supports_learn=pin["supports_learn"],
                )
            }
            if pin
            else {}
        )
        return LockCapabilities(
            supports_user_management=caps["supports_user_management"],
            max_users=caps["max_users"],
            credential_types=credential_types,
        )

    async def async_set_user(self, user: User) -> SetUserResult:
        """
        Create or update the lock user; report whether it was created.

        ``created`` is derived from a pre-write cache read of the explicit
        ``user.user_id`` (which the helper always honors, never auto-allocating
        here), not from the helper's return value, so the base orchestration
        can roll back a user this call newly created.
        """
        existing = await self.node.access_control.get_user_cached(user.user_id)
        result = await lock_helpers.async_set_user(
            self.node,
            user_id=user.user_id,
            user_name=user.name,
            active=user.active,
        )
        return SetUserResult(user_id=result["user_id"], created=existing is None)

    async def async_delete_user(self, user_id: int) -> None:
        """Delete the lock user (cascades its credentials)."""
        await lock_helpers.async_delete_user(self.node, user_id)

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """Write the Personal Identification Number credential under user_id; map device rejections."""
        pin = credential.readable_pin
        if pin is None:
            # The set path only ever carries a readable Personal Identification
            # Number; guard so an unreadable credential fails cleanly rather
            # than passing None into the helper's str-only signature.
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason="cannot write an unreadable credential",
            )
        try:
            await lock_helpers.async_set_credential(
                self.node,
                user_id,
                UserCredentialType.PIN_CODE,
                pin,
                credential_slot=credential.slot,
            )
        except HomeAssistantError as err:
            if getattr(err, "translation_key", None) == "credential_rejected_duplicate":
                raise DuplicateCodeError(
                    code_slot=credential.slot,
                    lock_entity_id=self.lock.entity_id,
                ) from err
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason=str(err),
            ) from err
        return True

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """Delete the Personal Identification Number credential addressed by ref."""
        await lock_helpers.async_delete_credential(
            self.node, ref.user_id, UserCredentialType.PIN_CODE, ref.slot
        )
        return True

    def _get_client_state(self) -> tuple[bool, str]:
        """Return whether the Z-Wave JS client is ready and a retry reason."""
        if self.lock_config_entry.state != ConfigEntryState.LOADED:
            return False, "config entry not loaded"

        runtime_data: ZwaveJSData | None = getattr(
            self.lock_config_entry, "runtime_data", None
        )
        client: Client | None = (
            getattr(runtime_data, "client", None) if runtime_data else None
        )
        if not client:
            return False, "Z-Wave JS client not ready"

        if not client.connected:
            return False, "Z-Wave JS client not connected"

        if client.driver is None:
            return False, "Z-Wave JS driver not ready"

        return True, ""

    def code_slot_in_use(self, code_slot: int) -> bool | None:
        """Return whether a code slot is in use."""
        try:
            return get_usercode(self.node, code_slot)[ATTR_IN_USE]
        except KeyError, ValueError:
            return None

    @callback
    def _handle_usercode_status_update(self, code_slot: int, status: Any) -> None:
        """Handle userIdStatus value update for a code slot."""
        if status == CodeSlotStatus.AVAILABLE:
            # Ignore AVAILABLE status if Lock Code Manager expects a PIN on this
            # slot. Some locks send stale AVAILABLE events after a code was set,
            # which would cause infinite sync loops.
            if (
                self.coordinator
                and self.coordinator.desired_credential(code_slot).is_present
            ):
                _LOGGER.debug(
                    "Lock %s: ignoring userIdStatus=AVAILABLE for slot %s "
                    "(LCM expects PIN on this slot)",
                    self.lock.entity_id,
                    code_slot,
                )
                return

            # Slot was cleared - update coordinator if needed
            current = self.coordinator.data.get(code_slot) if self.coordinator else None
            if self.coordinator and (current is None or not current.is_empty):
                _LOGGER.debug(
                    "Lock %s: slot %s userIdStatus=AVAILABLE, marking cleared",
                    self.lock.entity_id,
                    code_slot,
                )
                self._push_credential_update(code_slot, SlotCredential.empty())

    @callback
    def _handle_usercode_value_update(self, code_slot: int, new_value: Any) -> None:
        """Handle userCode value update for a code slot."""
        if not new_value:
            resolved = SlotCredential.empty()
        else:
            value = str(new_value)
            slot_in_use = self.code_slot_in_use(code_slot)
            # Asymmetric in_use checks: masked codes count as unreadable even
            # when in_use is None (some firmwares mask before reporting
            # status), but all-zeros only counts as empty when in_use is
            # explicitly False (zeros from a partially-loaded cache must
            # not be misread as cleared).
            if value == "*" * len(value) and slot_in_use is not False:
                resolved = SlotCredential.unreadable()
            elif value.strip("0") == "" and slot_in_use is False:
                resolved = SlotCredential.empty()
            else:
                resolved = SlotCredential.known(value)

        # Z-Wave JS sends duplicate events; skip if the value is unchanged.
        if self.coordinator and self.coordinator.data.get(code_slot) == resolved:
            return

        _LOGGER.debug(
            "Lock %s received push update for slot %s: %s",
            self.lock.entity_id,
            code_slot,
            "****" if resolved.is_readable else f"({resolved.as_label()})",
        )
        self._push_credential_update(code_slot, resolved)

    @callback
    def setup_push_subscription(self) -> None:
        """Subscribe to User Code CC value update events."""
        if self._push_unsubs:
            return

        ready, reason = self._get_client_state()
        if not ready:
            raise LockDisconnected(reason)

        @callback
        def on_value_updated(event: dict[str, Any]) -> None:
            """Handle value update events from Z-Wave JS."""
            args: dict[str, Any] = event["args"]
            if args.get("commandClass") != CommandClass.USER_CODE:
                return

            property_name = args.get("property")
            if property_name not in (
                LOCK_USERCODE_PROPERTY,
                LOCK_USERCODE_STATUS_PROPERTY,
            ):
                return

            code_slot = int(args["propertyKey"])

            # Slot 0 is not a valid user code slot.
            if code_slot == 0:
                return

            # Clear in-progress tracking only on userCode updates for the slot
            # we were setting. userIdStatus updates don't confirm acceptance and
            # could race with duplicate-code notifications.
            if (
                property_name == LOCK_USERCODE_PROPERTY
                and code_slot == self._set_in_progress_code_slot
            ):
                self._set_in_progress_code_slot = None

            if property_name == LOCK_USERCODE_STATUS_PROPERTY:
                self._handle_usercode_status_update(code_slot, args.get("newValue"))
            else:
                self._handle_usercode_value_update(code_slot, args.get("newValue"))

        try:
            unsub = self.node.on("value updated", on_value_updated)
        except ValueError as err:
            raise LockDisconnected(f"node not ready: {err}") from err
        self._register_push_unsub(unsub)

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from value update events."""
        self._clear_push_unsubs()

    @callback
    def _zwave_js_event_filter(self, event_data: dict[str, Any]) -> bool:
        """Return True if the event belongs to this lock's node."""
        assert self.node.client.driver
        return (
            event_data[ATTR_HOME_ID] == self.node.client.driver.controller.home_id
            and event_data[ATTR_NODE_ID] == self.node.node_id
            and event_data[ATTR_DEVICE_ID] == self.lock.device_id
        )

    @callback
    def _handle_zwave_js_event(self, evt: Event) -> None:
        """Handle Z-Wave JS event."""
        if evt.data[ATTR_TYPE] != NotificationType.ACCESS_CONTROL:
            _LOGGER.debug(
                "Lock %s received non Access Control event: %s",
                self.lock.entity_id,
                evt.as_dict(),
            )
            return

        params = evt.data.get(ATTR_PARAMETERS) or {}
        code_slot = params.get("userId", 0)

        # Handle duplicate code rejection — only when LCM initiated the set.
        # Mark the slot as rejected so the sync manager raises DuplicateCodeError
        # on the next tick, routing through the standard CodeRejectedError flow
        # (tracker reset, circuit breaker awareness, notification).
        # Some Z-Wave lock firmwares report this notification with userId=0
        # instead of the offending slot; treat 0 as referring to the slot
        # we're currently setting.
        if (
            evt.data[ATTR_EVENT]
            == AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
            and self._set_in_progress_code_slot is not None
            and code_slot in (0, self._set_in_progress_code_slot)
        ):
            slot = self._set_in_progress_code_slot
            self._set_in_progress_code_slot = None
            self.mark_code_rejected(slot)
            return

        self.async_fire_code_slot_event(
            code_slot=code_slot,
            to_locked=next(
                (
                    to_locked
                    for to_locked, codes in ACCESS_CONTROL_NOTIFICATION_TO_LOCKED.items()
                    if evt.data[ATTR_EVENT] in codes
                ),
                None,
            ),
            action_text=evt.data.get(ATTR_EVENT_LABEL),
            source_data=evt,
        )

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return ZWAVE_JS_DOMAIN

    def _clear_listeners(self) -> None:
        """Unsubscribe and clear all HA event bus listeners."""
        for listener in self._listeners:
            listener()
        self._listeners.clear()

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """
        Set up lock by provider.

        Idempotent: clears existing listeners before re-registering.
        """
        self._clear_listeners()
        self._listeners.append(
            self.hass.bus.async_listen(
                ZWAVE_JS_NOTIFICATION_EVENT,
                self._handle_zwave_js_event,
                self._zwave_js_event_filter,
            )
        )

    async def async_unload(self, remove_permanently: bool) -> None:
        """Unload lock."""
        self._clear_listeners()
        await super().async_unload(remove_permanently)

    async def async_is_integration_connected(self) -> bool:
        """Return whether the Z-Wave JS client is connected."""
        ready, _reason = self._get_client_state()
        return ready

    async def async_is_device_available(self) -> bool:
        """Return whether the Z-Wave node is available for commands."""
        try:
            return self.node.status != NodeStatus.DEAD
        except Exception as err:
            _LOGGER.debug(
                "Lock %s: failed to check device availability: %s",
                self.lock.entity_id,
                err,
            )
            return False

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """Re-read users AND credentials fresh from the device, then project to slots."""
        try:
            await self.node.access_control.get_users()
            await self.node.access_control.get_all_credentials()
        except Exception as err:
            raise LockDisconnected from err
        return await self.async_get_usercodes()
