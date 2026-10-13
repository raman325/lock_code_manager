"""
A stateful Schlage lock behind Home Assistant's ``schlage`` code services.

The fake registers ``schlage.get_codes``, ``schlage.add_code`` and
``schlage.delete_code`` and answers them the way Home Assistant core's
Schlage lock entity does, over a table of access codes that stands for the
cloud's. Every behavior cites the core line it follows (``SL`` =
homeassistant/components/schlage/lock.py at core ``519029a0ac0``):

- ``get_codes`` refreshes the codes from the cloud and answers
  ``{code_id: {"name", "code"}}`` (SL:165-177), wrapped per entity as an
  entity service answers.
- ``add_code`` refreshes, refuses a name already there (case- and
  space-insensitive) or a code already there (SL:74-104), then adds it
  (SL:117-134).
- ``delete_code`` refreshes and deletes the code whose name matches; a name
  not there is a success (SL:136-163).

The cloud itself is the part the fake models beyond core: for
``read_lag`` seconds after a write, a refresh answers what the cloud held
before it, as the reporter of #1083 saw (codes added, then read back
missing). The lag is time, not a count of reads, as a cloud catching up
is: the reads the coordinator makes right after a write would use up a
count before any sync tick looked. A transport
failure is the raw ``requests`` timeout core does not catch: only
``SchlageError`` is translated (SL:107-115), so a ``ReadTimeout`` (an
``OSError``) reaches the caller as is.

A delete that finds its code only in a lagging read succeeds without a
change. Core deletes that stale ``AccessCode`` through the cloud
(SL:136-163); what the cloud answers for a code already gone is not
documented by pyschlage, so the fake takes the quiet answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
import uuid

from requests.exceptions import ReadTimeout

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import translation

from custom_components.lock_code_manager.providers.schlage import SCHLAGE_DOMAIN

from ..service_fault import ServiceFault, first_fault


@dataclass
class FakeSchlageCloud:
    """The cloud's access codes for one lock, behind core's code services."""

    hass: HomeAssistant
    entity_id: str
    # code_id -> (name, code), as pyschlage's ``AccessCode`` carries them.
    codes: dict[str, tuple[str, str]] = field(default_factory=dict)
    # How many refreshes after a write still answer the table from before it.
    # Seconds after a write during which refreshes answer the table from
    # before it.
    read_lag: float = 0.0
    calls: list[tuple[str, dict]] = field(default_factory=list)
    faults: list[ServiceFault] = field(default_factory=list)
    # The table a lagging read answers, and the monotonic time it stops.
    _stale: dict[str, tuple[str, str]] | None = None
    _stale_until: float = 0.0

    async def async_install(self) -> None:
        """
        Register the three code services in place of the integration's.

        Also caches the integration's exception translations, as loading it
        does: an error raised with a translation key renders its English
        message only from that cache (homeassistant/helpers/translation.py:
        433-458), and the message is what reaches a caller.
        """
        # The cache answers only for integrations that are set up
        # (async_get_cached_translations reads hass.config.components), as
        # Schlage is wherever these services exist.
        self.hass.config.components.add(SCHLAGE_DOMAIN)
        await translation.async_get_translations(
            self.hass, "en", "exceptions", {SCHLAGE_DOMAIN}
        )
        for service, handler in (
            ("get_codes", self._async_get_codes),
            ("add_code", self._async_add_code),
            ("delete_code", self._async_delete_code),
        ):
            self.hass.services.async_register(
                SCHLAGE_DOMAIN,
                service,
                handler,
                supports_response=SupportsResponse.OPTIONAL,
            )

    def add(self, name: str, code: str) -> str:
        """Put a code on the lock, as its app or keypad would; return its id."""
        code_id = uuid.uuid4().hex
        self.codes[code_id] = (name, code)
        return code_id

    def time_out(self, service: str, *, times: int | None = 1) -> ServiceFault:
        """Make ``service`` raise the cloud's raw ``ReadTimeout`` (``times`` more)."""
        fault = ServiceFault(self.faults, service, times)
        self.faults.append(fault)
        return fault

    @property
    def lagging(self) -> bool:
        """Return whether reads still answer a table from before a write."""
        return self._stale is not None and time.monotonic() < self._stale_until

    def count(self, service: str) -> int:
        """Return how many times ``service`` was called."""
        return sum(name == service for name, _ in self.calls)

    # Services -------------------------------------------------------------

    def _enter(self, service: str, call: ServiceCall) -> None:
        self.calls.append((service, dict(call.data)))
        if first_fault(self.faults, service) is not None:
            raise ReadTimeout("Read timed out. (read timeout=60)")

    def _refresh(self) -> dict[str, tuple[str, str]]:
        """Answer the codes as the cloud returns them right now (SL:106-115)."""
        if self.lagging:
            return dict(self._stale)
        self._stale = None
        return dict(self.codes)

    def _wrote(self, before: dict[str, tuple[str, str]]) -> None:
        """
        Start or extend a lag after a write, for ``read_lag`` seconds.

        A write while reads still lag keeps the table they answer, from
        before the first write: the cloud has caught up with neither.
        """
        if not self.read_lag:
            return
        if not self.lagging:
            self._stale = before
        self._stale_until = time.monotonic() + self.read_lag

    async def _async_get_codes(self, call: ServiceCall) -> ServiceResponse:
        self._enter("get_codes", call)
        codes = self._refresh()
        return {
            self.entity_id: {
                code_id: {"name": name, "code": code}
                for code_id, (name, code) in codes.items()
            }
        }

    async def _async_add_code(self, call: ServiceCall) -> None:
        self._enter("add_code", call)
        name: str = call.data["name"]
        code: str = call.data["code"]
        codes = self._refresh()
        if any(_normalize(n) == _normalize(name) for n, _ in codes.values()):
            raise ServiceValidationError(
                translation_domain=SCHLAGE_DOMAIN,
                translation_key="schlage_name_exists",
                translation_placeholders={"name": name},
            )
        if any(c == code for _, c in codes.values()):
            raise ServiceValidationError(
                translation_domain=SCHLAGE_DOMAIN,
                translation_key="schlage_code_exists",
            )
        before = dict(self.codes)
        self.codes[uuid.uuid4().hex] = (name, code)
        self._wrote(before)

    async def _async_delete_code(self, call: ServiceCall) -> None:
        self._enter("delete_code", call)
        codes = self._refresh()
        target = next(
            (
                code_id
                for code_id, (name, _code) in codes.items()
                if _normalize(name) == _normalize(call.data["name"])
            ),
            None,
        )
        if target is None or target not in self.codes:
            return
        before = dict(self.codes)
        del self.codes[target]
        self._wrote(before)


def _normalize(name: str) -> str:
    """Normalize a code name the way core compares them (SL:74-77)."""
    return name.lower().strip()
