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
from zwave_js_server.const.command_class.access_control import (
    SetCredentialResult,
    UserCredentialType,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.exceptions import BaseZwaveJSServerError
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import get_usercodes

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
from ._util import parse_tag

_LOGGER = logging.getLogger(__name__)

# String key used by lock_helpers for Personal Identification Number credentials
# in the supported_credential_types dict returned by async_get_credential_capabilities.
_PIN_TYPE_STR = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]

# Z-Wave UserCredentialType -> domain CredentialType. The domain vocabulary
# is intentionally narrower than the Z-Wave one: types with no domain
# equivalent (BLE, UWB, DESFIRE, unspecified/eye/hand biometrics) are
# omitted and silently dropped by async_get_users. The base orchestration
# only acts on Personal Identification Number credentials today, but the
# non-PIN types we can represent are surfaced so direct callers (and a
# future expansion past PIN-only) see the full picture without another
# read.
_ZWAVE_TO_DOMAIN_CREDENTIAL_TYPE: dict[UserCredentialType, CredentialType] = {
    UserCredentialType.PIN_CODE: CredentialType.PIN,
    UserCredentialType.PASSWORD: CredentialType.PASSWORD,
    UserCredentialType.RFID_CODE: CredentialType.RFID,
    UserCredentialType.NFC: CredentialType.NFC,
    UserCredentialType.FACE_BIOMETRIC: CredentialType.FACE,
    UserCredentialType.FINGER_BIOMETRIC: CredentialType.FINGERPRINT,
}


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
        """
        Read every user and all of their credentials from the lock.

        Returns users carrying every credential type the domain model can
        represent (Personal Identification Number, Radio Frequency
        Identification, Near Field Communication, password, face,
        fingerprint). The base orchestration filters to Personal
        Identification Number at the slot-projection layer via
        ``user.pin_credentials``, so this method does no type-specific
        filtering -- direct callers see the full picture without an
        extra read. Z-Wave credential types with no domain equivalent
        (BLE, UWB, DESFIRE, unspecified/eye/hand biometrics) are
        dropped.

        Uses the unified ``access_control`` API which dispatches to UC
        or U3C internally per node-zwave-js v15.23.4+.
        """
        try:
            users = await self.node.access_control.get_users_cached()
            credentials = await self.node.access_control.get_all_credentials_cached()
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get users failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"get users failed: {err}") from err
        users_by_id: dict[int, User] = {
            user.user_id: User(
                user_id=user.user_id,
                name=user.user_name,
                active=user.active,
            )
            for user in users
        }
        for cred in credentials:
            domain_type = _ZWAVE_TO_DOMAIN_CREDENTIAL_TYPE.get(cred.type)
            owner = users_by_id.get(cred.user_id)
            if domain_type is None or owner is None:
                continue
            # _pin_state decodes the Personal Identification Number value
            # so the slot-projection sees a comparable string. For other
            # credential types the data is opaque to Lock Code Manager
            # (an RFID tag identifier, a biometric hash, ...), so they
            # surface as unreadable -- "the slot is occupied" without
            # revealing a value the integration would not act on anyway.
            state = (
                self._pin_state(cred.data)
                if cred.type is UserCredentialType.PIN_CODE
                else SlotCredential.unreadable()
            )
            owner.credentials.append(
                Credential(type=domain_type, slot=cred.slot, state=state)
            )
        return list(users_by_id.values())

    async def async_get_capabilities(self) -> LockCapabilities:
        """
        Report the lock's user/credential capabilities.

        Auto-detects User Code CC (UC) vs User Credential CC (U3C) and
        returns capabilities shaped so the seam routes through the right
        code path.

        The unified ``access_control`` API in node-zwave-js claims support
        for both UC and U3C, but ``async_get_credential_capabilities``
        queries U3C-specific data only. UC-only locks (mostly older
        500-series firmwares but also some current Z-Wave Plus
        implementations) come back with the PIN credential type either
        missing or advertising ``num_slots=0``. When that happens we
        fall back to reading the lock's UC slot count directly and
        return slot-only capabilities: ``supports_user_management=False``
        and ``max_user_name_length=0``, which the seam recognizes as a
        slot-only provider and routes through the credential-only
        primitives (``async_set_credential`` / ``async_delete_credential``
        / ``async_get_users``) without the user lifecycle.
        """
        try:
            caps = await lock_helpers.async_get_credential_capabilities(self.node)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get capabilities failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"get capabilities failed: {err}") from err
        pin = caps["supported_credential_types"].get(_PIN_TYPE_STR)

        if pin and pin["num_slots"] > 0:
            # U3C path — the lock advertises real PIN credential slots.
            return LockCapabilities(
                supports_user_management=caps["supports_user_management"],
                max_users=caps["max_users"],
                credential_types={
                    CredentialType.PIN: CredentialTypeCapability(
                        num_slots=pin["num_slots"],
                        min_length=pin["min_length"],
                        max_length=pin["max_length"],
                        supports_learn=pin["supports_learn"],
                    )
                },
                max_user_name_length=caps.get("max_user_name_length", 0),
            )

        # UC path — try the legacy User Code CC. ``get_usercodes`` walks
        # slot 1, 2, 3, ... in the value DB until ``NotFoundError``, so
        # the returned list length is the lock's actual UC slot count.
        # An empty list means the lock genuinely has no PIN support.
        try:
            uc_slots = get_usercodes(self.node)
        except Exception:
            uc_slots = []
        if not uc_slots:
            return LockCapabilities(
                supports_user_management=False,
                max_users=0,
                credential_types={},
                max_user_name_length=0,
            )
        return LockCapabilities(
            # Force slot-only routing: supports_user_management=False
            # gates _supports_user_records() at the seam, so the User
            # lifecycle (async_set_user / async_delete_user) is skipped
            # and our async_set_credential / async_delete_credential
            # become the direct call targets.
            supports_user_management=False,
            max_users=0,
            credential_types={
                CredentialType.PIN: CredentialTypeCapability(
                    num_slots=len(uc_slots),
                    # UC spec allows 4-10 ASCII digits per User Code CC v1+.
                    min_length=4,
                    max_length=10,
                    supports_learn=False,
                )
            },
            max_user_name_length=0,
        )

    async def async_set_user(self, user: User) -> SetUserResult:
        """
        Find-or-create the lock user for the LCM slot encoded in ``user.name``.

        The base seam passes a tagged ``user.name`` (``lcm:<slot>:<display>``)
        whose slot is the LCM-side identity for this credential. The Z-Wave
        lock's own ``user_id`` is whatever Z-Wave happens to allocate; LCM
        no longer pins it to the slot. Discovery on every call:

        1. Scan the lock's current user list for a user whose name carries
           the same ``lcm:<slot>:`` tag.
        2. If found (UPDATE): rename via ``async_set_user`` with the
           existing ``user_id``, return that id.
        3. If not (legacy adoption): scan for an *untagged* user whose
           ``user_id == slot`` that also owns a PIN at ``credential.slot
           == slot``. Pre-PR-C LCM pinned ``user_id`` to the slot, so
           such a user is almost certainly the LCM 2.0 user for this
           slot. Adopting it preserves a single per-slot anchor across
           the upgrade.
        4. Otherwise (CREATE): allocate a fresh ``user_id`` via
           ``async_set_user(user_id=None)`` (Z-Wave finds first free).

        ``user.user_id`` set by the seam is used only as the slot identity
        when ``user.name`` is untagged (defensive fallback; the seam
        always passes a tagged name on this code path).
        """
        slot = self._slot_from_seam_user(user)
        existing_user_id = await self._find_user_index_for_slot(slot)
        write_user_id: int | None = existing_user_id
        try:
            result = await lock_helpers.async_set_user(
                self.node,
                user_id=write_user_id,
                user_name=user.name,
                active=user.active,
            )
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"set user for slot {slot} failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(
                f"set user for slot {slot} failed: {err}"
            ) from err
        return SetUserResult(
            user_id=result["user_id"], created=existing_user_id is None
        )

    def _slot_from_seam_user(self, user: User) -> int:
        """
        Return the LCM slot encoded in ``user.name`` or fall back to ``user_id``.

        The base seam always passes a tagged name; this helper centralizes
        the fallback so the rest of the provider can treat the resolved
        slot as a single value.
        """
        if user.name:
            slot, _ = parse_tag(user.name)
            if slot is not None:
                return slot
        return user.user_id

    async def _find_user_index_for_slot(self, slot: int) -> int | None:
        """
        Return the lock ``user_id`` LCM owns for ``slot``, if any.

        Two lookups, in priority order:

        1. **Canonical** -- a user whose name carries the ``lcm:<slot>:``
           tag. This is the post-PR-C identity rule.
        2. **Legacy adoption** -- an *untagged* user whose ``user_id ==
           slot`` AND who owns a Personal Identification Number
           credential at ``credential.slot == slot``. Pre-PR-C LCM pinned
           ``user_id`` to the slot, so a user matching both halves of the
           old invariant is almost certainly the LCM 2.0 user for this
           slot. Adopting it (the subsequent ``async_set_user`` rewrites
           the name to the tagged form) preserves a single identifiable
           user per slot across the upgrade. Without this fallback the
           new model would CREATE a second user every time, silently
           leaving the pre-upgrade Personal Identification Number active
           on the lock.

           The legacy pass MUST skip users whose names already parse to
           ANY LCM slot, so a user tagged for slot A is never adopted as
           slot B's anchor.

        Returns ``None`` when neither lookup matches.
        """
        users = await self.async_get_users()
        try:
            return next(
                existing.user_id
                for existing in users
                if existing.name and parse_tag(existing.name)[0] == slot
            )
        except StopIteration:
            return next(
                (
                    existing.user_id
                    for existing in users
                    if existing.user_id == slot
                    and parse_tag(existing.name or "")[0] is None
                    for cred in existing.pin_credentials
                    if cred.slot == slot
                ),
                None,
            )

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
        pin: str,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """
        Write the PIN credential under user_id; map device rejections.

        Bypasses ``lock_helpers.async_set_credential`` and calls
        ``node.access_control.set_credential`` directly. The HA helper
        validates ``1 <= slot <= num_slots`` using
        ``get_credential_capabilities_cached()`` which returns U3C-only
        data -- on a UC-only lock that means zero slots, and the helper
        rejects with ``Credential slot for pin_code must be between 1
        and 0`` (issue #1251). The driver-level call unifies UC and U3C
        internally per the node-zwave-js v15.23.4+ contract, so going
        direct works for both lock generations. We map the
        ``SetCredentialResult`` status enum to LCM's typed exceptions
        in place of HA's translation-key flow.
        """
        try:
            status = await self.node.access_control.set_credential(
                user_id,
                UserCredentialType.PIN_CODE,
                credential.slot,
                pin,
            )
        except BaseZwaveJSServerError as err:
            # Transient Z-Wave command failure (e.g. a sleeping/battery lock):
            # route to retry rather than slot suspension.
            raise LockDisconnected(
                f"set credential slot {credential.slot} failed: {err}"
            ) from err
        if status is SetCredentialResult.OK:
            return True
        if status in (
            SetCredentialResult.ERROR_DUPLICATE_CREDENTIAL,
            SetCredentialResult.ERROR_DUPLICATE_ADMIN_PIN_CODE,
        ):
            raise DuplicateCodeError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
            )
        raise CodeRejectedError(
            code_slot=credential.slot,
            lock_entity_id=self.lock.entity_id,
            reason=f"lock rejected with status {status.name}",
        )

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ref.

        Bypasses ``lock_helpers.async_delete_credential`` for the same
        reason as ``async_set_credential`` -- the helper's slot
        validation breaks for UC-only locks where
        ``get_credential_capabilities_cached()`` reports zero slots.
        Calls ``node.access_control.delete_credential`` directly so
        the driver's unified UC/U3C dispatch handles both lock
        generations.
        """
        try:
            status = await self.node.access_control.delete_credential(
                ref.user_id,
                UserCredentialType.PIN_CODE,
                ref.slot,
            )
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(
                f"delete credential slot {ref.slot} failed: {err}"
            ) from err
        if status is SetCredentialResult.OK:
            return True
        raise LockOperationFailed(
            f"delete credential slot {ref.slot} failed: "
            f"lock rejected with status {status.name}"
        )

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

        try:
            for name, handler in (
                ("credential added", self._on_credential_changed),
                ("credential modified", self._on_credential_changed),
                ("credential deleted", self._on_credential_deleted),
            ):
                self._register_push_unsub(self.node.on(name, handler))
        except ValueError as err:
            self._clear_push_unsubs()
            raise LockDisconnected(f"node not ready: {err}") from err

    @callback
    def _on_credential_changed(self, event: dict[str, Any]) -> None:
        """
        Handle credential added/modified events from the node.

        The event carries the value when the lock includes it (e.g. an
        out-of-band keypad change), so push the readable state rather
        than always unreadable -- otherwise the slot would be stranded
        as unreadable until the next set/clear or hard refresh.
        """
        args = event["args"]  # CredentialChangedArgs (pre-parsed by the library)
        if args.credential_type != UserCredentialType.PIN_CODE:
            return
        self._push_credential_update(args.credential_slot, self._pin_state(args.data))

    @callback
    def _on_credential_deleted(self, event: dict[str, Any]) -> None:
        """Handle credential deleted events from the node."""
        args = event["args"]  # CredentialDeletedArgs (pre-parsed by the library)
        if args.credential_type != UserCredentialType.PIN_CODE:
            return
        self._push_credential_update(args.credential_slot, SlotCredential.empty())

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
        Capability validation runs in the base ``async_setup_internal``.
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
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"hard refresh failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"hard refresh failed: {err}") from err
        return await self.async_get_usercodes()
