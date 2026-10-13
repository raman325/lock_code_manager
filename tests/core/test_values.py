"""The core's values keep PINs out of every repr."""

from __future__ import annotations

from custom_components.lock_code_manager.core.values import (
    Holds,
    Strength,
    WantPin,
    Wrote,
    fingerprint,
    pin_at,
)


def test_no_pin_reaches_a_repr() -> None:
    """A value that ends up in a log never carries the PIN (#917)."""
    pin = "73915286"
    salt = b"salt"
    for value in (
        Holds(pin),
        WantPin(pin),
        Wrote(1, fingerprint(pin, salt), Strength.APPLIED, 1.0),
        pin_at(3),
    ):
        assert pin not in repr(value)


def test_fingerprints_depend_on_the_pin_and_the_salt() -> None:
    """Equal PINs match under one salt; a different PIN or salt does not."""
    assert fingerprint("1234", b"a") == fingerprint("1234", b"a")
    assert fingerprint("1234", b"a") != fingerprint("1235", b"a")
    assert fingerprint("1234", b"a") != fingerprint("1234", b"b")
