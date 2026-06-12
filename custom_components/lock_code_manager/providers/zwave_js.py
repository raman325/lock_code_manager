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
import functools
import logging
from typing import Any, Literal

from zwave_js_server.client import Client
from zwave_js_server.const import CommandClass, NodeStatus, SetValueStatus
from zwave_js_server.const.command_class.access_control import UserCredentialType
from zwave_js_server.const.command_class.lock import (
    ATTR_CODE_SLOT,
    ATTR_IN_USE,
    ATTR_USERCODE,
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    NotificationType,
)
from zwave_js_server.exceptions import BaseZwaveJSServerError, NotFoundError
from zwave_js_server.model.node import Node
from zwave_js_server.util.lock import (
    clear_usercode,
    get_usercode,
    get_usercode_from_node,
    get_usercodes,
    set_usercode,
)

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


# SetValueResult statuses that mean a User Code CC value write was accepted.
_UC_SET_VALUE_OK = (
    SetValueStatus.SUCCESS,
    SetValueStatus.SUCCESS_UNSUPERVISED,
    SetValueStatus.WORKING,
)

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
    # Whether the unified access-control API is unusable for PIN management
    # on this lock and the legacy User Code CC utilities must be used
    # instead. None until ``async_get_capabilities`` runs the detection.
    # Deliberately stored (not re-probed per operation) so it shares the
    # lifetime of the base's capabilities cache: the seam's slot-only
    # routing decision is frozen from the same snapshot, and flipping one
    # without the other would route writes incoherently. Both reset
    # together when the provider is rebuilt (HA restart, LCM reload, or a
    # zwave_js entry reload -- which a driver upgrade always triggers),
    # so a lock healed by the upstream fix lands back in unified mode on
    # its next reload with no LCM change.
    _uc_fallback: bool | None = field(init=False, default=None)
    # Slot of a UC-fallback set operation currently in flight. User Code CC
    # has no in-band duplicate-rejection result; some firmwares report a
    # duplicate via an Access Control notification instead (sometimes with
    # userId=0). Tracking the in-flight slot lets the notification handler
    # attribute that rejection to the right slot.
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
        or U3C internally per node-zwave-js v15.23.4+. When that API is
        unusable for this lock (see ``async_get_capabilities``), users
        are synthesized from the User Code CC value DB instead: one
        implicit user per occupied slot with ``user_id == slot``,
        matching the User Code CC model where the user IS the credential.
        """
        if await self._async_uc_fallback_active():
            return await self._async_uc_users_from_value_db()
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

        The unified ``access_control`` API in node-zwave-js computes its
        capabilities from cached interview data; on some locks that data
        is degenerate -- the PIN credential type comes back either
        missing or advertising ``num_slots=0`` even though the lock
        manages codes fine through the legacy User Code CC (issue
        #1251, upstream fix in zwave-js/zwave-js#8873). When that
        happens AND the node actually advertises User Code CC, we fall
        back to reading the lock's UC slot count from the value DB and
        return slot-only capabilities: ``supports_user_management=False``
        and ``max_user_name_length=0``, which the seam recognizes as a
        slot-only lock and routes through the credential-only
        primitives (``async_set_credential`` / ``async_delete_credential``
        / ``async_get_users``) without the user lifecycle. All
        credential operations then use the User Code CC utilities
        directly. Once the upstream fix ships and the unified API
        reports usable PIN capabilities for these locks, the fallback
        detection stops triggering on its own.
        """
        try:
            caps = await lock_helpers.async_get_credential_capabilities(self.node)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get capabilities failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"get capabilities failed: {err}") from err
        pin = caps["supported_credential_types"].get(_PIN_TYPE_STR)

        if pin and pin["num_slots"] > 0:
            # The unified API advertises real PIN credential slots.
            self._uc_fallback = False
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

        # Degenerate unified capabilities. Only fall back when the node
        # advertises User Code CC -- without it the legacy utilities
        # cannot work either, and the lock genuinely has no PIN support
        # LCM can manage.
        # ``get_usercodes`` walks slot 1, 2, 3, ... in the value DB until
        # ``NotFoundError``, so the returned list length is the lock's
        # actual UC slot count. The function only raises ``NotFoundError``
        # internally (caught there) and is otherwise pure value-DB
        # walking, so we let any unexpected exception surface rather
        # than silently mis-routing the lock to "no PIN support".
        uc_slots = (
            get_usercodes(self.node) if self._node_supports_user_code_cc() else []
        )
        if not uc_slots:
            self._uc_fallback = False
            return LockCapabilities(
                supports_user_management=False,
                max_users=0,
                credential_types={},
                max_user_name_length=0,
            )
        _LOGGER.warning(
            "Lock %s: unified access-control API reports no usable PIN "
            "capabilities but the node supports User Code CC with %s slots; "
            "falling back to legacy User Code CC handling (see issue #1251)",
            self.lock.entity_id,
            len(uc_slots),
        )
        self._uc_fallback = True
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

    def _node_supports_user_code_cc(self) -> bool:
        """Return whether the node's endpoint 0 advertises User Code CC."""
        return any(cc.id == CommandClass.USER_CODE for cc in self.node.command_classes)

    @functools.cached_property
    def _usercode_cc_version(self) -> int:
        """Return the User Code CC version supported by this node."""
        version = next(
            (
                cc.version
                for cc in self.node.command_classes
                if cc.id == CommandClass.USER_CODE
            ),
            0,
        )
        if version == 0:
            _LOGGER.warning(
                "Lock %s: User Code CC not found on node %s. This may "
                "indicate an incomplete interview. Defaulting to V1 behavior",
                self.lock.entity_id,
                self.node.node_id,
            )
            return 1
        return version

    async def _async_uc_fallback_active(self) -> bool:
        """
        Return whether PIN operations must use the User Code CC fallback.

        The flag is computed by ``async_get_capabilities``; when a
        credential operation arrives before any capability probe (e.g. a
        direct service call right after a reload), run the probe first so
        routing never guesses.
        """
        if self._uc_fallback is None:
            await self._get_cached_capabilities()
        return bool(self._uc_fallback)

    @staticmethod
    def _uc_slot_state(in_use: bool | None, usercode: str | None) -> SlotCredential:
        """
        Project a User Code CC slot to a ``SlotCredential``.

        Masked codes (all asterisks) and occupied slots without a cached
        value count as unreadable; an unknown ``in_use`` (None) with no
        value counts as empty, matching the legacy 3.x reader.
        """
        if not in_use:
            return SlotCredential.empty()
        if not usercode:
            return SlotCredential.unreadable()
        code = str(usercode)
        if code == "*" * len(code):
            return SlotCredential.unreadable()
        return SlotCredential.known(code)

    async def _async_uc_users_from_value_db(self) -> list[User]:
        """
        Synthesize one implicit user per occupied User Code CC slot.

        User Code CC has no user records -- the user IS the credential --
        so each occupied slot becomes a user with ``user_id == slot``
        carrying its single PIN credential. The seam's slot projection
        and owner-resolution lookups (untagged user with ``user_id ==
        slot`` owning a PIN at ``credential.slot == slot``) match this
        shape via their legacy fallback path.

        When any managed slot is missing from the value DB or has an
        unknown ``in_use`` state, do one hard refresh before projecting
        so a partially populated cache is not misread as empty slots.
        """
        try:
            slots = get_usercodes(self.node)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get usercodes failed: {err}") from err
        slots_by_num = {int(slot[ATTR_CODE_SLOT]): slot for slot in slots}
        if any(
            slot_num not in slots_by_num
            or slots_by_num[slot_num].get(ATTR_IN_USE) is None
            for slot_num in self.managed_slots
        ):
            _LOGGER.debug(
                "Lock %s has missing/unknown slots, performing hard refresh",
                self.lock.entity_id,
            )
            await self._async_refresh_usercode_cache()
            try:
                slots = get_usercodes(self.node)
            except BaseZwaveJSServerError as err:
                raise LockDisconnected(f"get usercodes failed: {err}") from err

        users: list[User] = []
        for slot_info in slots:
            slot = int(slot_info[ATTR_CODE_SLOT])
            state = self._uc_slot_state(
                slot_info.get(ATTR_IN_USE), slot_info.get(ATTR_USERCODE)
            )
            if state.is_empty:
                continue
            user = User(user_id=slot, name=None, active=True)
            user.credentials.append(
                Credential(type=CredentialType.PIN, slot=slot, state=state)
            )
            users.append(user)
        return users

    async def _async_refresh_usercode_cache(self) -> None:
        """Refresh all User Code CC values from the device."""
        try:
            await self.node.async_refresh_cc_values(CommandClass.USER_CODE)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"usercode cache refresh failed: {err}") from err

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

        In UC-fallback mode the write goes through the legacy User Code
        CC utilities (``set_usercode``), which address the slot directly
        and never consult the unified API's broken capability data.
        Otherwise the write goes through HA's
        ``lock_helpers.async_set_credential``, whose translation-key
        errors are mapped to LCM's typed exceptions.
        """
        if await self._async_uc_fallback_active():
            return await self._async_uc_set_usercode(credential.slot, pin)
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
            # route to retry rather than slot suspension.
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
        """
        Delete the credential addressed by ref.

        In UC-fallback mode the clear goes through the legacy User Code
        CC utilities (``clear_usercode``); otherwise through HA's
        ``lock_helpers.async_delete_credential``.
        """
        if await self._async_uc_fallback_active():
            return await self._async_uc_clear_usercode(ref.slot)
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

    async def _async_uc_set_usercode(self, code_slot: int, usercode: str) -> bool:
        """
        Write a usercode through the legacy User Code CC value path.

        Returns False without writing when the cached value already
        matches (masked codes never match, so they are always
        rewritten). After a successful write, V1 locks are polled to
        force-update the value DB (they don't reliably report back),
        and the new state is pushed optimistically so the next sync
        tick doesn't read a stale cache and loop.
        """
        try:
            current = get_usercode(self.node, code_slot)
        except NotFoundError:
            current = None
        if current and current.get(ATTR_IN_USE):
            current_code = str(current.get(ATTR_USERCODE) or "")
            if current_code != "*" * len(current_code) and usercode == current_code:
                _LOGGER.debug(
                    "Lock %s slot %s already has this PIN, skipping set",
                    self.lock.entity_id,
                    code_slot,
                )
                return False

        self._set_in_progress_code_slot = code_slot
        try:
            result = await set_usercode(self.node, code_slot, usercode)
        except NotFoundError as err:
            self._set_in_progress_code_slot = None
            raise CodeRejectedError(
                code_slot=code_slot,
                lock_entity_id=self.lock.entity_id,
                reason=f"slot not found on lock: {err}",
            ) from err
        except BaseZwaveJSServerError as err:
            self._set_in_progress_code_slot = None
            raise LockDisconnected(
                f"set usercode slot {code_slot} failed: {err}"
            ) from err
        if result is not None and result.status not in _UC_SET_VALUE_OK:
            self._set_in_progress_code_slot = None
            raise CodeRejectedError(
                code_slot=code_slot,
                lock_entity_id=self.lock.entity_id,
                reason=f"set value returned {result.status.name}",
            )
        await self._async_uc_verify_write(code_slot, "set")
        # Optimistic update: the value cache updates asynchronously via push
        # notification; push now to prevent sync loops from reading stale cache.
        self._push_credential_update(code_slot, SlotCredential.known(usercode))
        return True

    async def _async_uc_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode through the legacy User Code CC value path.

        Returns False without writing when the slot is already clear.
        Mirrors ``_async_uc_set_usercode`` for verification and the
        optimistic push.
        """
        try:
            current = get_usercode(self.node, code_slot)
        except NotFoundError:
            current = None
        if current is not None and not current.get(ATTR_IN_USE):
            _LOGGER.debug(
                "Lock %s slot %s already cleared, skipping clear",
                self.lock.entity_id,
                code_slot,
            )
            return False

        try:
            result = await clear_usercode(self.node, code_slot)
        except NotFoundError as err:
            raise LockOperationFailed(
                f"clear usercode slot {code_slot} failed: {err}"
            ) from err
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(
                f"clear usercode slot {code_slot} failed: {err}"
            ) from err
        if result is not None and result.status not in _UC_SET_VALUE_OK:
            raise LockOperationFailed(
                f"clear usercode slot {code_slot} failed: "
                f"set value returned {result.status.name}"
            )
        await self._async_uc_verify_write(code_slot, "clear")
        # Optimistic update: see _async_uc_set_usercode for rationale.
        self._push_credential_update(code_slot, SlotCredential.empty())
        return True

    async def _async_uc_verify_write(
        self, code_slot: int, operation: Literal["set", "clear"]
    ) -> None:
        """
        Force-update the value cache after a set/clear on a V1 lock.

        V1 locks don't reliably update the Z-Wave JS value cache after a
        write. Poll the slot directly from the device to force-update the
        cache before the coordinator reads it, preventing sync loops.
        Wrap failures as LockDisconnected so they route to the retry path
        instead of suspending the slot.
        """
        if self._usercode_cc_version >= 2:
            return
        try:
            await get_usercode_from_node(self.node, code_slot)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(
                f"Post-{operation} verification poll failed for "
                f"{self.lock.entity_id} slot {code_slot}: {err}"
            ) from err

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
        """
        Subscribe to credential change events.

        In unified mode the driver emits ``credential added/modified/
        deleted`` node events. In UC-fallback mode those events never
        fire (the driver only emits them from its own unified API
        methods, which the fallback bypasses), so we subscribe to raw
        ``value updated`` events for the User Code CC values instead --
        the same push source the legacy 3.x provider used. When the
        mode is not yet known (capability probe hasn't run), subscribe
        to both; the handlers are self-filtering and pushes are
        idempotent.
        """
        if self._push_unsubs:
            return

        ready, reason = self._get_client_state()
        if not ready:
            raise LockDisconnected(reason)

        subscriptions: list[tuple[str, Callable[[dict[str, Any]], None]]] = []
        if self._uc_fallback is not False:
            subscriptions.append(("value updated", self._on_uc_value_updated))
        if not self._uc_fallback:
            subscriptions.extend(
                (
                    ("credential added", self._on_credential_changed),
                    ("credential modified", self._on_credential_changed),
                    ("credential deleted", self._on_credential_deleted),
                )
            )

        try:
            for name, handler in subscriptions:
                self._register_push_unsub(self.node.on(name, handler))
        except ValueError as err:
            self._clear_push_unsubs()
            raise LockDisconnected(f"node not ready: {err}") from err

    def _uc_code_slot_in_use(self, code_slot: int) -> bool | None:
        """Return whether a User Code CC slot is in use, None when unknown."""
        try:
            return get_usercode(self.node, code_slot)[ATTR_IN_USE]
        except NotFoundError, KeyError:
            return None

    @callback
    def _on_uc_value_updated(self, event: dict[str, Any]) -> None:
        """Handle ``value updated`` node events for User Code CC values."""
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
            self._handle_uc_status_update(code_slot, args.get("newValue"))
        else:
            self._handle_uc_value_update(code_slot, args.get("newValue"))

    @callback
    def _handle_uc_status_update(self, code_slot: int, status: Any) -> None:
        """Handle a userIdStatus value update for a code slot."""
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
    def _handle_uc_value_update(self, code_slot: int, new_value: Any) -> None:
        """Handle a userCode value update for a code slot."""
        if not new_value:
            resolved = SlotCredential.empty()
        else:
            value = str(new_value)
            slot_in_use = self._uc_code_slot_in_use(code_slot)
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

        # Handle duplicate code rejection — only when LCM initiated the set
        # (the in-progress slot is only tracked by the UC-fallback write
        # path; unified-mode writes report duplicates in-band). Mark the
        # slot as rejected so the sync manager raises DuplicateCodeError
        # on the next tick, routing through the standard CodeRejectedError
        # flow. Some Z-Wave lock firmwares report this notification with
        # userId=0 instead of the offending slot; treat 0 as referring to
        # the slot we're currently setting.
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
        if await self._async_uc_fallback_active():
            await self._async_refresh_usercode_cache()
            return await self.async_get_usercodes()
        try:
            await self.node.access_control.get_users()
            await self.node.access_control.get_all_credentials()
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"hard refresh failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"hard refresh failed: {err}") from err
        return await self.async_get_usercodes()
