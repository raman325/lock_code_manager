"""
Matter lock provider.

Handles PIN credential management via Matter lock helpers.
PINs are write-only: occupied slots report ``SlotCredential.unreadable()``,
cleared slots report ``SlotCredential.empty()``. Subscribes to DoorLock
cluster events via the push framework for code slot tracking (LockOperation)
and occupancy updates (LockUserChange).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from matter_server.common.models import EventType

from homeassistant.components.matter.helpers import (
    get_matter,
    get_node_from_device_entry,
)
from homeassistant.components.matter.lock_helpers import (
    SetCredentialFailedError,
    clear_lock_credential,
    clear_lock_user,
    get_lock_credential_status,
    get_lock_info,
    get_lock_users,
    set_lock_credential,
    set_lock_user,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

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
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
)
from ..domain.models import SlotCredential
from ._base import BaseLock
from ._util import parse_slot_num
from .const import LOGGER

# DoorLock cluster ID (0x0101 = 257)
_DOOR_LOCK_CLUSTER_ID = 257

# DoorLock cluster event IDs
_LOCK_OPERATION_EVENT_ID = 2
_LOCK_USER_CHANGE_EVENT_ID = 4

# LockUserChange LockDataType values
_LOCK_DATA_TYPE_PIN = 6

# LockUserChange DataOperationType values
_DATA_OP_ADD = 0
_DATA_OP_CLEAR = 1
_DATA_OP_MODIFY = 2


@dataclass(repr=False, eq=False)
class MatterLock(BaseLock):
    """Class to represent a Matter lock."""

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "matter"

    @property
    def supports_push(self) -> bool:
        """
        Return whether this lock supports push-based updates.

        Matter locks push occupancy changes via LockUserChange events.
        PINs are still write-only (values are never pushed), but slot
        occupancy (unreadable/empty credentials) is pushed in real time.
        """
        return True

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes."""
        return timedelta(minutes=5)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """
        Return interval between hard refreshes for drift detection.

        Matter locks support push events for local changes, but API-initiated
        changes bypass push notifications. Periodic hard refresh catches drift
        from external tools or missed events.
        """
        return timedelta(hours=1)

    @property
    def supports_native_users(self) -> bool:
        """
        Return True — Matter locks expose the full User/Credential model.

        Matter's DoorLock cluster manages users and credentials as distinct
        entities, so the base orchestration runs the user-first lifecycle
        (create/update user, then write its Personal Identification Number
        credential; delete the user when its last credential is removed).
        """
        return True

    def _get_matter_node(self) -> Any | None:
        """
        Get the MatterNode for this lock from the Matter integration.

        Uses the Matter integration's helper to resolve the node from the
        device entry, which correctly handles the device identifier format.
        Returns the node object with .node_id and access to the client.
        """
        if not self.device_entry:
            return None
        try:
            return get_node_from_device_entry(self.hass, self.device_entry)
        except Exception as err:
            LOGGER.debug(
                "Failed to resolve Matter node for %s: %s",
                self.lock.entity_id,
                err,
            )
            return None

    def _get_matter_client(self) -> Any | None:
        """Get the MatterClient via the Matter integration helper."""
        try:
            return get_matter(self.hass).matter_client
        except Exception as err:
            LOGGER.debug(
                "Failed to get Matter client for %s: %s",
                self.lock.entity_id,
                err,
            )
            return None

    def _require_client_and_node(self) -> tuple[Any, Any]:
        """Get client and node, raising LockDisconnected if unavailable."""
        client = self._get_matter_client()
        node = self._get_matter_node()
        if not client or not node:
            raise LockDisconnected(
                f"Matter client or node unavailable for {self.lock.entity_id}"
            )
        return client, node

    # -- Credential primitives -----------------------------------------------

    async def async_get_users(self) -> list[User]:
        """
        Read every user and their Personal Identification Number credentials from the lock.

        Matter PINs are write-only: each occupied credential slot is projected to
        SlotCredential.unreadable(). Non-PIN credentials (for example RFID) are
        filtered out because the coordinator and sync manager only manage PIN slots.
        """
        client, node = self._require_client_and_node()
        try:
            lock_data = await get_lock_users(client, node)
        except ServiceValidationError as err:
            raise LockOperationFailed(
                f"Matter get_lock_users rejected input for {self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter get_lock_users failed for {self.lock.entity_id}: {err}"
            ) from err

        # A for-loop (not a comprehension) so the int-or-None user_index and
        # credential index are narrowed by explicit guards before use: the
        # Matter helper types both as ``int | None``.
        users: list[User] = []
        for raw_user in lock_data.get("users", []):
            user_index = raw_user.get("user_index")
            if user_index is None:
                continue
            pin_credentials: list[Credential] = []
            for cred in raw_user.get("credentials", []):
                slot = cred.get("index")
                if cred.get("type") != "pin" or slot is None:
                    continue
                pin_credentials.append(
                    Credential(
                        type=CredentialType.PIN,
                        slot=slot,
                        state=SlotCredential.unreadable(),
                    )
                )
            users.append(
                User(
                    user_id=user_index,
                    name=raw_user.get("user_name"),
                    active=True,
                    credentials=pin_credentials,
                )
            )
        return users

    async def async_get_capabilities(self) -> LockCapabilities:
        """
        Read lock capabilities from the Matter DoorLock cluster.

        Maps the get_lock_info result to a platform-neutral LockCapabilities.
        Only the Personal Identification Number credential type is surfaced
        when the lock advertises PIN support. None capacity fields default
        to 0 (unknown capacity) rather than raising.
        """
        client, node = self._require_client_and_node()
        try:
            info = await get_lock_info(client, node)
        except ServiceValidationError as err:
            raise LockOperationFailed(
                f"Matter get_lock_info rejected input for {self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter get_lock_info failed for {self.lock.entity_id}: {err}"
            ) from err

        credential_types: dict[CredentialType, CredentialTypeCapability] = {}
        if "pin" in (info.get("supported_credential_types") or []):
            credential_types[CredentialType.PIN] = CredentialTypeCapability(
                num_slots=info.get("max_pin_users") or 0,
                min_length=info.get("min_pin_length") or 0,
                max_length=info.get("max_pin_length") or 0,
                supports_learn=False,
            )

        return LockCapabilities(
            supports_user_management=bool(info.get("supports_user_management")),
            max_users=info.get("max_users") or 0,
            credential_types=credential_types,
        )

    async def async_set_user(self, user: User) -> SetUserResult:
        """
        Create or update a lock user, returning whether it was newly created.

        The user.user_id here is the slot (credential_index). Matter allocates
        user_index independently: you cannot create a user at a chosen index —
        set_lock_user raises UserSlotEmptyError when user_index is given for an
        empty slot. So the flow is:

        1. Check the credential slot's current owner via get_lock_credential_status.
        2. If the slot is occupied (UPDATE): call set_lock_user with the existing
           user_index from the status response.
        3. If the slot is empty (CREATE): call set_lock_user with user_index=None
           so Matter auto-allocates a free user slot and returns the new index.

        The returned SetUserResult carries the real Matter user_index, which the
        base orchestration passes into async_set_credential as the user_id.
        """
        client, node = self._require_client_and_node()
        try:
            status = await get_lock_credential_status(
                client,
                node,
                credential_type="pin",
                credential_index=user.user_id,
            )
        except ServiceValidationError as err:
            raise LockOperationFailed(
                f"Matter get_lock_credential_status rejected input for "
                f"{self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter get_lock_credential_status failed for "
                f"{self.lock.entity_id}: {err}"
            ) from err

        credential_exists = status.get("credential_exists", False)
        existing_user_index = status.get("user_index")

        if credential_exists and existing_user_index is None:
            # The credential is reportedly occupied but the lock did not
            # surface its owner. Falling through to CREATE would orphan
            # the original user (it would still exist with zero credentials)
            # and break the user-per-credential invariant, so refuse the
            # write and let the caller decide how to recover.
            raise LockOperationFailed(
                f"Matter lock {self.lock.entity_id} slot {user.user_id} reports "
                "credential_exists=True but no user_index"
            )

        if credential_exists and existing_user_index is not None:
            # UPDATE: the slot is occupied — modify the existing user record.
            # set_lock_user here is a metadata-only name update. The historical
            # Matter contract (PR #1077) tolerated name-set failures so a
            # transient 500 or a name the lock rejects does not block the
            # subsequent credential write; the user still exists at the
            # known index, the only thing lost is the name update. Log a
            # warning and fall through.
            try:
                await set_lock_user(
                    client,
                    node,
                    user_index=existing_user_index,
                    user_name=user.name,
                )
            except HomeAssistantError as err:
                LOGGER.warning(
                    "Lock %s: failed to update user name on slot %s "
                    "(user_index=%s); continuing without name update: %s",
                    self.lock.entity_id,
                    user.user_id,
                    existing_user_index,
                    err,
                )
            return SetUserResult(user_id=existing_user_index, created=False)

        # CREATE: the slot is empty — let Matter auto-allocate a user_index.
        try:
            result = await set_lock_user(
                client,
                node,
                user_index=None,
                user_name=user.name,
            )
        except ServiceValidationError as err:
            raise LockOperationFailed(
                f"Matter set_lock_user rejected input for {self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter set_lock_user failed for {self.lock.entity_id}: {err}"
            ) from err
        return SetUserResult(user_id=result["user_index"], created=True)

    async def async_delete_user(self, user_id: int) -> None:
        """
        Delete a lock user and all of its credentials.

        The Matter DoorLock ClearUser command also clears all associated
        credentials and schedules for the user per the Matter specification.
        """
        client, node = self._require_client_and_node()
        try:
            await clear_lock_user(client, node, user_id)
        except ServiceValidationError as err:
            raise LockOperationFailed(
                f"Matter clear_lock_user rejected input for {self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter clear_lock_user failed for {self.lock.entity_id}: {err}"
            ) from err

    async def _send_set_credential(
        self,
        client: Any,
        node: Any,
        code_slot: int,
        pin: str,
        user_id: int,
    ) -> None:
        """
        Send set_lock_credential to the lock for the given slot, PIN, and user.

        Raises SetCredentialFailedError on lock rejection,
        CodeRejectedError on validation failure,
        LockDisconnected on communication failure.
        """
        try:
            await set_lock_credential(
                client,
                node,
                credential_type="pin",
                credential_data=pin,
                credential_index=code_slot,
                user_index=user_id,
            )
        except SetCredentialFailedError:
            raise
        except ServiceValidationError as err:
            # Bad Personal Identification Number / unsupported type -> the lock
            # rejects the value; surface as a code rejection.
            raise CodeRejectedError(
                code_slot=code_slot,
                lock_entity_id=self.lock.entity_id,
                reason=str(err),
            ) from err
        except HomeAssistantError as err:
            # Transport / endpoint failure -> route to the retry path.
            raise LockDisconnected(
                f"Matter set_lock_credential failed for {self.lock.entity_id}: {err}"
            ) from err

    async def async_set_credential(
        self,
        user_id: int,
        credential: Credential,
        *,
        name: str | None,
        source: Literal["sync", "direct"],
    ) -> bool:
        """
        Write a Personal Identification Number credential to the lock.

        Raises CodeRejectedError when the credential has no readable value
        (for example when projecting an already-unreadable slot — the lock
        requires a concrete PIN string). Handles the duplicate-slot restart
        case for sync sources by clearing and retrying once.

        The base orchestration skips coordinator refresh for push providers.
        Matter does not emit LockUserChange for LCM-initiated writes, so an
        optimistic push is required to keep the coordinator current.
        """
        if credential.readable_pin is None:
            raise CodeRejectedError(
                code_slot=credential.slot,
                lock_entity_id=self.lock.entity_id,
                reason="credential has no readable Personal Identification Number value",
            )

        client, node = self._require_client_and_node()
        pin = credential.readable_pin
        slot = credential.slot

        try:
            await self._send_set_credential(client, node, slot, pin, user_id)
        except SetCredentialFailedError as err:
            status = (err.translation_placeholders or {}).get("status", "")
            if status != "duplicate":
                raise CodeRejectedError(
                    code_slot=slot,
                    lock_entity_id=self.lock.entity_id,
                    reason=str(err),
                ) from err
            if source != "sync":
                raise DuplicateCodeError(
                    code_slot=slot,
                    lock_entity_id=self.lock.entity_id,
                ) from err

            # Sync duplicate: clear and retry once
            LOGGER.debug(
                "Lock %s: duplicate on slot %s, clearing and retrying",
                self.lock.entity_id,
                slot,
            )
            try:
                await clear_lock_credential(
                    client, node, credential_type="pin", credential_index=slot
                )
            except ServiceValidationError as clear_err:
                raise LockOperationFailed(
                    f"Matter clear_lock_credential rejected input for "
                    f"{self.lock.entity_id} during sync-duplicate retry: {clear_err}"
                ) from clear_err
            except HomeAssistantError as clear_err:
                raise LockDisconnected(
                    f"Matter clear_lock_credential failed for "
                    f"{self.lock.entity_id} during sync-duplicate retry: {clear_err}"
                ) from clear_err
            try:
                await self._send_set_credential(client, node, slot, pin, user_id)
            except SetCredentialFailedError as retry_err:
                retry_status = (retry_err.translation_placeholders or {}).get(
                    "status", ""
                )
                if retry_status == "duplicate":
                    raise DuplicateCodeError(
                        code_slot=slot,
                        lock_entity_id=self.lock.entity_id,
                    ) from retry_err
                raise CodeRejectedError(
                    code_slot=slot,
                    lock_entity_id=self.lock.entity_id,
                    reason=str(retry_err),
                ) from retry_err

        self._push_credential_update(slot, SlotCredential.unreadable())
        return True

    async def async_delete_credential(self, ref: CredentialRef) -> bool:
        """
        Clear a Personal Identification Number credential from the lock.

        Returns True on success. Pushes SlotCredential.empty() to the
        coordinator immediately because Matter does not emit LockUserChange
        for LCM-initiated clears.
        """
        client, node = self._require_client_and_node()
        try:
            await clear_lock_credential(
                client,
                node,
                credential_type="pin",
                credential_index=ref.slot,
            )
        except ServiceValidationError as err:
            raise LockOperationFailed(
                f"Matter clear_lock_credential rejected input for "
                f"{self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter clear_lock_credential failed for {self.lock.entity_id}: {err}"
            ) from err

        self._push_credential_update(ref.slot, SlotCredential.empty())
        return True

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Validate the lock supports Matter user management."""
        client, node = self._require_client_and_node()
        try:
            lock_info = await get_lock_info(client, node)
        except ServiceValidationError as err:
            raise LockCodeManagerProviderError(
                f"Matter get_lock_info rejected input for {self.lock.entity_id}: {err}"
            ) from err
        except HomeAssistantError as err:
            raise LockDisconnected(
                f"Matter get_lock_info failed for {self.lock.entity_id}: {err}"
            ) from err
        if not lock_info.get("supports_user_management"):
            raise LockCodeManagerProviderError(
                f"Matter lock {self.lock.entity_id} does not support user management"
            )
        if "pin" not in (lock_info.get("supported_credential_types") or []):
            raise LockCodeManagerProviderError(
                f"Matter lock {self.lock.entity_id} does not support PIN credentials"
            )
        LOGGER.debug(
            "Matter lock %s setup complete: %s",
            self.lock.entity_id,
            lock_info,
        )

    async def async_is_device_available(self) -> bool:
        """Return whether the Matter lock device is available for commands."""
        try:
            client, node = self._require_client_and_node()
            await get_lock_info(client, node)
        except (LockCodeManagerProviderError, HomeAssistantError) as err:
            LOGGER.debug(
                "Lock %s: availability check failed: %s",
                self.lock.entity_id,
                err,
            )
            return False
        return True

    # -- Event subscription via push framework --------------------------------

    @callback
    def setup_push_subscription(self) -> None:
        """
        Subscribe to Matter DoorLock cluster events.

        Handles two event types:
        - LockOperation (event 2): fires code slot events when a PIN is used
        - LockUserChange (event 4): pushes occupancy updates to coordinator
          when credentials are added, modified, or cleared

        Called by BaseLock.subscribe_push_updates(). On failure, the
        reconnect handlers will retry when the integration reloads.
        """
        if self._push_unsubs:
            return

        client = self._get_matter_client()
        node = self._get_matter_node()
        node_id = node.node_id if node else None
        if not client or node_id is None:
            raise LockDisconnected(
                f"Matter client or node ID unavailable for {self.lock.entity_id}"
            )

        self._register_push_unsub(
            client.subscribe_events(
                callback=self._on_node_event,
                event_filter=EventType.NODE_EVENT,
                node_filter=node_id,
            )
        )
        LOGGER.debug(
            "Lock %s: subscribed to Matter events (node %s)",
            self.lock.entity_id,
            node_id,
        )

    @callback
    def teardown_push_subscription(self) -> None:
        """Unsubscribe from Matter DoorLock cluster events."""
        self._clear_push_unsubs()

    @callback
    def _on_node_event(self, _event: Any, node_event: Any) -> None:
        """Dispatch DoorLock cluster events to the appropriate handler."""
        if getattr(node_event, "cluster_id", None) != _DOOR_LOCK_CLUSTER_ID:
            return

        event_id = getattr(node_event, "event_id", None)
        if event_id == _LOCK_OPERATION_EVENT_ID:
            self._handle_lock_operation(node_event)
        elif event_id == _LOCK_USER_CHANGE_EVENT_ID:
            self._handle_lock_user_change(node_event)
        else:
            LOGGER.debug(
                "Lock %s: unhandled DoorLock event_id=%s",
                self.lock.entity_id,
                event_id,
            )

    @callback
    def _handle_lock_operation(self, node_event: Any) -> None:
        """
        Handle LockOperation events (event ID 2).

        Fires a code slot event when a PIN credential is used to lock/unlock.
        Only PIN credentials (credentialType=1) trigger the event — other
        credential types (RFID, fingerprint, etc.) are ignored.
        """
        data: dict[str, Any] = getattr(node_event, "data", None) or {}
        credentials = data.get("credentials")
        lock_operation_type = data.get("lockOperationType")

        # Must have credentials with a PIN type to fire
        if not credentials:
            return

        # Find the PIN credential index (credentialType 1 = PIN)
        code_slot: int | None = None
        for cred in credentials:
            if isinstance(cred, dict) and cred.get("credentialType") == 1:
                code_slot = cred.get("credentialIndex")
                break

        if code_slot is None:
            return

        # lockOperationType: 0=Lock, 1=Unlock, 2=NonAccessUserEvent, 3=ForcedUserEvent, 4=Unlatch
        if lock_operation_type == 0:
            to_locked = True
        elif lock_operation_type == 1:
            to_locked = False
        else:
            to_locked = None

        LOGGER.debug(
            "Lock %s: LockOperation event — slot=%s, locked=%s",
            self.lock.entity_id,
            code_slot,
            to_locked,
        )

        self.async_fire_code_slot_event(
            code_slot=code_slot,
            to_locked=to_locked,
            action_text="locked"
            if to_locked
            else "unlocked"
            if to_locked is False
            else "operated",
            source_data=data,
        )

    @callback
    def _handle_lock_user_change(self, node_event: Any) -> None:
        """
        Handle LockUserChange events (event ID 4).

        Pushes occupancy updates to the coordinator when a PIN credential is
        added, modified, or cleared. This provides real-time change detection
        without waiting for the next poll cycle.

        Only PIN credentials (LockDataType=6) are handled. The DataIndex field
        maps to the credential index (code slot number).
        """
        data: dict[str, Any] = getattr(node_event, "data", None) or {}

        if data.get("lockDataType") != _LOCK_DATA_TYPE_PIN:
            return

        raw_index = data.get("dataIndex")
        if raw_index is None:
            return
        code_slot = parse_slot_num(raw_index)
        if code_slot is None:
            LOGGER.warning(
                "Lock %s: LockUserChange has non-integer dataIndex %r, ignoring",
                self.lock.entity_id,
                raw_index,
            )
            return

        operation = data.get("dataOperationType")

        if operation == _DATA_OP_CLEAR:
            resolved = SlotCredential.empty()
        elif operation in (_DATA_OP_ADD, _DATA_OP_MODIFY):
            resolved = SlotCredential.unreadable()
        else:
            LOGGER.debug(
                "Lock %s: LockUserChange event with unknown operation %s for slot %s",
                self.lock.entity_id,
                operation,
                code_slot,
            )
            return

        LOGGER.debug(
            "Lock %s: LockUserChange event — slot=%s, operation=%s, resolved=%s",
            self.lock.entity_id,
            code_slot,
            operation,
            resolved,
        )

        self._push_credential_update(code_slot, resolved)

    async def async_hard_refresh_codes(self) -> dict[int, SlotCredential]:
        """
        Perform a hard refresh and return all slot credentials.

        Matter has no on-device cache to invalidate. This re-fetches the current
        user list fresh from the lock and projects it through the base
        async_get_usercodes() projection (managed slots as empty, occupied
        Personal Identification Number slots as unreadable).
        """
        return await self.async_get_usercodes()
