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
from zwave_js_server.exceptions import BaseZwaveJSServerError, NotFoundError
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
    WriteResult,
)
from ..domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
)
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import parse_tag

_LOGGER = logging.getLogger(__name__)

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
    """
    Class to represent ZWave JS lock.

    PIN management runs entirely through node-zwave-js's unified
    ``access_control`` API, which dispatches to User Code CC or User
    Credential CC internally. This relies on the driver fixes in
    zwave-js 15.24.3 (spec-compliant interview that defers to User Code
    CC when User Credential CC is inactive, and tolerant masked-code
    write verification) -- guaranteed by the integration's minimum Home
    Assistant version. The legacy User Code CC value-path fallback that
    worked around the pre-15.24.3 capability bug (#1251) has been removed.

    One temporary bridge remains: the User Code CC report shim (grep
    ``_uc_``), which compensates for report-driven User Code CC changes
    emitting no unified credential events on released drivers. See the
    shim section below for details and the removal recipe.
    """

    lock_config_entry: ConfigEntry = field(repr=False)
    # Home Assistant event-bus listeners (separate lifecycle from push
    # subscriptions: registered in ``async_setup``, released in
    # ``async_unload``).
    _listeners: list[Callable[[], None]] = field(init=False, default_factory=list)

    @property
    def node(self) -> Node:
        """
        Return ZWave JS node.

        Home Assistant's helper raises a bare ``ValueError`` while the
        zwave_js config entry is still loading (issue #1321); translate it
        to ``LockDisconnected`` so callers route it to the degraded/retry
        path instead of treating it as an unexpected crash.
        """
        try:
            return async_get_node_from_entity_id(
                self.hass, self.lock.entity_id, self.ent_reg
            )
        except ValueError as err:
            raise LockDisconnected(f"Z-Wave JS node unavailable: {err}") from err

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
        Disable periodic drift detection: Z-Wave is event-driven end to end.

        Both User Code CC and User Credential CC push change notifications, so a
        periodic re-read would only catch out-of-band programming that arrived
        with no event -- and on a masking lock a re-read cannot recover the value
        anyway, so it would not even resolve the slot it claims to protect. LCM
        does not chase silent out-of-band changes, so the hourly poll earned its
        keep for neither readability nor occupancy. Hard refresh is still used on
        demand (initial load, missing/unknown slots) and by the per-write
        confirmation read -- just not on a timer.
        """
        return None

    @property
    def supports_native_users(self) -> bool:
        """Return True: this provider implements the credential primitives."""
        return True

    def _pin_state(self, data: str | bytes | None) -> SlotCredential:
        """
        Project Z-Wave credential data to a SlotCredential.

        Universal across both command classes (User Code CC and User
        Credential CC): a present slot whose code we can read becomes
        ``known``; a present slot whose code is withheld becomes
        ``unreadable``. Many locks report the PIN back masked (all
        asterisks) or omit it entirely for security -- those carry no
        comparable value, so they are unreadable, NOT ``known`` of the
        masked string (which would surface a wrong PIN and never
        reconcile against the configured one).
        """
        if not data:
            return SlotCredential.unreadable()
        # CredentialData.data is str | bytes; decode bytes so a Personal
        # Identification Number is the digit string, not "b'1234'".
        code = data if isinstance(data, str) else data.decode()
        if not code or code == "*" * len(code):
            return SlotCredential.unreadable()
        return SlotCredential.known(code)

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
        or U3C internally per node-zwave-js v15.24.3+. A User Code CC
        lock surfaces one user per occupied slot here too, because the
        unified API models each User Code CC slot as an implicit user
        carrying its single PIN credential.
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
        Report the lock's user/credential capabilities via the unified API.

        Every lock -- U3C or User Code CC -- routes through the unified
        ``access_control`` API, which dispatches to the right command class
        internally (zwave-js 15.24.3+ defers to User Code CC when User
        Credential CC is inactive). Masked-code locks stay correct via the
        universal read projection (``_pin_state`` maps a withheld code to
        ``unreadable``) and tolerant write handling (a driver
        ``ERROR_UNKNOWN`` from the masked read-back verification is treated
        as a completed set in ``async_set_credential``, not a rejection).

        ``num_slots == 0`` while a PIN type is advertised is no longer a
        routing branch -- it means the node interview is incomplete or the
        driver is too old. Before concluding that, a one-shot recovery
        query re-reads the users count from the device (see
        ``_async_recover_user_code_slot_count``); only when the re-read is
        still degenerate do we raise an actionable error (issue #1298).
        """
        caps = await self._async_read_credential_capabilities()
        pin = caps["supported_credential_types"].get(_PIN_TYPE_STR)

        if (
            pin is not None
            and pin["num_slots"] == 0
            and self._node_advertises_user_code_cc()
            and await self._async_recover_user_code_slot_count()
        ):
            caps = await self._async_read_credential_capabilities()
            pin = caps["supported_credential_types"].get(_PIN_TYPE_STR)

        if pin and pin["num_slots"] > 0:
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

        if pin is not None:
            # PIN type advertised but zero usable slots. Re-reading can't
            # conjure the missing slot values: either the node interview is
            # incomplete (it completed while the lock was asleep) or the
            # connected Z-Wave JS driver predates the spec-compliant
            # capability fix (15.24.3). Surface the real remedy rather than
            # the base's misleading "does not advertise PIN credential
            # support" (issue #1298).
            raise LockCodeManagerProviderError(
                f"{self.lock.entity_id}: lock reports no usable PIN slots -- "
                "the node interview is likely incomplete or the Z-Wave JS "
                "driver is older than 15.24.3. Re-interview the lock with it "
                "awake and update Z-Wave JS, then reload."
            )

        # No PIN credential type at all: the lock genuinely has no PIN
        # support LCM can manage. Base setup rejects this with its generic
        # "does not advertise PIN credential support" message, which is
        # accurate here.
        return LockCapabilities(
            supports_user_management=False,
            max_users=0,
            credential_types={},
            max_user_name_length=0,
        )

    async def _async_read_credential_capabilities(self) -> dict[str, Any]:
        """Read raw credential capabilities, mapping transport errors."""
        try:
            return await lock_helpers.async_get_credential_capabilities(self.node)
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(f"get capabilities failed: {err}") from err
        except HomeAssistantError as err:
            raise LockOperationFailed(f"get capabilities failed: {err}") from err

    async def _async_recover_user_code_slot_count(self) -> bool:
        """
        Re-query the User Code CC users count so the driver caches it.

        The driver derives a User Code CC lock's PIN slot count from the
        cached ``supportedUsers`` value and reports 0 when it is missing
        from the value database -- the state a battery lock lands in when
        its interview completes while asleep (common right after a
        factory-reset re-inclusion, issue #1298). ``refreshValues`` on the
        CC reads that same cached value rather than re-querying it, so the
        only primitive that repopulates it is the CC API ``getUsersCount``
        device query: the solicited UsersNumberReport persists
        ``supportedUsers``, after which the cached capability read serves
        real numbers.

        Returns True when the query completed and a capability re-read is
        worthwhile; False when it failed (the caller falls through to the
        actionable structural error).
        """
        _LOGGER.info(
            "Lock %s reports zero PIN slots; re-querying the User Code CC "
            "users count once before concluding the lock is unusable",
            self.lock.entity_id,
        )
        # .get() rather than subscription: a KeyError from a node model
        # missing its root endpoint would escape past every typed handler
        # and get the lock dropped instead of degraded.
        endpoint = self.node.endpoints.get(0)
        if endpoint is None:
            return False
        try:
            await endpoint.async_invoke_cc_api(CommandClass.USER_CODE, "getUsersCount")
        except BaseZwaveJSServerError as err:
            _LOGGER.debug(
                "Lock %s: users count recovery query failed: %s",
                self.lock.entity_id,
                err,
            )
            return False
        return True

    async def async_set_user(self, user: User) -> SetUserResult:
        """
        Find-or-create the lock user for the LCM slot encoded in ``user.name``.

        The base seam passes a tagged ``user.name`` (``lcm:<slot>:<display>``)
        whose slot is the LCM-side identity for this credential. The Z-Wave
        lock's own ``user_id`` is whatever Z-Wave happens to allocate; LCM
        treats it as opaque and rediscovers it via the tag on every call:

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
    ) -> WriteResult:
        """
        Write the PIN credential under user_id; map device rejections.

        The write goes through HA's ``lock_helpers.async_set_credential``,
        whose translation-key errors are mapped to LCM's typed exceptions.

        A driver ``ERROR_UNKNOWN`` (HA key ``credential_rejected_unknown``)
        is treated as a COMPLETED-but-unconfirmed set rather than a
        rejection: the driver returns it when its post-write verification
        can't confirm the code, which happens for genuinely write-only or
        masked/withheld locks -- there the write actually succeeded
        (``userIdStatus`` -> Enabled). The seam's verified-credential
        lifecycle records it pending and reconciles it (last-set tracking
        + the masked-as-unreadable read-back) rather than permanently
        disabling an accepted write. (zwave-js 15.24.3 fixed the narrower
        User Code CC v1 case where code obfuscation produced this error
        falsely; this branch now guards the remaining unverifiable writes.)
        Definitive rejections (duplicate, occupied, manufacturer rules,
        validation) still surface as typed errors.
        """
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
            key = getattr(err, "translation_key", None)
            if key == "credential_rejected_unknown":
                _LOGGER.debug(
                    "Lock %s slot %s: driver returned ERROR_UNKNOWN; treating "
                    "as an optimistic (unconfirmed) set -- the lock is "
                    "write-only or reports the code back masked. The seam "
                    "records it pending until a credential event or hard "
                    "refresh confirms it; otherwise it re-syncs: %s",
                    self.lock.entity_id,
                    credential.slot,
                    err,
                )
                # No reconciliation read here: the seam's on-demand
                # confirmation read hard-refreshes for every OPTIMISTIC
                # write, so a single-slot read would be pure duplication.
                return WriteResult.OPTIMISTIC
            # Definitive rejection: pre-15.25.2 drivers never re-read the
            # slot after a supervised failure, so an already-stale cache
            # entry would stay wrong forever (see
            # _async_uc_reconcile_value_db).
            await self._async_uc_reconcile_value_db(credential.slot)
            if key == "credential_rejected_duplicate":
                raise DuplicateCodeError(
                    code_slot=credential.slot,
                    lock_entity_id=self.lock.entity_id,
                ) from err
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason=str(err),
            ) from err
        # Pre-15.25.2 drivers never persist a supervised success to the
        # value database (see _async_uc_reconcile_value_db).
        await self._async_uc_reconcile_value_db(credential.slot)
        return WriteResult.CONFIRMED

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Delete the credential addressed by ref.

        The clear goes through HA's ``lock_helpers.async_delete_credential``.
        """
        try:
            await lock_helpers.async_delete_credential(
                self.node, ref.user_id, UserCredentialType.PIN_CODE, ref.slot
            )
        except BaseZwaveJSServerError as err:
            raise LockDisconnected(
                f"delete credential slot {ref.slot} failed: {err}"
            ) from err
        except HomeAssistantError as err:
            # Same supervised-failure staleness as the set path (see
            # _async_uc_reconcile_value_db). Success needs no read: the
            # driver clears its cached User Code CC values on a
            # successful delete since 15.24.3 (zwave-js/zwave-js#8866).
            await self._async_uc_reconcile_value_db(ref.slot)
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
        """
        Subscribe to credential change events.

        The driver emits ``credential added/modified/deleted`` node events
        from its unified ``access_control`` API for both User Code CC and
        User Credential CC locks. The handlers are self-filtering and
        pushes are idempotent.

        On nodes that advertise User Code CC, also subscribe to raw
        ``value updated`` events for the User Code CC report shim (see
        that section below): released drivers emit no unified events for
        report-driven User Code CC changes, and both listener sets are
        safe together because the handlers self-filter and pushes are
        idempotent.
        """
        if self._push_unsubs:
            return

        ready, reason = self._get_client_state()
        if not ready:
            raise LockDisconnected(reason)

        subscriptions: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
            ("credential added", self._on_credential_changed),
            ("credential modified", self._on_credential_changed),
            ("credential deleted", self._on_credential_deleted),
        ]
        if self._node_advertises_user_code_cc():
            subscriptions.append(("value updated", self._on_uc_value_updated))

        try:
            for name, handler in subscriptions:
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
        # Route through _confirm_slot: a credential event confirms a pending
        # optimistic write (keeping the believed value even when the lock
        # reports it masked); otherwise it is an external change taken as-is.
        self._confirm_slot(args.credential_slot, self._pin_state(args.data))

    @callback
    def _on_credential_deleted(self, event: dict[str, Any]) -> None:
        """Handle credential deleted events from the node."""
        args = event["args"]  # CredentialDeletedArgs (pre-parsed by the library)
        if args.credential_type != UserCredentialType.PIN_CODE:
            return
        self._confirm_slot(args.credential_slot, SlotCredential.empty())

    # ------------------------------------------------------------------
    # User Code CC report shim
    #
    # Bridges an upstream gap on released drivers: on locks the driver
    # dispatches to User Code CC (User Code CC-only locks, and dual-CC
    # locks whose User Credential CC advertises zero users), any change
    # that arrives as a *report* -- keypad programming, zwave-js-ui
    # edits, Home Assistant's ``zwave_js.set/clear_lock_usercode``
    # services, and the driver's own post-write verification polls --
    # updates the driver's value database without emitting a unified
    # credential event. LCM disables periodic polling for push
    # providers, so without this shim those changes would never reach
    # the coordinator and sync could not reconcile them.
    #
    # zwave-js/zwave-js#8930 (merged to the v16 branch, unreleased)
    # fixes this by making the access-control API the source of truth
    # for User Code CC. It also makes these User Code CC values
    # internal -- and the driver does not emit value events for
    # internal value IDs -- so once a driver with it ships the value
    # events stop arriving and the unified events we already subscribe
    # to take over: the shim goes dormant on its own, no LCM change
    # needed.
    #
    # The shim also carries ``_async_uc_reconcile_value_db``, a
    # post-write single-slot read that bridges a second gap fixed in
    # driver 15.25.2 (zwave-js/zwave-js#8927): before that, supervised
    # User Code CC writes through the unified API never persisted to
    # (success) or reconciled (failure) the driver's value database,
    # so cached reads served stale slots until the next report.
    #
    # To remove the whole section once the minimum supported driver
    # includes #8930 (which implies #8927):
    #
    # 1. Delete everything from this comment through
    #    ``_async_uc_reconcile_value_db``.
    # 2. Delete the ``_node_advertises_user_code_cc`` branch in
    #    ``setup_push_subscription`` (and its docstring paragraph).
    # 3. Delete the ``_async_uc_reconcile_value_db`` call sites in
    #    ``async_set_credential`` and ``async_delete_credential``.
    # 4. Delete the shim tests in
    #    ``tests/providers/zwave_js/test_events.py`` and
    #    ``tests/providers/zwave_js/test_provider.py`` (grep ``_uc_``)
    #    and restore ``_EXPECTED_PUSH_UNSUB_COUNT`` to 3.
    # ------------------------------------------------------------------

    def _node_advertises_user_code_cc(self) -> bool:
        """Return whether the node's endpoint 0 advertises User Code CC."""
        return any(cc.id == CommandClass.USER_CODE for cc in self.node.command_classes)

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

        if property_name == LOCK_USERCODE_STATUS_PROPERTY:
            self._handle_uc_status_update(code_slot, args.get("newValue"))
        else:
            self._handle_uc_code_update(code_slot, args.get("newValue"))

    @callback
    def _handle_uc_status_update(self, code_slot: int, status: Any) -> None:
        """Handle a userIdStatus value update for a code slot."""
        if status != CodeSlotStatus.AVAILABLE:
            # Occupied statuses carry no code; the paired userCode update
            # delivers the value.
            return
        # Ignore AVAILABLE when Lock Code Manager expects a PIN on this
        # slot. Some locks send stale AVAILABLE events after a code was
        # set, which would cause infinite sync loops.
        if (
            self.coordinator is not None
            and self.coordinator.desired_credential(code_slot).is_present
        ):
            _LOGGER.debug(
                "Lock %s: ignoring userIdStatus=AVAILABLE for slot %s "
                "(LCM expects PIN on this slot)",
                self.lock.entity_id,
                code_slot,
            )
            return
        self._confirm_slot(code_slot, SlotCredential.empty())

    @callback
    def _handle_uc_code_update(self, code_slot: int, new_value: Any) -> None:
        """Handle a userCode value update for a code slot."""
        if not new_value:
            resolved = SlotCredential.empty()
        else:
            value = str(new_value)
            slot_in_use = self._uc_slot_in_use(code_slot)
            # Asymmetric in_use checks: masked codes count as unreadable
            # even when in_use is None (some firmwares mask before
            # reporting status), but all-zeros only counts as empty when
            # in_use is explicitly False (zeros from a partially-loaded
            # cache must not be misread as cleared).
            if value == "*" * len(value) and slot_in_use is not False:
                resolved = SlotCredential.unreadable()
            elif value.strip("0") == "" and slot_in_use is False:
                resolved = SlotCredential.empty()
            else:
                resolved = SlotCredential.known(value)
        # Route through _confirm_slot like the unified handlers: the
        # driver's post-write verification report doubles as the
        # confirming push for a pending optimistic write.
        self._confirm_slot(code_slot, resolved)

    def _uc_slot_in_use(self, code_slot: int) -> bool | None:
        """Return whether a User Code CC slot is in use, None when unknown."""
        try:
            in_use = get_usercode(self.node, code_slot).get(ATTR_IN_USE)
        except NotFoundError:
            return None
        return in_use if isinstance(in_use, bool) else None

    async def _async_uc_reconcile_value_db(self, code_slot: int) -> None:
        """
        Read one slot back from the device so the driver's cache converges.

        Pre-15.25.2 drivers never persist a supervised User Code CC write
        to the value database (success) and never re-read the slot after a
        failure, so every later cached read -- including LCM's own initial
        load after a restart -- serves the pre-write state. This fresh
        single-slot read through the unified API forces the driver to
        query the device: on the User Code CC dispatch path the solicited
        report repairs the value database and doubles as a push through
        the report shim above; on a User Credential CC lock the driver
        dispatches natively and the read is merely redundant.

        Best-effort by design: the write already concluded (the caller
        returns or raises on its own evidence), so a failed read must not
        change the outcome -- the next hard refresh or sync tick reconciles
        instead.
        """
        if not self._node_advertises_user_code_cc():
            return
        try:
            await self.node.access_control.get_credential(
                UserCredentialType.PIN_CODE, code_slot
            )
        except BaseZwaveJSServerError as err:
            _LOGGER.debug(
                "Lock %s slot %s: post-write reconciliation read failed (%s); "
                "leaving it to the next hard refresh or sync tick",
                self.lock.entity_id,
                code_slot,
                err,
            )
        except Exception:
            # Broad by design, mirroring the coordinator's confirmation-read
            # backstop: two call sites run inside except clauses mapping the
            # write outcome to typed errors, and an escaping exception here
            # would replace that typed error and derail the seam's handling.
            _LOGGER.exception(
                "Lock %s slot %s: unexpected error during post-write "
                "reconciliation read; leaving it to the next hard refresh "
                "or sync tick",
                self.lock.entity_id,
                code_slot,
            )

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
