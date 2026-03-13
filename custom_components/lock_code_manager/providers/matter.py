"""Module for Matter locks."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.matter.const import DOMAIN as MATTER_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr

from ..const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from ..data import get_entry_data
from ..exceptions import LockDisconnected
from ._base import BaseLock

if TYPE_CHECKING:
    from matter_server.client import MatterClient
    from matter_server.common.models import MatterNodeData

_LOGGER = logging.getLogger(__name__)

# Door Lock cluster ID
CLUSTER_ID_DOOR_LOCK = 257  # 0x0101

# Credential types
CREDENTIAL_TYPE_PIN = 1

# Operation types for SetCredential
OPERATION_TYPE_ADD = 0
OPERATION_TYPE_CLEAR = 1
OPERATION_TYPE_MODIFY = 2


def _get_matter_client(hass) -> MatterClient | None:
    """Get the Matter client from the config entry."""
    if MATTER_DOMAIN not in hass.data:
        return None

    # Matter stores the adapter/client in runtime_data
    for entry in hass.config_entries.async_entries(MATTER_DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            # runtime_data is MatterEntryData which has .adapter.matter_client
            adapter = getattr(entry.runtime_data, "adapter", None)
            if adapter:
                return getattr(adapter, "matter_client", None)
    return None


def _get_node_for_device(hass, device_id: str) -> tuple[MatterNodeData | None, int]:
    """Get the Matter node and endpoint for a device."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(device_id)
    if not device:
        return None, 0

    # Find Matter identifier
    node_id = None
    for identifier in device.identifiers:
        if len(identifier) >= 2 and identifier[0] == MATTER_DOMAIN:
            # Matter identifier format: (matter, "node_id")
            try:
                node_id = int(identifier[1])
                break
            except (ValueError, TypeError):
                continue

    if node_id is None:
        return None, 0

    # Get the Matter client and find the node
    client = _get_matter_client(hass)
    if not client:
        return None, 0

    # Get node from client's node list
    nodes = client.get_nodes()
    for node in nodes:
        if node.node_id == node_id:
            # Find the Door Lock endpoint
            for endpoint_id, endpoint_info in node.endpoints.items():
                if endpoint_id == 0:  # Skip root endpoint
                    continue
                # Check if this endpoint has Door Lock cluster
                clusters = (
                    endpoint_info.get("clusters", {})
                    if isinstance(endpoint_info, dict)
                    else getattr(endpoint_info, "clusters", {})
                )
                if CLUSTER_ID_DOOR_LOCK in clusters:
                    return node, endpoint_id

    return None, 0


