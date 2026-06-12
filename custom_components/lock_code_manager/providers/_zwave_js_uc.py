"""
Temporary User Code CC fallback support for the Z-Wave JS provider.

Works around issue #1251: on some locks, node-zwave-js's unified
access-control API computes degenerate credential capabilities (PIN
missing or zero slots) from its cached interview data, and both Home
Assistant's ``lock_helpers`` and the driver itself validate every
credential write against that data -- bricking PIN management even
though the lock works fine through the legacy User Code CC. When the
node advertises User Code CC, this layer routes all PIN operations
through the legacy User Code CC value paths instead, restoring the
pre-4.0 behavior for exactly the population that needs it.

The upstream fix is zwave-js/zwave-js#8873. Once the minimum supported
driver includes it, the unified API reports usable capabilities for
these locks, the detection in ``_uc_fallback_capabilities`` stops
triggering, and everything here goes dormant. To remove the fallback
entirely at that point:

1. Delete this module.
2. Make ``ZWaveJSLock`` extend ``BaseLock`` directly again.
3. Delete the fallback branch points in ``zwave_js.py`` (grep for
   ``_uc_`` and ``_set_in_progress_code_slot``).
4. Delete ``tests/providers/zwave_js/test_uc_fallback.py`` and the
   ``uc_only_caps_response`` / ``uc_slot_walk`` / ``mock_uc_utils`` /
   ``uc_fallback_lock`` fixtures in that package's ``conftest.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import functools
import logging
from typing import Any, Literal

from zwave_js_server.const import CommandClass, SetValueStatus
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

from homeassistant.components.zwave_js.const import ATTR_EVENT
from homeassistant.core import Event, callback

from ..domain.credentials import (
    Credential,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    User,
)
from ..domain.exceptions import (
    CodeRejectedError,
    LockDisconnected,
    LockOperationFailed,
)
from ..domain.models import SlotCredential
from ._base import BaseLock

_LOGGER = logging.getLogger(__name__)

# SetValueResult statuses that mean a User Code CC value write was accepted.
_UC_SET_VALUE_OK = (
    SetValueStatus.SUCCESS,
    SetValueStatus.SUCCESS_UNSUPERVISED,
    SetValueStatus.WORKING,
)


@dataclass(repr=False, eq=False)
class ZWaveJSUserCodeFallbackSupport(BaseLock):
    """
    User Code CC fallback layer for the Z-Wave JS provider.

    Holds every UC-fallback-specific field, helper, and push handler so
    the whole workaround can be deleted as a unit (see the module
    docstring for the removal recipe). The concrete provider keeps only
    the routing branch points that call into this layer.
    """

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
        """Return the Z-Wave JS node; the concrete provider supplies this."""
        raise NotImplementedError

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

    def _uc_fallback_capabilities(self) -> LockCapabilities | None:
        """
        Detect the fallback and build slot-only capabilities for it.

        Called by ``async_get_capabilities`` after the unified API
        reported no usable PIN support. Only falls back when the node
        advertises User Code CC -- without it the legacy utilities
        cannot work either, and the lock genuinely has no PIN support
        LCM can manage -- and when the User Code CC value DB walk finds
        slots. Sets ``_uc_fallback`` accordingly and returns None when
        no fallback is possible.

        ``get_usercodes`` walks slot 1, 2, 3, ... in the value DB until
        ``NotFoundError``, so the returned list length is the lock's
        actual UC slot count. The function only raises ``NotFoundError``
        internally (caught there) and is otherwise pure value-DB
        walking, so we let any unexpected exception surface rather
        than silently mis-routing the lock to "no PIN support".
        """
        uc_slots = (
            get_usercodes(self.node) if self._node_supports_user_code_cc() else []
        )
        if not uc_slots:
            self._uc_fallback = False
            return None
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

    @staticmethod
    def _uc_slot_state(in_use: bool | None, usercode: str | None) -> SlotCredential:
        """
        Project a User Code CC slot to a ``SlotCredential``.

        Slots count as empty only when ``in_use`` is explicitly ``False``,
        or when ``in_use`` is unknown (``None``) with no cached value --
        the latter matches the legacy 3.x reader. Masked codes (all
        asterisks) and occupied slots without a cached value count as
        unreadable; an unknown ``in_use`` with a present value is treated
        as occupied so a partially populated cache cannot erase a live
        PIN (mirrors the push-path rule at ``_handle_uc_value_update``).
        """
        if in_use is False:
            return SlotCredential.empty()
        if not usercode:
            return (
                SlotCredential.empty()
                if in_use is None
                else SlotCredential.unreadable()
            )
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
        # Only an explicit in_use=False short-circuits: a missing/None
        # ATTR_IN_USE means the cache is partially populated and may hide
        # a live PIN, so the clear has to proceed.
        if current is not None and current.get(ATTR_IN_USE) is False:
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
    def _uc_handle_duplicate_notification(self, evt: Event, code_slot: int) -> bool:
        """
        Attribute a duplicate-code notification to the in-flight set, if any.

        Only fires when LCM initiated the set (the in-progress slot is
        only tracked by the UC-fallback write path; unified-mode writes
        report duplicates in-band). Marks the slot as rejected so the
        sync manager raises DuplicateCodeError on the next tick, routing
        through the standard CodeRejectedError flow. Some Z-Wave lock
        firmwares report this notification with userId=0 instead of the
        offending slot; treat 0 as referring to the slot we're currently
        setting. Returns True when the notification was consumed.
        """
        if (
            evt.data[ATTR_EVENT]
            == AccessControlNotificationEvent.NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
            and self._set_in_progress_code_slot is not None
            and code_slot in (0, self._set_in_progress_code_slot)
        ):
            slot = self._set_in_progress_code_slot
            self._set_in_progress_code_slot = None
            self.mark_code_rejected(slot)
            return True
        return False
