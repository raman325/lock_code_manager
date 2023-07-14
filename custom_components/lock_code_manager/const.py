"""Constants for lock_code_manager."""
from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "lock_code_manager"
VERSION = "v0.0.0"  # this will be automatically updated as part of the release workflow
ISSUE_URL = "https://github.com/raman325/lock_code_manager"
PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.TEXT]

ATTR_CODE_SLOT = "code_slot"
ATTR_USERCODE = "usercode"

# hass.data attributes
COORDINATORS = "coordinators"

# Events
EVENT_LOCK_STATE_CHANGED = f"{DOMAIN}_state_changed"

# Event data constants
ATTR_ACTION_TEXT = "action_text"
ATTR_CODE_SLOT_NAME = "code_slot_name"
ATTR_NOTIFICATION_SOURCE = "notification_source"

# Configuration Properties
CONF_LOCKS = "locks"
CONF_SLOTS = "slots"
CONF_NUM_SLOTS = "num_slots"
CONF_START_SLOT = "start_slot"

# Code slot properties
CONF_NUMBER_OF_USES = "number_of_uses"
CONF_CALENDAR = "calendar"

# Defaults
DEFAULT_NUM_SLOTS = 3
DEFAULT_START = 1
DEFAULT_HIDE_PINS = False
