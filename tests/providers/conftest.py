"""Provider test fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from homeassistant.core import HomeAssistant


@pytest.fixture
async def mqtt_teardown(hass: HomeAssistant, mqtt_client_mock) -> AsyncGenerator[None]:
    """
    Cancel the MQTT client's misc periodic timer after the test.

    HA's MQTT client cancels that timer only on socket close, which the paho
    client mock never fires. Fire it here so teardown does not trip the
    lingering-timer check in verify_cleanup. Any test that sets up the real
    MQTT integration via ``mqtt_mock`` needs this.
    """
    yield
    mqtt_client_mock.on_socket_close(
        mqtt_client_mock, None, MagicMock(fileno=MagicMock(return_value=-1))
    )
    await hass.async_block_till_done()
