"""A fault for the service-backed lock fakes (Schlage, Akuvox)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Self


@dataclass(eq=False)
class ServiceFault:
    """
    One way a faked service misbehaves, until cleared.

    Shaped like ``tests.common.LockFault`` and the node fakes' ``NodeFault``
    where a service call allows it: ``times`` limits how many calls it
    meets before it removes itself (None: until cleared), ``hits`` counts
    the calls it met, and it is a context manager that clears it on exit.
    A service call cannot be held, so there is no ``held``.
    """

    faults: list[ServiceFault]
    service: str
    times: int | None = None
    hits: int = 0

    def __enter__(self) -> Self:
        """Return the fault, to clear on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Clear the fault."""
        self.clear()

    def clear(self) -> None:
        """Stop meeting calls."""
        if self in self.faults:
            self.faults.remove(self)

    def meet(self, service: str) -> bool:
        """Return whether a call of ``service`` meets this fault, counting it."""
        if service != self.service:
            return False
        self.hits += 1
        if self.times is not None and self.hits >= self.times:
            self.clear()
        return True


def first_fault(faults: list[ServiceFault], service: str) -> ServiceFault | None:
    """Return the first fault a call of ``service`` meets, if any."""
    return next((fault for fault in list(faults) if fault.meet(service)), None)
