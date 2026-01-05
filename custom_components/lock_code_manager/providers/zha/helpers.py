"""Helper functions for ZHA lock provider."""

from __future__ import annotations

from homeassistant.components.zha.helpers import (
    get_zha_gateway_proxy as _get_zha_gateway_proxy,
)
from homeassistant.core import HomeAssistant


def get_zha_gateway(hass: HomeAssistant):
    """Get the ZHA gateway proxy.

    Returns the gateway proxy from the ZHA integration's runtime data,
    or None if ZHA is not loaded or has no gateway.
    """
    try:
        return _get_zha_gateway_proxy(hass)
    except (KeyError, ValueError):
        return None
