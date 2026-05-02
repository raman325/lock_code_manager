"""Random PIN generation with unsafe-pattern filtering."""

from __future__ import annotations

import secrets

# Common 4-digit PINs that should never be generated.
# Source: composite of public PIN-frequency studies (Berry, DataGenetics).
COMMON_WEAK_PINS_4: frozenset[str] = frozenset(
    {
        "0000",
        "1004",
        "1010",
        "1111",
        "1122",
        "1212",
        "1234",
        "1313",
        "2000",
        "2001",
        "2222",
        "3333",
        "4321",
        "4444",
        "5555",
        "6666",
        "6969",
        "7777",
        "8888",
        "9999",
    }
)

MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 12
DEFAULT_PIN_LENGTH = 4


def is_unsafe_pin(pin: str) -> bool:
    """
    Return True if the PIN matches a known weak/unsafe pattern.

    Rejects all-same digits, fully sequential ascending or descending
    (with wrap at 9-to-0 / 0-to-9), repeating sub-sequence patterns
    (1212, 123123, etc.), and for 4-digit PINs the common-PIN list
    derived from public leak studies.
    """
    n = len(pin)
    if n == 0:
        return True

    # All same digits.
    if len(set(pin)) == 1:
        return True

    # Repeating sub-sequence: for some k that divides n, the first k digits
    # repeated n/k times equals the pin (k < n so we don't trivially match
    # the whole pin against itself).
    for k in range(1, n):
        if n % k == 0 and pin[:k] * (n // k) == pin:
            return True

    # Fully sequential ascending or descending, with wrap at 9-to-0 / 0-to-9.
    digits = [int(c) for c in pin]
    asc = all((digits[i] + 1) % 10 == digits[i + 1] for i in range(n - 1))
    desc = all((digits[i] - 1) % 10 == digits[i + 1] for i in range(n - 1))
    if asc or desc:
        return True

    # Common 4-digit weak PINs (statistically the most-guessed values).
    return n == 4 and pin in COMMON_WEAK_PINS_4


def generate_pin(length: int = DEFAULT_PIN_LENGTH) -> str:
    """
    Generate a cryptographically-random PIN of ``length`` digits.

    Retries until the candidate passes ``is_unsafe_pin``. Uses ``secrets``
    for entropy. A safety cap of 100 attempts protects against pathological
    edge cases (e.g. a future filter change rejecting a much larger fraction
    of the space).
    """
    if not MIN_PIN_LENGTH <= length <= MAX_PIN_LENGTH:
        raise ValueError(
            f"length must be between {MIN_PIN_LENGTH} and {MAX_PIN_LENGTH}"
        )
    for _ in range(100):
        candidate = "".join(str(secrets.randbelow(10)) for _ in range(length))
        if not is_unsafe_pin(candidate):
            return candidate
    raise RuntimeError(
        f"Failed to generate a safe PIN of length {length} after 100 attempts"
    )