@dataclass(repr=False, eq=False)
class MatterLock(BaseLock):
    """Class to represent Matter lock."""

    lock_config_entry: ConfigEntry = field(repr=False)
    _node_id: int | None = field(init=False, default=None)
    _endpoint_id: int = field(init=False, default=1)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return MATTER_DOMAIN

    @property
    def usercode_scan_interval(self) -> timedelta:
        """Return scan interval for usercodes.

        Matter locks don't support push updates for user codes, so we poll.
        Use a longer interval to reduce traffic.
        """
        return timedelta(minutes=5)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return interval for hard refresh."""
        return timedelta(hours=1)

    @property
    def connection_check_interval(self) -> timedelta | None:
        """Return interval for connection checks."""
        return timedelta(seconds=30)

    def _get_node_info(self) -> tuple[Any | None, int]:
        """Get the Matter node and endpoint for this lock."""
        if self._node_id is not None:
            client = _get_matter_client(self.hass)
            if client:
                nodes = client.get_nodes()
                for node in nodes:
                    if node.node_id == self._node_id:
                        return node, self._endpoint_id
            return None, 0

        if not self.lock.device_id:
            return None, 0

        node, endpoint_id = _get_node_for_device(self.hass, self.lock.device_id)
        if node:
            self._node_id = node.node_id
            self._endpoint_id = endpoint_id
        return node, endpoint_id

    async def async_is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        client = _get_matter_client(self.hass)
        if not client:
            return False

        node, _endpoint_id = self._get_node_info()
        if not node:
            return False

        # Check if node is available
        return getattr(node, "available", True)

    async def _async_send_command(
        self, command_name: str, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Send a Door Lock cluster command."""
        client = _get_matter_client(self.hass)
        if not client:
            raise LockDisconnected("Matter client not available")

        node, endpoint_id = self._get_node_info()
        if not node:
            raise LockDisconnected("Could not find Matter node for lock")

        try:
            result = await client.send_device_command(
                node_id=node.node_id,
                endpoint_id=endpoint_id,
                cluster_id=CLUSTER_ID_DOOR_LOCK,
                command_name=command_name,
                payload=payload,
            )
            return result
        except Exception as err:
            _LOGGER.error(
                "Matter command %s failed for %s: %s",
                command_name,
                self.lock.entity_id,
                err,
            )
            raise LockDisconnected(f"Matter command failed: {err}") from err

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        if not await self.async_is_connection_up():
            raise LockDisconnected("Lock not connected")

        # Get configured code slots for this lock
        code_slots = {
            int(code_slot)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for code_slot in get_entry_data(entry, CONF_SLOTS, {})
            if self.lock.entity_id in get_entry_data(entry, CONF_LOCKS, [])
        }

        data: dict[int, int | str] = {}

        for slot_num in code_slots:
            try:
                # GetCredentialStatus command
                result = await self._async_send_command(
                    "GetCredentialStatus",
                    {
                        "credential": {
                            "credentialType": CREDENTIAL_TYPE_PIN,
                            "credentialIndex": slot_num,
                        }
                    },
                )

                _LOGGER.debug(
                    "Lock %s slot %s GetCredentialStatus result: %s",
                    self.lock.entity_id,
                    slot_num,
                    result,
                )

                if result and isinstance(result, dict):
                    # Check if credential exists
                    credential_exists = result.get("credentialExists", False)
                    if credential_exists:
                        # Matter doesn't return the actual PIN for security
                        # We can only know if a credential exists
                        # Return placeholder indicating slot is in use
                        data[slot_num] = "****"
                    else:
                        data[slot_num] = ""
                else:
                    data[slot_num] = ""

            except Exception as err:
                _LOGGER.debug(
                    "Failed to get credential status for %s slot %s: %s",
                    self.lock.entity_id,
                    slot_num,
                    err,
                )
                data[slot_num] = ""

        return data

    async def async_set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot."""
        if not await self.async_is_connection_up():
            raise LockDisconnected("Lock not connected")

        try:
            # Credential data must be base64 encoded
            credential_data = base64.b64encode(str(usercode).encode("utf-8")).decode(
                "ascii"
            )

            result = await self._async_send_command(
                "SetCredential",
                {
                    "operationType": OPERATION_TYPE_ADD,
                    "credential": {
                        "credentialType": CREDENTIAL_TYPE_PIN,
                        "credentialIndex": code_slot,
                    },
                    "credentialData": credential_data,
                    "userIndex": None,
                    "userStatus": None,
                    "userType": None,
                },
            )

            _LOGGER.debug(
                "Lock %s slot %s SetCredential result: %s",
                self.lock.entity_id,
                code_slot,
                result,
            )

            # Check for success status
            if result and isinstance(result, dict):
                status = result.get("status", 0)
                if status != 0:
                    _LOGGER.warning(
                        "SetCredential failed for %s slot %s: status %s",
                        self.lock.entity_id,
                        code_slot,
                        status,
                    )
                    raise LockDisconnected(f"SetCredential failed: status {status}")

            return True

        except LockDisconnected:
            raise
        except Exception as err:
            _LOGGER.error(
                "Failed to set PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to set PIN: {err}") from err

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot."""
        if not await self.async_is_connection_up():
            raise LockDisconnected("Lock not connected")

        try:
            result = await self._async_send_command(
                "ClearCredential",
                {
                    "credential": {
                        "credentialType": CREDENTIAL_TYPE_PIN,
                        "credentialIndex": code_slot,
                    }
                },
            )

            _LOGGER.debug(
                "Lock %s slot %s ClearCredential result: %s",
                self.lock.entity_id,
                code_slot,
                result,
            )

            return True

        except Exception as err:
            _LOGGER.error(
                "Failed to clear PIN for %s slot %s: %s",
                self.lock.entity_id,
                code_slot,
                err,
            )
            raise LockDisconnected(f"Failed to clear PIN: {err}") from err

    async def async_hard_refresh_codes(self) -> dict[int, int | str]:
        """Perform hard refresh and return all codes."""
        return await self.async_get_usercodes()
