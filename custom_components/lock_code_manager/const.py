"""Constants for lock_code_manager."""

from __future__ import annotations

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, Platform

DOMAIN = "lock_code_manager"
VERSION = "0.0.0"  # this will be automatically updated as part of the release workflow
PLATFORMS = (Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR)

FILES_URL_BASE = f"/{DOMAIN}_files"
STRATEGY_FILENAME = "lock-code-manager-strategy.js"
STRATEGY_PATH = f"{FILES_URL_BASE}/{STRATEGY_FILENAME}"

SERVICE_HARD_REFRESH_USERCODES = "hard_refresh_usercodes"

ATTR_SETUP_TASKS = "setup_tasks"
ATTR_ENTITIES_ADDED_TRACKER = "entities_added_tracker"
ATTR_ENTITIES_REMOVED_TRACKER = "entities_removed_tracker"

ATTR_CODE_SLOT = "code_slot"
ATTR_USERCODE = "usercode"
ATTR_FROM = "from"
ATTR_TO = "to"
ATTR_LCM_CONFIG_ENTRY_ID = "lock_code_manager_config_entry_id"
ATTR_LOCK_CONFIG_ENTRY_ID = "lock_config_entry_id"
ATTR_EXTRA_DATA = "extra_data"

# hass.data attributes
COORDINATORS = "coordinators"

# Events
EVENT_LOCK_STATE_CHANGED = f"{DOMAIN}_lock_state_changed"

# Event data constants
ATTR_ACTION_TEXT = "action_text"
ATTR_CODE_SLOT_NAME = "code_slot_name"
ATTR_NOTIFICATION_SOURCE = "notification_source"

# Event entity event type
EVENT_PIN_USED = "pin_used"

# Configuration Properties
CONF_LOCKS = "locks"
CONF_SLOTS = "slots"
CONF_NUM_SLOTS = "num_slots"
CONF_START_SLOT = "start_slot"

# Additional entity keys
ATTR_ACTIVE = "active"
ATTR_CODE = "code"
ATTR_IN_SYNC = "in_sync"

# Code slot properties
CONF_NUMBER_OF_USES = "number_of_uses"
CONF_CALENDAR = "calendar"

# Defaults
DEFAULT_NUM_SLOTS = 3
DEFAULT_START = 1
DEFAULT_HIDE_PINS = False

PLATFORM_MAP = {
    CONF_CALENDAR: Platform.CALENDAR,
    CONF_ENABLED: Platform.SWITCH,
    CONF_NAME: Platform.TEXT,
    CONF_NUMBER_OF_USES: Platform.NUMBER,
    CONF_PIN: Platform.TEXT,
    EVENT_PIN_USED: Platform.EVENT,
}
