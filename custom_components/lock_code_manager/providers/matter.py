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
from ._util import parse_slot_num, parse_tag
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


def _lcm_slot_from_raw_users_by_user_index(
    raw_users: list[dict[str, Any]], user_index: int | None
) -> int | None:
    """
    Resolve the LCM slot by matching a raw lock user by ``user_index``.

    Pure helper over the already-fetched user list -- the caller owns the
    ``_raw_lock_users`` round-trip and the transport-error handling.
    """
    if user_index is None:
        return None
    name = next(
        (
            raw_user.get("user_name")
            for raw_user in raw_users
            if raw_user.get("user_index") == user_index
        ),
        None,
    )
    if not name:
        return None
    slot, _ = parse_tag(name)
    return slot


def _lcm_slot_from_raw_users_by_credential_index(
    raw_users: list[dict[str, Any]], credential_index: int | None
) -> int | None:
    """
    Resolve the LCM slot by finding the PIN credential at ``credential_index``.

    Walks each raw user's credentials list for a Personal Identification
    Number credential whose Matter index matches; if found, parses the
    owning user's ``lcm:<slot>:`` tag. Used as a fallback when the
    LockOperation event omits ``userIndex``.
    """
    if credential_index is None:
        return None
    name = next(
        (
            raw_user.get("user_name")
            for raw_user in raw_users
            for cred in raw_user.get("credentials", []) or []
            if cred.get("type") == "pin" and cred.get("index") == credential_index
        ),
        None,
    )
    if not name:
        return None
    slot, _ = parse_tag(name)
    return slot


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

    async def _raw_lock_users(self) -> list[dict[str, Any]]:
        """
        Return the raw user list from ``get_lock_users``.

        Internal helper for Matter-side lookups that need the unprojected
        Matter credential index (e.g. set/clear lock-credential calls).
        ``async_get_users`` consumes the same data but projects
        credentials to the LCM slot via the owning user's tag; raw
        callers want the lock's own identifiers.
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
        return lock_data.get("users", [])

    async def async_get_users(self) -> list[User]:
        """
        Read every user and their Personal Identification Number credentials from the lock.

        Matter PINs are write-only: each occupied credential slot is projected to
        SlotCredential.unreadable(). Non-PIN credentials (for example RFID) are
        filtered out because the coordinator and sync manager only manage PIN slots.

        Credential.slot is the LCM slot, NOT the Matter credential index.
        The LCM slot is recovered from the owning user's ``lcm:<slot>:`` tag;
        untagged users (legacy LCM 2.0) followed the
        ``credential_index == LCM slot`` invariant, so for them the Matter
        credential index doubles as the LCM slot. The translation keeps
        the rest of LCM (``_project_users_to_slots``, sync manager,
        coordinator) working in LCM-slot terms even though Matter now
        auto-allocates the lock-side credential index.
        """
        # A for-loop (not a comprehension) so the int-or-None user_index and
        # credential index are narrowed by explicit guards before use: the
        # Matter helper types both as ``int | None``.
        users: list[User] = []
        for raw_user in await self._raw_lock_users():
            user_index = raw_user.get("user_index")
            if user_index is None:
                continue
            user_name = raw_user.get("user_name")
            lcm_slot_from_tag: int | None = None
            if user_name:
                lcm_slot_from_tag, _ = parse_tag(user_name)
            pin_credentials: list[Credential] = []
            for cred in raw_user.get("credentials", []):
                credential_index = cred.get("index")
                if cred.get("type") != "pin" or credential_index is None:
                    continue
                slot = (
                    lcm_slot_from_tag
                    if lcm_slot_from_tag is not None
                    else credential_index
                )
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
                    name=user_name,
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
            # Matter DoorLock spec caps UserName at 32 bytes UTF-8;
            # ``matter.lock_helpers`` does not yet surface the attribute.
            max_user_name_length=32,
        )

    async def async_set_user(self, user: User) -> SetUserResult:
        """
        Find-or-create the lock user for the LCM slot encoded in ``user.name``.

        The base seam passes a tagged ``user.name`` (``lcm:<slot>:<display>``)
        whose slot is the LCM-side identity for this credential. The Matter
        lock's own ``user_index`` is whatever Matter happens to allocate;
        LCM does NOT pin it to the slot. Discovery on every call walks the
        lock's user list and matches by tag:

        1. Scan ``async_get_users()`` for a user whose name parses to the
           same LCM slot as ``user`` carries.
        2. If found (UPDATE): rename via ``set_lock_user`` with the
           existing ``user_index``, return that index.
        3. If not found (CREATE): allocate a fresh ``user_index`` via
           ``set_lock_user(user_index=None)`` and return the new index.

        ``user.user_id`` -- set by the seam from the LCM slot in
        ``user_from_slot`` -- is used as the slot identity when ``user.name``
        is untagged (defensive fallback; the seam should always pass a
        tagged name on this code path).
        """
        slot = self._slot_from_seam_user(user)
        client, node = self._require_client_and_node()

        existing_user_index = await self._find_user_index_for_slot(slot)

        if existing_user_index is not None:
            # UPDATE: rename via set_lock_user.
            #
            # set_lock_user here is a metadata-only name update. The
            # historical Matter contract (PR #1077) tolerated name-set
            # failures so a transient 500 or a name the lock rejects does
            # not block the subsequent credential write; the user still
            # exists at the known index, the only thing lost is the name
            # update. Log a warning and fall through.
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
                    slot,
                    existing_user_index,
                    err,
                )
            return SetUserResult(user_id=existing_user_index, created=False)

        # CREATE: no LCM-tagged user exists for this slot yet — let Matter
        # auto-allocate a free user_index.
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
        Return the ``user_index`` of the user LCM owns for ``slot``, if any.

        Two lookups, in priority order:

        1. **Canonical** -- a user whose name carries the
           ``lcm:<slot>:`` tag. This is the post-PR-B identity rule.
        2. **Legacy adoption** -- an *untagged* user owning a PIN
           credential at ``credential_index == slot``. Pre-PR-B Matter
           LCM pinned ``credential_index`` to the LCM slot, so an
           untagged user owning a PIN at that index is almost certainly
           the LCM 2.0 user for this slot. Adopting it (the subsequent
           ``set_lock_user`` rewrites the name to the tagged form, and
           ``set_lock_credential`` MODIFY'es the existing credential
           index in place) preserves a single, identifiable user per
           slot across the upgrade. Without this fallback the new model
           would CREATE a second user every time, silently leaving the
           pre-upgrade PIN active on the lock.

           The legacy pass MUST skip users whose names already parse to
           ANY LCM slot. Under the new model the Matter credential index
           is auto-allocated, so a user tagged ``lcm:<A>:`` can own a
           PIN at index ``B``; matching on ``cred.slot == slot`` alone
           would mis-adopt slot-A's user as slot-B's anchor.

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
                    if parse_tag(existing.name or "")[0] is None
                    for cred in existing.pin_credentials
                    if cred.slot == slot
                ),
                None,
            )

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

    async def async_release_managed_slot(self, slot: int) -> None:
        """
        Tear down the LCM-owned user that anchors ``slot``.

        Called by the base teardown path when the slot is removed from
        LCM config (see ``__init__.py``'s ``pairs_removed`` loop). Looks
        up the lock-side user via the same find-or-adopt logic the set
        path uses, then deletes it. Matter's ClearUser cascade then
        removes the user's PIN credential automatically.

        Tolerates "no user found": the slot may have never had an LCM
        user on this lock (e.g. the user was already removed out-of-band,
        or the slot was configured but never written). Lock-side
        transport failures bubble up so the base wraps them in a warning
        and the teardown still completes.
        """
        user_index = await self._find_user_index_for_slot(slot)
        if user_index is None:
            LOGGER.debug(
                "Lock %s: no LCM-owned user to release for slot %s",
                self.lock.entity_id,
                slot,
            )
            return
        await self.async_delete_user(user_index)

    async def _send_set_credential(
        self,
        client: Any,
        node: Any,
        code_slot: int,
        pin: str,
        user_id: int,
        credential_index: int | None,
    ) -> None:
        """
        Send set_lock_credential to the lock for the given user and PIN.

        ``credential_index=None`` auto-allocates the next free credential slot
        (CREATE). Passing an existing index addresses the user's current PIN
        credential for MODIFY. ``code_slot`` is the LCM slot only and is used
        for error reporting; it is no longer pinned to the Matter index.

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
                credential_index=credential_index,
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

    async def _find_pin_credential_index_for_user(self, user_id: int) -> int | None:
        """
        Return the user's current Matter PIN credential index, or ``None``.

        LCM no longer pins ``credential_index`` to the LCM slot; instead it
        treats Matter's credential index as opaque and rediscovers it per
        operation. This helper deliberately walks the **raw** lock-side
        user data (not ``async_get_users``) so the returned value is the
        Matter credential index Matter expects for
        ``set_lock_credential`` / ``clear_lock_credential`` -- not the
        LCM-projected slot that ``async_get_users`` exposes upward.
        """
        return next(
            (
                cred.get("index")
                for raw_user in await self._raw_lock_users()
                if raw_user.get("user_index") == user_id
                for cred in raw_user.get("credentials", [])
                if cred.get("type") == "pin" and cred.get("index") is not None
            ),
            None,
        )

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
        Write a Personal Identification Number credential to the lock.

        Looks up the user's existing PIN credential index. If present this
        is a MODIFY (same Matter credential index, new PIN value); if
        absent this is a CREATE (Matter auto-allocates the next free
        credential index). ``pin`` is the resolved PIN string the seam
        already validated as non-None. Handles the duplicate-slot restart
        case for sync sources by clearing the existing credential and
        retrying with a fresh allocation.

        The base orchestration skips coordinator refresh for push providers.
        Matter does not emit LockUserChange for LCM-initiated writes, so an
        optimistic push is required to keep the coordinator current.
        """
        client, node = self._require_client_and_node()
        slot = credential.slot
        existing_credential_index = await self._find_pin_credential_index_for_user(
            user_id
        )

        try:
            await self._send_set_credential(
                client, node, slot, pin, user_id, existing_credential_index
            )
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
            # Sync duplicate: only meaningful when WE own the credential.
            # If we don't, the duplicate is external -- surface to the
            # caller; clearing it would step on another controller's code.
            if existing_credential_index is None:
                raise DuplicateCodeError(
                    code_slot=slot,
                    lock_entity_id=self.lock.entity_id,
                ) from err

            LOGGER.debug(
                "Lock %s: duplicate on slot %s, clearing credential_index %s and retrying",
                self.lock.entity_id,
                slot,
                existing_credential_index,
            )
            try:
                await clear_lock_credential(
                    client,
                    node,
                    credential_type="pin",
                    credential_index=existing_credential_index,
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
                # Retry with credential_index=None so Matter auto-allocates a
                # fresh slot; we cleared the old one above.
                await self._send_set_credential(client, node, slot, pin, user_id, None)
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

        Looks up the user's current PIN credential index and clears that
        Matter credential. ``ref.slot`` is the LCM slot identifier; the
        Matter credential index is rediscovered per call (LCM does not
        pin the index to the LCM slot under the user-tag idempotency
        design). Returns True when the user had a PIN to clear and the
        clear succeeded; False when no PIN was present.

        Pushes SlotCredential.empty() to the coordinator immediately
        because Matter does not emit LockUserChange for LCM-initiated
        clears.
        """
        credential_index = await self._find_pin_credential_index_for_user(ref.user_id)
        if credential_index is None:
            return False

        client, node = self._require_client_and_node()
        try:
            await clear_lock_credential(
                client,
                node,
                credential_type="pin",
                credential_index=credential_index,
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
        """No matter-specific setup; base validates required capabilities."""

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
        Only PIN credentials (credentialType=1) trigger the event -- other
        credential types (RFID, fingerprint, etc.) are ignored.

        The event's ``credentials[].credentialIndex`` is the Matter credential
        index, which is no longer pinned to the LCM slot under the user-tag
        model. To find the LCM slot we resolve via the event's top-level
        ``userIndex`` -> user.name -> ``lcm:<slot>:`` tag, falling back to
        walking the user list for a PIN credential at ``credentialIndex``
        when ``userIndex`` is absent. The lookup is async so the callback
        schedules a task rather than blocking the event loop.
        """
        data: dict[str, Any] = getattr(node_event, "data", None) or {}
        credentials = data.get("credentials")

        if not credentials:
            return

        # Find the PIN credential index (credentialType 1 = PIN).
        credential_index = next(
            (
                cred.get("credentialIndex")
                for cred in credentials
                if isinstance(cred, dict) and cred.get("credentialType") == 1
            ),
            None,
        )
        if credential_index is None:
            return

        user_index = parse_slot_num(data.get("userIndex"))

        self.hass.async_create_task(
            self._dispatch_lock_operation(user_index, credential_index, data),
            f"Matter LockOperation dispatch for {self.lock.entity_id}",
        )

    async def _dispatch_lock_operation(
        self,
        user_index: int | None,
        credential_index: int,
        data: dict[str, Any],
    ) -> None:
        """
        Resolve the LCM slot for a LockOperation event and fire it.

        Primary path: lock-reported ``userIndex`` -> user.name -> tag.
        Fallback path: walk the user list to find the PIN credential at
        ``credentialIndex`` and parse the owning user's tag. Events whose
        owning user isn't LCM-tagged are silently dropped so out-of-band
        PIN uses on non-LCM credentials don't drive spurious code slot
        events.
        """
        try:
            raw_users = await self._raw_lock_users()
        except (LockDisconnected, LockOperationFailed) as err:
            LOGGER.debug(
                "Lock %s: could not resolve LockOperation userIndex=%s "
                "credentialIndex=%s: %s",
                self.lock.entity_id,
                user_index,
                credential_index,
                err,
            )
            return

        code_slot = _lcm_slot_from_raw_users_by_user_index(
            raw_users, user_index
        ) or _lcm_slot_from_raw_users_by_credential_index(raw_users, credential_index)
        if code_slot is None:
            LOGGER.debug(
                "Lock %s: LockOperation userIndex=%s credentialIndex=%s did not "
                "resolve to an LCM-tagged user; ignoring",
                self.lock.entity_id,
                user_index,
                credential_index,
            )
            return

        # lockOperationType: 0=Lock, 1=Unlock, 2=NonAccessUserEvent,
        # 3=ForcedUserEvent, 4=Unlatch
        lock_operation_type = data.get("lockOperationType")
        if lock_operation_type == 0:
            to_locked: bool | None = True
        elif lock_operation_type == 1:
            to_locked = False
        else:
            to_locked = None

        LOGGER.debug(
            "Lock %s: LockOperation event -- slot=%s, locked=%s",
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
        added, modified, or cleared. Real-time change detection without
        waiting for the next poll cycle.

        Only PIN credentials (LockDataType=6) are handled.

        The event's ``dataIndex`` is the Matter credential index, which is
        no longer pinned to the LCM slot (Matter auto-allocates). To find
        the LCM slot we use the event's ``userIndex`` to look up the
        owning user's name and parse its ``lcm:<slot>:`` tag. The lookup
        is async (a fresh ``_raw_lock_users`` round-trip) so the callback
        schedules a task rather than blocking the event loop. Events for
        users LCM doesn't own (untagged names) are ignored -- those
        credentials don't belong to any LCM-managed slot.
        """
        data: dict[str, Any] = getattr(node_event, "data", None) or {}

        if data.get("lockDataType") != _LOCK_DATA_TYPE_PIN:
            return

        raw_index = data.get("dataIndex")
        if raw_index is None:
            return
        credential_index = parse_slot_num(raw_index)
        if credential_index is None:
            LOGGER.warning(
                "Lock %s: LockUserChange has non-integer dataIndex %r, ignoring",
                self.lock.entity_id,
                raw_index,
            )
            return

        user_index = parse_slot_num(data.get("userIndex"))
        if user_index is None:
            LOGGER.debug(
                "Lock %s: LockUserChange has non-integer userIndex %r, ignoring",
                self.lock.entity_id,
                data.get("userIndex"),
            )
            return

        operation = data.get("dataOperationType")
        if operation == _DATA_OP_CLEAR:
            resolved = SlotCredential.empty()
        elif operation in (_DATA_OP_ADD, _DATA_OP_MODIFY):
            resolved = SlotCredential.unreadable()
        else:
            LOGGER.debug(
                "Lock %s: LockUserChange event with unknown operation %s "
                "(userIndex=%s, credentialIndex=%s)",
                self.lock.entity_id,
                operation,
                user_index,
                credential_index,
            )
            return

        self.hass.async_create_task(
            self._dispatch_lock_user_change(user_index, resolved, operation),
            f"Matter LockUserChange dispatch for {self.lock.entity_id}",
        )

    async def _dispatch_lock_user_change(
        self,
        user_index: int,
        resolved: SlotCredential,
        operation: int,
    ) -> None:
        """
        Resolve the LCM slot for a LockUserChange and push the update.

        Fetches the lock's current user list, finds the owner by
        ``user_index``, parses its ``lcm:<slot>:`` tag, and pushes the
        update. Events whose owning user isn't LCM-tagged are ignored so
        out-of-band credential changes on non-LCM users don't drive
        spurious coordinator updates.
        """
        try:
            raw_users = await self._raw_lock_users()
        except (LockDisconnected, LockOperationFailed) as err:
            LOGGER.debug(
                "Lock %s: could not resolve LockUserChange userIndex %s: %s",
                self.lock.entity_id,
                user_index,
                err,
            )
            return

        code_slot = _lcm_slot_from_raw_users_by_user_index(raw_users, user_index)
        if code_slot is None:
            LOGGER.debug(
                "Lock %s: LockUserChange userIndex %s did not resolve to an "
                "LCM-tagged user; ignoring",
                self.lock.entity_id,
                user_index,
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
