"""
Z-Wave JS lock provider.

Handles push updates via access-control credential node events and operation
notifications for lock/unlock state changes. See ARCHITECTURE.md for the
provider's role in the data flow.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any, Literal

from zwave_js_server.client import Client
from zwave_js_server.const import NodeStatus
from zwave_js_server.const.command_class.access_control import UserCredentialType
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.exceptions import BaseZwaveJSServerError
from zwave_js_server.model.node import Node

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
from ..domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockDisconnected,
    LockOperationFailed,
)
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
    # Cached max user name length from lock_helpers capabilities. Populated
    # lazily on first async_set_user call (or by async_get_capabilities).
    # ``None`` means "not yet queried"; the value is always an int once set.
    _max_user_name_length: int | None = field(init=False, default=None)

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
    def hard_refresh_interval(self) -> timedelta | None:
        """
        Re-read all credentials hourly to recover from missed push events.

        Credentials are normally kept current by the access-control node-event
        push, but a missed or value-less event would otherwise strand a slot
        (for example as unreadable). This periodic drift refresh is the backstop.
        """
        return timedelta(hours=1)

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
        try:
            users = await self.node.access_control.get_users_cached()
            credentials = await self.node.access_control.get_all_credentials_cached()
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get users failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"get users failed: {err}") from err
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
        try:
            caps = await lock_helpers.async_get_credential_capabilities(self.node)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get capabilities failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"get capabilities failed: {err}") from err
        # Cache max name length for async_set_user name validation.
        self._max_user_name_length = caps.get("max_user_name_length", 0)
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
        # Resolve the max user name length. If async_get_capabilities has
        # not yet been called (or the cache is stale), fetch capabilities
        # so the name is always validated against the lock's actual limit.
        if self._max_user_name_length is None:
            try:
                raw_caps = await lock_helpers.async_get_credential_capabilities(
                    self.node
                )
                self._max_user_name_length = raw_caps.get("max_user_name_length", 0)
            except BaseZwaveJSServerError, HomeAssistantError:
                # Capabilities fetch failed — proceed without a name guard
                # so the write is not blocked by a read failure.
                self._max_user_name_length = 0
        max_name_len: int = self._max_user_name_length or 0
        # Enforce the lock's name length constraint:
        # - max_name_len == 0: the lock does not support user names; omit it
        # - name exceeds max_name_len: truncate to the allowed length
        user_name = user.name
        if user_name is not None:
            if max_name_len == 0:
                user_name = None
            elif len(user_name) > max_name_len:
                user_name = user_name[:max_name_len]
        try:
            existing = await self.node.access_control.get_user_cached(user.user_id)
            result = await lock_helpers.async_set_user(
                self.node,
                user_id=user.user_id,
                user_name=user_name,
                active=user.active,
            )
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"set user {user.user_id} failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"set user {user.user_id} failed: {err}") from err
        return SetUserResult(user_id=result["user_id"], created=existing is None)

    async def async_delete_user(self, user_id: int) -> None:
        """Delete the lock user (cascades its credentials)."""
        try:
            await lock_helpers.async_delete_user(self.node, user_id)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"delete user {user_id} failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"delete user {user_id} failed: {err}") from err

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """Write the Personal Identification Number credential under user_id; map device rejections."""
        if credential.type is not CredentialType.PIN:
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason=f"unsupported credential type: {credential.type}",
            )
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
        except BaseZwaveJSServerError as err:
            # Transient Z-Wave command failure (e.g. a sleeping/battery lock):
            # route to the retry path rather than a slot suspension.
            raise LockDisconnected(
                f"set credential slot {credential.slot} failed: {err}"
            ) from err
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
        if ref.type is not CredentialType.PIN:
            raise CodeRejectedError(
                code_slot=ref.slot,
                lock_entity_id=self.lock.entity_id,
                reason=f"unsupported credential type: {ref.type}",
            )
        try:
            await lock_helpers.async_delete_credential(
                self.node, ref.user_id, UserCredentialType.PIN_CODE, ref.slot
            )
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(
                f"delete credential slot {ref.slot} failed: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockOperationFailed(
                f"delete credential slot {ref.slot} failed: {err}"
            ) from err
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

    @callback
    def setup_push_subscription(self) -> None:
        """Subscribe to access-control credential change events."""
        if self._push_unsubs:
            return

        ready, reason = self._get_client_state()
        if not ready:
            raise LockDisconnected(reason)

        @callback
        def on_credential_changed(event: dict[str, Any]) -> None:
            """Handle credential added/modified events from the node."""
            args = event["args"]  # CredentialChangedArgs (pre-parsed by the library)
            if args.credential_type != UserCredentialType.PIN_CODE:
                return
            # The event carries the value when the lock includes it (e.g. an
            # out-of-band keypad change), so push the readable state rather than
            # always unreadable -- otherwise the slot would be stranded as
            # unreadable until the next set/clear or hard refresh.
            self._push_credential_update(
                args.credential_slot, self._pin_state(args.data)
            )

        @callback
        def on_credential_deleted(event: dict[str, Any]) -> None:
            """Handle credential deleted events from the node."""
            args = event["args"]  # CredentialDeletedArgs (pre-parsed by the library)
            if args.credential_type != UserCredentialType.PIN_CODE:
                return
            self._push_credential_update(args.credential_slot, SlotCredential.empty())

        try:
            for name, handler in (
                ("credential added", on_credential_changed),
                ("credential modified", on_credential_changed),
                ("credential deleted", on_credential_deleted),
            ):
                self._register_push_unsub(self.node.on(name, handler))
        except ValueError as err:
            self._clear_push_unsubs()
            raise LockDisconnected(f"node not ready: {err}") from err

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from credential change events."""
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
            raise LockDisconnected(f"hard refresh failed: {err}") from err
        return await self.async_get_usercodes()
