"""Tests for centralized provider class resolution."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_CODELESS,
    CONF_LOCKS,
    CONF_MEMBERS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.allocation import (
    LockQuerySkipped,
    build_lock_instance,
)
from custom_components.lock_code_manager.domain.locks import async_create_lock_instance
from custom_components.lock_code_manager.providers import (
    Zigbee2MQTTLock,
    ZWaveJSLock,
    ZWaveJSUILock,
    resolve_provider_class,
)
from custom_components.lock_code_manager.providers.codeless import CodelessLock
from custom_components.lock_code_manager.providers.zwave_js_ui import (
    parse_zwave_js_ui_identifier,
)

from ..common import (
    LOCK_1_ENTITY_ID,
    async_discover_unclaimed_mqtt_lock,
    register_codeless_lock,
)


def test_single_provider_platform_ignores_device():
    """Non-mqtt platforms resolve from the map; device entry is irrelevant."""
    assert resolve_provider_class("zwave_js", None) is ZWaveJSLock


def test_unknown_platform_resolves_none():
    """An unrecognized platform resolves to None."""
    assert resolve_provider_class("not_a_platform", None) is None


async def test_mqtt_dispatches_on_identifier(hass: HomeAssistant) -> None:
    """mqtt resolves per-device by identifier prefix; unclaimed devices resolve None."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)

    z2m_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zigbee2mqtt_0xc0ffee")},
        name="Z2MLock",
    )
    zui_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zwavejs2mqtt_0xd4ee5a7a_node20")},
        name="ZUILock",
    )
    unclaimed_device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "somebridge_1")},
        name="UnclaimedLock",
    )

    assert resolve_provider_class("mqtt", z2m_device) is Zigbee2MQTTLock
    assert resolve_provider_class("mqtt", zui_device) is ZWaveJSUILock
    assert resolve_provider_class("mqtt", unclaimed_device) is None
    assert resolve_provider_class("mqtt", None) is None


async def test_mqtt_dispatches_a_custom_zwave_js_ui_prefix(hass: HomeAssistant) -> None:
    """
    A gateway with a renamed UID_DISCOVERY_PREFIX still dispatches to its provider.

    The prefix is an environment variable on the gateway and only defaults to
    ``zwavejs2mqtt_``. An identifier that does not resolve here resolves to no
    provider at all, and the config flow refuses the lock outright.
    """
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "myzwave_0xd4ee5a7a_node20")},
        name="RenamedGatewayLock",
    )

    assert resolve_provider_class("mqtt", device) is ZWaveJSUILock


async def test_mqtt_dispatch_prefers_zigbee2mqtt_over_the_zui_tail(
    hass: HomeAssistant,
) -> None:
    """
    Zigbee2MQTT is checked first, and the order became load-bearing.

    Its prefix is fixed while zwave-js-ui is now recognized by its tail alone,
    so a Zigbee2MQTT address ending in a zwave-js-ui-shaped tail matches both
    rules; only the ordering keeps it with the provider that speaks Zigbee.
    """
    identifier = "zigbee2mqtt_0xd4ee5a7a_node20"
    assert parse_zwave_js_ui_identifier(identifier) is not None

    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", identifier)},
        name="AmbiguousLock",
    )

    assert resolve_provider_class("mqtt", device) is Zigbee2MQTTLock


async def test_precedence_holds_across_separate_identifiers(
    hass: HomeAssistant,
) -> None:
    """
    The order is over the device, not over one identifier at a time.

    A device can carry both shapes in separate identifiers -- a stale
    registry row left by a re-paired device, or a bridge that publishes more
    than one address. ``identifiers`` is a SET, so testing both rules against
    each entry in turn resolved to whichever the hash happened to yield
    first, and the same device could dispatch differently across restarts.
    Two passes over the whole set is what makes the documented precedence
    mean anything.
    """
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={
            ("mqtt", "zwavejs2mqtt_0xd4ee5a7a_node20"),
            ("mqtt", "zigbee2mqtt_0x00124b0021fb1234"),
        },
        name="BothShapesLock",
    )

    assert resolve_provider_class("mqtt", device) is Zigbee2MQTTLock


