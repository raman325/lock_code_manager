"""Helper functions for ZHA lock provider."""

from __future__ import annotations

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.core import HomeAssistant


def get_zha_gateway(hass: HomeAssistant):
    """Get the ZHA gateway proxy.

    Returns the gateway proxy from the ZHA integration's runtime data,
    or None if ZHA is not loaded or has no gateway.
    """
    if ZHA_DOMAIN not in hass.data:
        return None
    # ZHA stores gateway in runtime_data on the config entry
    for entry in hass.config_entries.async_entries(ZHA_DOMAIN):
        if hasattr(entry, "runtime_data") and entry.runtime_data:
            return getattr(entry.runtime_data, "gateway_proxy", None)
    return None
