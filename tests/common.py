"""Common constants for tests."""

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN

from custom_components.lock_code_manager.const import (
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
)

LOCK_DATA = f"mock_{DOMAIN}"

LOCK_1_ENTITY_ID = "lock.test_1"
LOCK_2_ENTITY_ID = "lock.test_2"

BASE_CONFIG = {
    CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {
            CONF_NAME: "test2",
            CONF_PIN: "5678",
            CONF_ENABLED: True,
            CONF_NUMBER_OF_USES: 5,
            CONF_CALENDAR: "calendar.test",
        },
    },
}

ENABLED_ENTITY = "switch.mock_title_code_slot_2_enabled"
NUMBER_OF_USES_ENTITY = "number.mock_title_code_slot_2_number_of_uses"
PIN_SYNCED_ENTITY = "binary_sensor.mock_title_code_slot_2_pin_synced_to_locks"
EVENT_ENTITY = "event.mock_title_code_slot_2"
PIN_ENTITY = "text.mock_title_code_slot_2_pin"
NAME_ENTITY = "text.mock_title_code_slot_2_name"
