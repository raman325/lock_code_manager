"""Constants for lock_code_manager."""

from __future__ import annotations

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, Platform

DOMAIN = "lock_code_manager"
VERSION = "0.0.0"  # this will be automatically updated as part of the release workflow
PLATFORMS = (Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR)

FILES_URL_BASE = f"/{DOMAIN}_files"
STRATEGY_FILENAME = "generated/lock-code-manager.js"
STRATEGY_PATH = f"{FILES_URL_BASE}/{STRATEGY_FILENAME}"

SERVICE_HARD_REFRESH_USERCODES = "hard_refresh_usercodes"

ATTR_ENTITIES_ADDED_TRACKER = "entities_added_tracker"
ATTR_ENTITIES_REMOVED_TRACKER = "entities_removed_tracker"

ATTR_CODE_SLOT = "code_slot"
ATTR_USERCODE = "usercode"
ATTR_FROM = "from"
ATTR_TO = "to"
ATTR_LCM_CONFIG_ENTRY_ID = "lock_code_manager_config_entry_id"
ATTR_LOCK_CONFIG_ENTRY_ID = "lock_config_entry_id"
ATTR_EXTRA_DATA = "extra_data"
ATTR_MANAGED = "managed"
ATTR_SLOT = "slot"
ATTR_SLOT_NUM = "slot_num"
ATTR_CODE_LENGTH = "code_length"
ATTR_CONFIGURED_CODE = "configured_code"
ATTR_CONFIGURED_CODE_LENGTH = "configured_code_length"
ATTR_LOCK_ENTITY_ID = "lock_entity_id"
ATTR_LOCK_NAME = "lock_name"
ATTR_PIN_LENGTH = "pin_length"
ATTR_LAST_USED = "last_used"
ATTR_LAST_USED_LOCK = "last_used_lock"
ATTR_LAST_SYNCED = "last_synced"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_CONFIG_ENTRY_TITLE = "config_entry_title"
ATTR_EVENT_ENTITY_ID = "event_entity_id"
ATTR_CALENDAR_ENTITY_ID = "calendar_entity_id"
ATTR_CALENDAR = "calendar"
ATTR_CALENDAR_NEXT = "calendar_next"
ATTR_CALENDAR_ACTIVE = "active"
ATTR_CALENDAR_SUMMARY = "summary"
ATTR_CALENDAR_END_TIME = "end_time"
ATTR_CALENDAR_START_TIME = "start_time"
ATTR_CALENDAR_NEXT_START = "start_time"
ATTR_CALENDAR_NEXT_SUMMARY = "summary"

# Condition entity attributes
ATTR_CONDITION_ENTITY = "condition_entity"
ATTR_CONDITION_ENTITY_ID = "condition_entity_id"
ATTR_CONDITION_ENTITY_DOMAIN = "domain"
ATTR_CONDITION_ENTITY_STATE = "state"
ATTR_CONDITION_ENTITY_NAME = "friendly_name"

ATTR_SCHEDULE = "schedule"
ATTR_SCHEDULE_NEXT_EVENT = "next_event"

# Events
EVENT_LOCK_STATE_CHANGED = f"{DOMAIN}_lock_state_changed"

# Event data constants
ATTR_ACTION_TEXT = "action_text"
ATTR_CODE_SLOT_NAME = "code_slot_name"
ATTR_NOTIFICATION_SOURCE = "notification_source"

# Event entity event type
EVENT_PIN_USED = "pin_used"

# Configuration Properties
CONF_CONFIG_ENTRY = "config_entry"
CONF_CONDITIONS = "conditions"
CONF_ENTITIES = "entities"
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

# Supported domains for condition entities (CONF_ENTITY_ID option)
CONDITION_ENTITY_DOMAINS = [
    "calendar",
    "binary_sensor",
    "switch",
    "schedule",
    "input_boolean",
]

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
