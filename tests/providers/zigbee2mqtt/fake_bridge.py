"""
A stateful Zigbee2MQTT bridge in front of one Zigbee lock.

The fake sits on the Home Assistant MQTT client mock: what the provider
publishes to the device's ``/set`` and ``/get`` topics reaches it, and what
Zigbee2MQTT would publish back on the device topic is fired into Home
Assistant as a received message. References, read at:

- zigbee2mqtt ``344b6e03`` (``Z2M``): lib/controller.ts, lib/state.ts,
  lib/util/objectAssignDeep.ts.
- zigbee-herdsman-converters ``3cdf1b86`` (``ZHC``):
  src/converters/fromZigbee.ts (``FZ``), src/converters/toZigbee.ts
  (``TZ``), src/lib/constants.ts (``CN``), src/devices/yale.ts (``YA``).

What it models:

- The lock's PIN table, one ``(userstatus, pincodevalue)`` per user id, in
  the Door Lock cluster's terms (``CN:340-344``: 0 available, 1 enabled,
  3 disabled). Any other status is published as ``not_supported_<status>``
  (``FZ:388-390``).
- ``/set`` with ``pin_code`` (``TZ:430-455``): no code clears the user
  (``clearPinCode``); a code sets it enabled, or disabled when
  ``user_enabled`` is false (``setPinCode``). A user outside
  ``0..pinCodeCount-1`` is refused before reaching the lock, and nothing is
  published on the device topic. Nothing is published for an accepted
  write either: the converter returns no state.
- ``/get`` with ``pin_code.user`` (``TZ:456-477``): the lock answers
  ``getPinCode``, and ``lock_pin_code_response`` (``FZ:380-400``) turns the
  answer into ``{"users": {"<id>": {"status": ...}}}``, with ``pin_code``
  only when ``expose_pin`` is on and the lock sent digits.
- Every publish on the device topic is Zigbee2MQTT's whole cached state
  (``cache_state`` defaults on; ``Z2M`` controller.ts:382-391), merged
  deeply into what was cached before (state.ts:115-126,
  objectAssignDeep.ts). So a users entry the lock has since changed is
  republished as it was last read, and a key the new answer leaves out
  (``pin_code``) keeps its old value. ``action`` and ``action_*`` ride along
  on the message that carries them and are never cached (state.ts:10-28).
- Operation and programming events (``FZ:263-314``), published as
  ``action``/``action_user``/``action_source(_name)``.

The default ``pin_code_count`` is the Yale lock extend's 250 (``YA:158-198``),
whose converters (``YA:160-167``) do not read a code back after a
programming event.
"""

from dataclasses import dataclass, field
import json
from typing import Any
from unittest.mock import DEFAULT, MagicMock

from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from homeassistant.core import HomeAssistant

# ZCL Door Lock user status (CN:340-344).
AVAILABLE = 0
ENABLED = 1
DISABLED = 3
# The cluster's "Not Supported": the lock cannot report its users.
NOT_SUPPORTED = 0xFF
_STATUS_NAMES = {AVAILABLE: "available", ENABLED: "enabled", DISABLED: "disabled"}

# lockSourceName (CN:290-296).
_SOURCE_NAMES = {0: "keypad", 1: "rf", 2: "manual", 3: "rfid", 4: "fingerprint"}

# The keys Zigbee2MQTT never caches (Z2M state.ts:10-28) that this fake sends.
_UNCACHED = ("action", "action_user", "action_source", "action_source_name")


