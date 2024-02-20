"""Common constants for tests."""

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUMBER_OF_USES,
    CONF_SLOTS,
    DOMAIN,
)
from homeassistant.const import CONF_CODE, CONF_ENABLED, CONF_NAME

LOCK_DATA = f"mock_{DOMAIN}"

LOCK_1_ENTITY_ID = "lock.test_1"
LOCK_2_ENTITY_ID = "lock.test_2"

BASE_CONFIG = {
    CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
    CONF_SLOTS: {
        "1": {CONF_NAME: "test1", CONF_CODE: "1234", CONF_ENABLED: True},
        "2": {
            CONF_NAME: "test2",
            CONF_CODE: "5678",
            CONF_ENABLED: True,
            CONF_NUMBER_OF_USES: 5,
        },
    },
}
