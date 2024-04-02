"""Backports for lock_code_manager."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import Event

_LOGGER = logging.getLogger(__name__)

EVENT_DATA_PASSED_IN = MAJOR_VERSION > 2024 or (
    MAJOR_VERSION == 2024 and MINOR_VERSION >= 4
)


def get_event_data_for_filter(evt: Event | dict[str, Any]) -> dict[str, Any]:
    """Get event data for filter."""
    return evt if EVENT_DATA_PASSED_IN else evt.data  # type: ignore[union-attr]