@dataclass
class FakeZ2MBridge:
    """Zigbee2MQTT and the lock behind it, as seen over MQTT."""

    hass: HomeAssistant
    mqtt_mock: MagicMock
    device_topic: str
    pin_code_count: int = 250
    expose_pin: bool = True
    # Reads never answered (a bridge or lock that cannot read codes back);
    # writes still land.
    silent: bool = False
    # Reads answered, but every one with user status Not Supported and no
    # code (#1581); writes still land.
    cannot_tell: bool = False
    # Reads of an empty user answered available, of any other Not Supported:
    # a lock that reports which slots are free and not what the rest hold.
    cannot_tell_codes: bool = False
    # The lock reports an attribute after each write, so Zigbee2MQTT
    # republishes its cache, users entries as last read included (#1335).
    # The report goes out once the write's publish has returned, as a real
    # one arrives: after the provider's own view of the write.
    report_after_write: bool = False
    # The first read of a user after each write to it goes unanswered: a
    # flaky mesh losing the read-back (#1397 shape); later reads answer.
    drop_read_backs: bool = False
    # Writes reach the lock and it drops them, saying nothing: its table
    # stays as it was.
    drop_writes: bool = False
    # Seconds the lock takes to answer a read. While it does, it reports an
    # attribute, so Zigbee2MQTT republishes its cache ahead of the answer:
    # the asked-for user's entry as last read, before any write since.
    answer_after: float = 0.0
    # user id -> (userstatus, pincodevalue): the lock's own table.
    users: dict[int, tuple[int, str | None]] = field(default_factory=dict)
    # Zigbee2MQTT's cached state for the device. ``state`` matches the
    # ``state_locked`` the test discovery payload declares.
    cache: dict[str, Any] = field(default_factory=lambda: {"state": "LOCKED"})
    # What the provider sent: (topic suffix, body).
    requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Users written whose next read is lost, while ``drop_read_backs``.
    _lose_next_get: set[int] = field(default_factory=set)

    def install(self) -> None:
        """Answer the provider's publishes from now on."""
        self.mqtt_mock.async_publish.side_effect = self._on_publish

    def uninstall(self) -> None:
        """Stop answering."""
        self.mqtt_mock.async_publish.side_effect = None

    # The lock -------------------------------------------------------------

    def set_user(self, user: int, code: str | None, status: int = ENABLED) -> None:
        """Change a user on the lock itself, as its keypad would."""
        self.users[user] = (status, code) if code is not None else (AVAILABLE, None)

    def report(self, **attributes: Any) -> None:
        """Have the lock report attributes; Zigbee2MQTT republishes its cache."""
        self._publish_entity_state(attributes or {"battery": 90})

    def operation_event(self, action: str, user: Any, source: int = 0) -> None:
        """Send an operation event (``FZ:263-293``), keypad by default."""
        self._publish_event(action, user, source)

    def programming_event(self, action: str, user: int, source: int = 0) -> None:
        """Send a programming event (``FZ:294-314``), keypad by default."""
        self._publish_event(action, user, source)

    def count(self, suffix: str) -> int:
        """Return how many requests went to ``<device topic>/<suffix>``."""
        return sum(s == suffix for s, _ in self.requests)

    def sets_for(self, user: int) -> list[dict[str, Any]]:
        """Return the ``pin_code`` bodies sent to ``/set`` for ``user``."""
        return [
            body["pin_code"]
            for s, body in self.requests
            if s == "set" and body.get("pin_code", {}).get("user") == user
        ]

    # Zigbee2MQTT ----------------------------------------------------------

    def _on_publish(self, topic: str, payload: Any, *args: Any, **kwargs: Any) -> Any:
        """Take one publish from the provider; keep the real publish running."""
        prefix = f"{self.device_topic}/"
        if not topic.startswith(prefix):
            return DEFAULT
        suffix = topic.removeprefix(prefix)
        try:
            body = json.loads(payload)
        except json.JSONDecodeError, TypeError:
            return DEFAULT
        self.requests.append((suffix, body))
        request = body.get("pin_code") if isinstance(body, dict) else None
        if not isinstance(request, dict) or (self.silent and suffix == "get"):
            return DEFAULT
        user = request.get("user")
        if not isinstance(user, int) or not 0 <= user < self.pin_code_count:
            # "user must be in range for device" (TZ:440, 471): logged by
            # Zigbee2MQTT, nothing on the device topic.
            return DEFAULT
        if suffix == "set":
            self._set_pin_code(user, request)
            if self.drop_read_backs:
                self._lose_next_get.add(user)
        elif suffix == "get" and user in self._lose_next_get:
            self._lose_next_get.discard(user)
        elif suffix == "get":
            self._answer_get_pin_code(user)
        return DEFAULT

    def _set_pin_code(self, user: int, request: dict[str, Any]) -> None:
        """Apply ``setPinCode``/``clearPinCode`` (``TZ:442-454``)."""
        if self.drop_writes:
            return
        code = request.get("pin_code")
        if code is None:
            self.users[user] = (AVAILABLE, None)
        else:
            enabled = request.get("user_enabled", True)
            self.users[user] = (ENABLED if enabled else DISABLED, str(code))
        if self.report_after_write:
            self.hass.loop.call_soon(self.report)

    def _answer_get_pin_code(self, user: int) -> None:
        """Answer ``getPinCode`` through ``lock_pin_code_response`` (``FZ:380-400``)."""
        status, code = self.users.get(user, (AVAILABLE, None))
        if self.cannot_tell or (self.cannot_tell_codes and status != AVAILABLE):
            status, code = NOT_SUPPORTED, None
        entry: dict[str, Any] = {
            "status": _STATUS_NAMES.get(status, f"not_supported_{status}")
        }
        if self.expose_pin and code:
            entry["pin_code"] = code
        update = {"users": {str(user): entry}}
        if not self.answer_after:
            self._publish_entity_state(update)
            return
        self.report()
        self.hass.loop.call_later(self.answer_after, self._publish_entity_state, update)

    def _publish_event(self, action: str, user: Any, source: int) -> None:
        """Publish an event's ``action`` fields, as both event converters build them."""
        self._publish_entity_state(
            {
                "action": action,
                "action_user": user,
                "action_source": source,
                "action_source_name": _SOURCE_NAMES.get(source),
            }
        )

    def _publish_entity_state(self, update: dict[str, Any]) -> None:
        """Merge into the cache and publish the whole of it (``Z2M`` controller.ts:382-391)."""
        message = _assign_deep(_assign_deep({}, self.cache), update)
        self.cache = {k: v for k, v in message.items() if k not in _UNCACHED}
        async_fire_mqtt_message(self.hass, self.device_topic, json.dumps(message))


def _assign_deep(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """Merge ``source`` into ``target`` the way ``objectAssignDeep`` does."""
    for key, value in source.items():
        if isinstance(value, dict):
            existing = target.get(key)
            target[key] = _assign_deep(
                _assign_deep({}, existing) if isinstance(existing, dict) else {},
                value,
            )
        else:
            target[key] = value
    return target
