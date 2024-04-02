"""Backports for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Event

from .const import EVENT_DATA_PASSED_IN

_LOGGER = logging.getLogger(__name__)


def get_event_data_for_filter(evt: Event | dict[str, Any]) -> dict[str, Any]:
    """Get event data for filter."""
    return evt if EVENT_DATA_PASSED_IN else evt.data  # type: ignore[union-attr]