async def test_mqtt_dispatch_skips_malformed_identifier(hass: HomeAssistant) -> None:
    """A malformed short identifier tuple is skipped, not treated as a crash."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt",), ("mqtt", "zwavejs2mqtt_0xd4ee5a7a_node20")},
        name="MalformedIdentifierLock",
    )
    # Identifiers are a set, so the well-formed one may be visited first and
    # never exercise the guard. A device carrying only a malformed tuple has
    # to walk through it -- and that tuple must not collide with the first
    # device's ("mqtt",), or the registry merges the two devices and the
    # well-formed identifier leaks into this one.
    malformed_only = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt_bridge",)},
        name="OnlyMalformedIdentifierLock",
    )

    assert resolve_provider_class("mqtt", device) is ZWaveJSUILock
    assert resolve_provider_class("mqtt", malformed_only) is None


async def test_factory_rejects_unclaimed_mqtt_lock(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """The lock factory refuses an mqtt lock no provider claims."""
    lock_entry = await async_discover_unclaimed_mqtt_lock(hass)
    lcm_entry = MockConfigEntry(domain=DOMAIN, unique_id="unclaimed-mqtt")
    lcm_entry.add_to_hass(hass)

    with pytest.raises(
        HomeAssistantError, match="No Lock Code Manager provider claims"
    ):
        async_create_lock_instance(
            hass,
            dr.async_get(hass),
            er.async_get(hass),
            lcm_entry,
            lock_entry.entity_id,
        )


async def test_allocation_skips_unclaimed_mqtt_lock(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """Allocation treats an unclaimed mqtt lock as one it will never write to."""
    lock_entry = await async_discover_unclaimed_mqtt_lock(hass)

    with pytest.raises(LockQuerySkipped) as raised:
        build_lock_instance(
            hass, dr.async_get(hass), er.async_get(hass), None, lock_entry.entity_id
        )

    assert raised.value.managed is False


def _entry_declaring(
    hass: HomeAssistant, lock_entry: er.RegistryEntry, **declared: bool
) -> MockConfigEntry:
    """Return an entry that manages a lock and declares what it was told."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"declaring-{lock_entry.entity_id}",
        data={
            CONF_LOCKS: [lock_entry.entity_id],
            CONF_MEMBERS: {lock_entry.id: declared},
        },
    )
    lcm_entry.add_to_hass(hass)
    return lcm_entry


async def test_a_declared_member_resolves_to_the_codeless_provider(
    hass: HomeAssistant,
) -> None:
    """A lock nothing claims is buildable once its entry declares it."""
    lock_entry = register_codeless_lock(hass)
    lcm_entry = _entry_declaring(hass, lock_entry, **{CONF_CODELESS: True})

    lock = async_create_lock_instance(
        hass, dr.async_get(hass), er.async_get(hass), lcm_entry, lock_entry.entity_id
    )

    assert isinstance(lock, CodelessLock)
    assert lock.lock.entity_id == lock_entry.entity_id


async def test_allocation_reads_a_declared_member(hass: HomeAssistant) -> None:
    """
    Allocation builds the same provider the entry's own setup will.

    Skipping it instead would issue slot numbers against a member Lock Code
    Manager is about to start writing credentials to.
    """
    lock_entry = register_codeless_lock(hass)
    lcm_entry = _entry_declaring(hass, lock_entry, **{CONF_CODELESS: True})

    lock = build_lock_instance(
        hass, dr.async_get(hass), er.async_get(hass), lcm_entry, lock_entry.entity_id
    )

    assert isinstance(lock, CodelessLock)


async def test_an_undeclared_lock_nothing_claims_is_still_refused(
    hass: HomeAssistant,
) -> None:
    """
    Never guessing is the rule the declaration is an exception to.

    Only somebody answering makes a lock codeless; a lock somebody meant to
    reach through a provider Lock Code Manager has not been taught yet has
    to keep failing loudly.
    """
    lock_entry = register_codeless_lock(hass)
    lcm_entry = _entry_declaring(hass, lock_entry)

    with pytest.raises(
        HomeAssistantError, match="No Lock Code Manager provider claims"
    ):
        async_create_lock_instance(
            hass,
            dr.async_get(hass),
            er.async_get(hass),
            lcm_entry,
            lock_entry.entity_id,
        )

    with pytest.raises(LockQuerySkipped) as raised:
        build_lock_instance(
            hass,
            dr.async_get(hass),
            er.async_get(hass),
            lcm_entry,
            lock_entry.entity_id,
        )

    assert raised.value.managed is False


async def test_a_declaration_outranks_platform_dispatch(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    The declaration is read first, and the order is the point.

    Should Lock Code Manager ever gain a provider for a platform somebody
    already declared codeless, letting that provider win would move a
    member's credentials out of this integration's store and onto a device
    the user never agreed to write to, silently, on a version upgrade.
    """
    lock_entry = er.async_get(hass).async_get(LOCK_1_ENTITY_ID)
    assert lock_entry is not None
    # Claimed by platform dispatch on its own.
    assert resolve_provider_class(lock_entry.platform, None) is not None

    lcm_entry = _entry_declaring(hass, lock_entry, **{CONF_CODELESS: True})

    lock = async_create_lock_instance(
        hass, dr.async_get(hass), er.async_get(hass), lcm_entry, LOCK_1_ENTITY_ID
    )

    assert isinstance(lock, CodelessLock)
