"""Module for locks that keep no codes of their own."""

from __future__ import annotations

from dataclasses import dataclass

from .virtual import VirtualLock


@dataclass(repr=False, eq=False)
class CodelessLock(VirtualLock):
    """
    A real lock entity that has no credential storage, so this holds it.

    The lock is genuinely there -- an ESPHome lock, or any other integration
    with no code support -- and it is what gets locked and unlocked. What it
    has no notion of is a credential: nothing can be written to it and
    nothing can be read back. So Lock Code Manager keeps the credentials
    itself, in the same per-entity store a virtual lock uses, and a code
    entered at the device reaches this integration through the
    ``use_credential`` action rather than from the lock.

    Everything that makes that work is already ``VirtualLock``: the store,
    capabilities that advertise no limits of any kind, an integration that
    is always connected, and set/delete/get addressed by slot. The
    difference is what it is pointed at -- a real entity from any
    integration, rather than an entity the virtual integration made -- and
    that a user had to say so. Nothing here is inferred from a platform;
    see ``EntryConfig.is_codeless``.

    ``supports_code_slot_events`` stays False, inherited: a lock nothing can
    read codes from cannot report which one was used either. Uses are
    recorded from ``use_credential``, which needs no capability from the
    member it names.
    """

    @property
    def domain(self) -> str:
        """
        Return the name this provider answers to, not the member's platform.

        A fixed string rather than ``self.lock.platform``, for two reasons.
        It is what diagnostics reports per lock, and "codeless" is the fact
        somebody debugging needs -- that Lock Code Manager is holding these
        credentials, rather than the ESPHome (or whatever) integration the
        entity happens to come from, which the entity id already says.

        And the store key derives from it (``VirtualLock.async_setup``), so
        it decides where a member's credentials live. A member's own
        platform in that key would put two declared members of different
        integrations in differently named stores for no reason anybody can
        act on, and would move a store if a member were ever re-created
        elsewhere under the same entity id. Distinct from ``VirtualLock``'s
        "virtual" for the opposite reason: these two classes hold different
        things -- a virtual lock's codes belong to that integration, these
        belong to a declaration -- so neither may ever read the other's
        store for the same entity id.
        """
        return "codeless"
