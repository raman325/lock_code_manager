"""
Structural guard: every Matter SDK call must translate its exceptions.

The three Matter exception families (``HomeAssistantError`` /
``ServiceValidationError``, ``matter_server.common.errors.MatterError``, and
``matter_server.client.exceptions.MatterClientException``) do not subclass each
other, so a call site that catches only one lets the others escape to the sync
catchall, which suspends the slot (issue #1257). ``MatterLock._invoke_sdk`` is
the single choke point that catches all three. This test parses the provider
source and asserts every awaited SDK call either flows through
``self._invoke_sdk(...)`` or lives in a function on the bespoke allowlist that
translates exceptions itself, converting a silent omission into a reviewed
allowlist edit.
"""

from __future__ import annotations

import ast
from pathlib import Path

from custom_components.lock_code_manager.providers import matter as matter_module

# Matter lock-helper functions whose exceptions must be translated.
_SDK_FUNCTION_NAMES = frozenset(
    {
        "get_lock_users",
        "get_lock_info",
        "set_lock_user",
        "clear_lock_user",
        "set_lock_credential",
        "clear_lock_credential",
    }
)

# Functions that translate SDK exceptions with bespoke semantics and so are
# allowed to call the SDK directly instead of routing through _invoke_sdk:
# - _send_set_credential: SetCredentialFailedError passthrough + CodeRejectedError
# - _try_set_lock_user_with_fallbacks: MatterError is a charset-recovery
#   fall-through signal, not an immediate raise
# - async_set_credential: the sync-duplicate retry clears with a distinct
#   "during sync-duplicate retry" message
# Adding a name here must be a conscious, reviewed decision.
_ALLOWLIST = frozenset(
    {
        "_send_set_credential",
        "_try_set_lock_user_with_fallbacks",
        "async_set_credential",
    }
)


def _called_name(node: ast.Call) -> str | None:
    """Resolve the called function's bare name for Name or Attribute calls."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_invoke_sdk_call(node: ast.Call) -> bool:
    """Return True when ``node`` is a ``self._invoke_sdk(...)`` call."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "_invoke_sdk"
        and isinstance(func.value, ast.Name)
        and func.value.id == "self"
    )


def _enclosing_function_by_node(tree: ast.Module) -> dict[ast.AST, str]:
    """Map each descendant node to the name of its nearest enclosing function."""
    mapping: dict[ast.AST, str] = {}
    # ast.walk is breadth-first, so an outer function is visited before any
    # function nested in it. Stamping every descendant and always overwriting
    # therefore leaves each node mapped to its CLOSEST enclosing function: the
    # inner def's later pass re-stamps its own body with the inner name.
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(func):
                mapping[child] = func.name
    return mapping


def _invoke_sdk_second_positional_args(tree: ast.Module) -> set[int]:
    """Return ``id()`` of every node passed as _invoke_sdk's 2nd positional arg."""
    wrapped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_invoke_sdk_call(node):
            # _invoke_sdk(operation, coro): the coro is the 2nd positional arg.
            if len(node.args) >= 2:
                wrapped.add(id(node.args[1]))
    return wrapped


def test_every_sdk_call_translates_exceptions() -> None:
    """Assert each SDK call goes through _invoke_sdk or the allowlist.

    A wrapped call (``await self._invoke_sdk("op", get_lock_users(...))``) is not
    itself awaited -- only ``_invoke_sdk`` is -- so the SDK call is recognized by
    identity as _invoke_sdk's 2nd positional argument. A bespoke call is awaited
    directly inside an allowlisted function. Any other SDK call is an offender.
    """
    source = Path(matter_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    enclosing = _enclosing_function_by_node(tree)
    wrapped = _invoke_sdk_second_positional_args(tree)

    offenders: list[str] = []
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        name = _called_name(call)
        if name not in _SDK_FUNCTION_NAMES:
            continue
        if id(call) in wrapped:
            continue
        enclosing_func = enclosing.get(call, "<module>")
        if enclosing_func in _ALLOWLIST:
            continue
        offenders.append(f"{name} (in {enclosing_func}, line {call.lineno})")

    assert not offenders, (
        "Matter SDK calls bypass exception translation: "
        + ", ".join(offenders)
        + ". A new matter SDK call must go through `self._invoke_sdk(...)`; if "
        "the enclosing function translates exceptions itself, add it to "
        "ALLOWLIST in this test."
    )
