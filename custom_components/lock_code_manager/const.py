"""Constants for lock_code_manager."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, Platform

DOMAIN = "lock_code_manager"
VERSION = "0.0.0"  # this will be automatically updated as part of the release workflow
PLATFORMS = (Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR)

FILES_URL_BASE = f"/{DOMAIN}_files"
STRATEGY_FILENAME = "generated/lock-code-manager.js"
STRATEGY_PATH = f"{FILES_URL_BASE}/{STRATEGY_FILENAME}"

SERVICE_HARD_REFRESH_USERCODES = "hard_refresh_usercodes"
SERVICE_SET_USERCODE = "set_usercode"
SERVICE_CLEAR_USERCODE = "clear_usercode"
SERVICE_SET_SLOT_CONDITION = "set_slot_condition"
SERVICE_CLEAR_SLOT_CONDITION = "clear_slot_condition"
SERVICE_ADD_USER = "add_user"
SERVICE_DELETE_USER = "delete_user"
SERVICE_GENERATE_PIN = "generate_pin"
SERVICE_DEOBFUSCATE_LOG = "deobfuscate_log"
SERVICE_USE_CREDENTIAL = "use_credential"
SERVICE_SET_CREDENTIAL = "set_credential"
SERVICE_CLEAR_CREDENTIAL = "clear_credential"
SERVICE_ENABLE_USER = "enable_user"
SERVICE_DISABLE_USER = "disable_user"
SERVICE_SET_CONDITION = "set_condition"
SERVICE_CLEAR_CONDITION = "clear_condition"

ATTR_TEXT = "text"


ATTR_CODE_SLOT = "code_slot"
ATTR_CREDENTIAL_TYPE = "credential_type"
# Which property of the slot an entity represents ("pin", "name", "enabled").
# Published so a template can find an entity without matching on its ID, whose
# shape depends on the user's name and on the language it was created in.
ATTR_SLOT_FIELD = "slot_field"
ATTR_USERCODE = "usercode"
# The credential itself on set_credential. Not ``usercode``: that name is
# PIN-shaped, and this field carries whatever the credential type is.
ATTR_VALUE = "value"
# Opt-in on set_credential: turn the user on once the credential is set.
# Named for the effect rather than the state -- ``enabled: false`` would read
# as a request to disable somebody, which this never does. Enabling a user who
# already is costs nothing, so there is no guard behind the name: the write is
# a no-op when the value has not changed.
ATTR_ENABLE_IF_DISABLED = "enable_if_disabled"
ATTR_FROM = "from"
ATTR_TO = "to"
ATTR_LCM_CONFIG_ENTRY_ID = "lock_code_manager_config_entry_id"
ATTR_LOCK_CONFIG_ENTRY_ID = "lock_config_entry_id"
ATTR_EXTRA_DATA = "extra_data"
ATTR_MANAGED = "managed"
# Any entity of the user being addressed. Distinct from ``entity_id``,
# which on the condition commands means the condition entity itself.
ATTR_USER_ENTITY_ID = "user_entity_id"

# What a per-lock entity is called after the lock's name. Mirrors the
# ``entity`` names in strings.json, which the migration cannot read: it has to
# build the id the running integration would generate. test_frontend_contract
# holds the two together.
PER_LOCK_ENTITY_SUFFIX = {"code": "PIN", "in_sync": "in sync"}

# One repair for the whole entity-ID rename, however many entries moved.
ENTITY_IDS_RENAMED_ISSUE = "entity_ids_renamed"
# Where the migration accumulates those renames while entries migrate.
RENAMES_KEY = "migration_renames"

ATTR_CLEAR_CREDENTIALS = "clear_credentials"
ATTR_SLOT = "slot"
ATTR_SLOT_NUM = "slot_num"
# Marks the final event of a slot subscription: the user is gone, and the
# card must stop rendering them rather than fall back to a payload of nulls.
ATTR_REMOVED = "removed"
ATTR_CODE_LENGTH = "code_length"
ATTR_CONFIGURED_CODE = "configured_code"
ATTR_CONFIGURED_CODE_LENGTH = "configured_code_length"
ATTR_LOCK_ENTITY_ID = "lock_entity_id"
ATTR_LOCK_NAME = "lock_name"
ATTR_PIN_LENGTH = "pin_length"
ATTR_LENGTH = "length"
ATTR_LAST_USED = "last_used"
ATTR_LAST_USED_LOCK = "last_used_lock"
ATTR_LAST_SYNCED = "last_synced"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_CONFIG_ENTRY_TITLE = "config_entry_title"
ATTR_EVENT_ENTITY_ID = "event_entity_id"
ATTR_CALENDAR = "calendar"
ATTR_CALENDAR_NEXT = "calendar_next"
ATTR_CALENDAR_ACTIVE = "active"
ATTR_CALENDAR_SUMMARY = "summary"
ATTR_CALENDAR_END_TIME = "end_time"
ATTR_CALENDAR_START_TIME = "start_time"
ATTR_CALENDAR_NEXT_START = ATTR_CALENDAR_START_TIME  # Same key, used for next event
ATTR_CALENDAR_NEXT_SUMMARY = ATTR_CALENDAR_SUMMARY  # Same key, used for next event

# Condition entity attributes
ATTR_CONDITION_ENTITY = "condition_entity"
ATTR_CONDITION_ENTITY_ID = "condition_entity_id"
ATTR_CONDITION_ENTITY_DOMAIN = "domain"
ATTR_CONDITION_ENTITY_STATE = "state"
ATTR_CONDITION_ENTITY_NAME = "friendly_name"

ATTR_SCHEDULE = "schedule"
ATTR_SCHEDULE_NEXT_EVENT = "next_event"

# Unified credential-used event payload keys. ``source`` is the entity where
# the credential was entered and ``target`` the entity it was used against --
# any entity, of any domain, not necessarily a lock. Both are always present,
# and a caller with no natural entity for one of them is expected to make one.
ATTR_SOURCE = "source"
ATTR_TARGET = "target"
# What the device did with the credential -- see ``CredentialOperation``.
ATTR_OPERATION = "operation"

# Bus events. The ``BUS_EVENT_`` prefix is load-bearing: ``EVENT_CREDENTIAL_USED``
# further down is an entity key with a bare, undomained value, and firing that
# on the bus (or listening for this one as an entity key) would silently do
# nothing.
BUS_EVENT_CREDENTIAL_USED = f"{DOMAIN}_credential_used"

# The older, lock-shaped event. Retained for backward compatibility while
# consumers migrate to the unified event above: both fire, no removal version
# is set, and nothing warns at runtime. See the fire site in
# providers/_base.py.
EVENT_LOCK_STATE_CHANGED = f"{DOMAIN}_lock_state_changed"

# Credential validation response keys
ATTR_REASON = "reason"
ATTR_VALID = "valid"
ATTR_USER = "user"

REASON_UNKNOWN_CODE = "unknown_code"
REASON_USER_DISABLED = "user_disabled"
REASON_CONDITION_NOT_MET = "condition_not_met"

# Failure reasons ordered least to most restrictive. Only the last two are
# ever ranked against each other: a user held back by more than a condition
# outranks one merely waiting on its condition. An unknown code is returned
# outright, without consulting this, because there is no user to rank.
REASON_PRECEDENCE = (
    REASON_UNKNOWN_CODE,
    REASON_CONDITION_NOT_MET,
    REASON_USER_DISABLED,
)

# Event data constants
ATTR_ACTION_TEXT = "action_text"
ATTR_CODE_SLOT_NAME = "code_slot_name"
ATTR_NOTIFICATION_SOURCE = "notification_source"

# Event entity event type
# The entity key, and so the last part of its unique ID. Renamed from
# "pin_used" in version 4; the migration rewrites the stored ones. Not a bus
# event -- that is ``BUS_EVENT_CREDENTIAL_USED``.
EVENT_CREDENTIAL_USED = "credential_used"
LEGACY_EVENT_PIN_USED = "pin_used"

# Configuration Properties
CONF_CONFIG_ENTRY = "config_entry"
CONF_CONDITIONS = "conditions"
CONF_ENTITIES = "entities"
CONF_LOCKS = "locks"
CONF_SLOTS = "slots"
CONF_USERS = "users"
CONF_NUM_USERS = "num_users"

# Retired from the configuration in version 3, but still recognised so the
# migration can drop them from a version 2 entry.
CONF_NUM_SLOTS = "num_slots"
CONF_START_SLOT = "start_slot"

# Additional entity keys
ATTR_ACTIVE = "active"
ATTR_CODE = "code"
ATTR_IN_SYNC = "in_sync"
ATTR_SYNC_STATUS = "sync_status"

# Code slot properties
CONF_CALENDAR = "calendar"

# Supported domains for condition entities (CONF_CONDITION option)
CONDITION_ENTITY_DOMAINS = [
    "calendar",
    "binary_sensor",
    "switch",
    "schedule",
    "input_boolean",
]

# Platforms (integrations) excluded from being condition entities
# These create switch/binary_sensor entities but their states don't map to access control
EXCLUDED_CONDITION_PLATFORMS = frozenset({"scheduler"})

# Coordinator backoff
BACKOFF_FAILURE_THRESHOLD: int = 3
BACKOFF_INITIAL_SECONDS: int = 60
BACKOFF_MAX_SECONDS: int = 1800  # 30 minutes

# Poll failure alerting
POLL_FAILURE_ALERT_THRESHOLD: int = 12

# How long a write waits to be seen on the lock before the sync tick stops
# waiting and re-syncs. The tick asks for a confirming read while it waits,
# so this does not have to span a polled lock's scan interval -- and must
# not: three of these have to fit inside SYNC_ATTEMPT_WINDOW for a write the
# lock never keeps to trip the slot breaker.
PENDING_WRITE_TTL: float = 60.0

# Sync timing
TICK_INTERVAL = timedelta(seconds=2)
MAX_SYNC_ATTEMPTS = 3
SYNC_ATTEMPT_WINDOW = timedelta(minutes=5)


# Defaults
DEFAULT_NUM_USERS = 3

# How far a search for free slot numbers goes when no lock will say where its
# own range ends.
#
# 255 is the largest value a one-byte user identifier can carry, and the older
# credential command classes address users with one. Where the slot number is
# this integration's own tag rather than an index on the lock (Schlage,
# Akuvox, virtual) there is no device range at all and this stands in.
#
# Bounds only the SEARCH: a number a user already holds above it keeps
# working, and a lock reporting a larger range is believed up to it.
MAX_SEARCHED_SLOT = 255

PLATFORM_MAP = {
    CONF_CALENDAR: Platform.CALENDAR,
    CONF_ENABLED: Platform.SWITCH,
    CONF_NAME: Platform.TEXT,
    CONF_PIN: Platform.TEXT,
    EVENT_CREDENTIAL_USED: Platform.EVENT,
}
