"""
Tests for rewriting slot-numbered registry identifiers to user names.

These seed a **pre-migration registry** deliberately. Every other test in
this suite starts with an empty registry, so entities register fresh under
the new identifiers and everything lines up -- which means a fully green
suite says almost nothing about the upgrade path. A real upgrade has rows
holding the old identifiers, and getting that wrong orphans every entity and
duplicates every device.
"""

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import ATTR_IN_SYNC, DOMAIN
from custom_components.lock_code_manager.domain.identifier_migration import (
    async_migrate_identifiers_to_names,
    async_rename_identifiers,
)

SLOTS = {
    1: {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1234"},
    2: {CONF_NAME: "Alice", CONF_ENABLED: True, CONF_PIN: "5678"},
}


@pytest.fixture(name="entry")
def entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry that is not set up, so only the registries matter."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="identifier-migration")
    entry.add_to_hass(hass)
    return entry


def _seed_entity(hass, entry, domain, unique_id) -> str:
    """Register an entity under a pre-migration unique ID; return its entity ID."""
    return (
        er.async_get(hass)
        .async_get_or_create(domain, DOMAIN, unique_id, config_entry=entry)
        .entity_id
    )


async def test_rewrite_preserves_entity_ids(hass: HomeAssistant, entry) -> None:
    """The whole point: unique IDs move, entity IDs do not.

    An entity ID is what automations, dashboards, and blueprints reference.
    Rewriting the unique ID in place keeps the same registry row, so the
    entity ID it already handed out stays valid.
    """
    ent_reg = er.async_get(hass)
    eid = entry.entry_id
    pin_entity = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|1|pin")
    in_sync_entity = _seed_entity(
        hass, entry, BINARY_SENSOR_DOMAIN, f"{eid}|2|in_sync|lock.front"
    )

    changed = async_migrate_identifiers_to_names(hass, eid, SLOTS)

    assert changed == 2
    # Entity IDs survive.
    assert ent_reg.async_get(pin_entity) is not None
    assert ent_reg.async_get(in_sync_entity) is not None
    # Unique IDs moved to the name, per-lock suffix intact.
    assert ent_reg.async_get(pin_entity).unique_id == f"{eid}|Raman|pin"
    assert (
        ent_reg.async_get(in_sync_entity).unique_id == f"{eid}|Alice|in_sync|lock.front"
    )
    # And nothing was left behind under the old identifier.
    assert ent_reg.async_get_entity_id(TEXT_DOMAIN, DOMAIN, f"{eid}|1|pin") is None


async def test_rewrite_moves_the_device_identifier(hass: HomeAssistant, entry) -> None:
    """The device is rewritten too, keeping its registry row and its area."""
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    device = dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|1")}, name="Code slot 1"
    )

    async_migrate_identifiers_to_names(hass, eid, SLOTS)

    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{eid}|1")}) is None
    moved = dev_reg.async_get_device(identifiers={(DOMAIN, f"{eid}|Raman")})
    assert moved is not None
    assert moved.id == device.id  # same row, so area and labels survive


async def test_rewrite_preserves_other_device_identifiers(
    hass: HomeAssistant, entry
) -> None:
    """Only the matching identifier moves; a device may carry others."""
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    other = ("some_other_domain", "keep-me")
    dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|1"), other}, name="x"
    )

    async_migrate_identifiers_to_names(hass, eid, SLOTS)

    moved = dev_reg.async_get_device(identifiers={(DOMAIN, f"{eid}|Raman")})
    assert other in moved.identifiers


async def test_rewrite_is_idempotent(hass: HomeAssistant, entry) -> None:
    """A second pass finds nothing to do rather than corrupting the first."""
    eid = entry.entry_id
    _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|1|pin")

    assert async_migrate_identifiers_to_names(hass, eid, SLOTS) == 1
    assert async_migrate_identifiers_to_names(hass, eid, SLOTS) == 0


async def test_rewrite_skips_foreign_and_malformed_identifiers(
    hass: HomeAssistant, entry
) -> None:
    """Anything not shaped like this entry's slot identifier is left alone."""
    ent_reg = er.async_get(hass)
    eid = entry.entry_id
    bare = _seed_entity(hass, entry, TEXT_DOMAIN, eid)
    other_entry = _seed_entity(hass, entry, TEXT_DOMAIN, "other|1|pin")
    unconfigured = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|99|pin")

    assert async_migrate_identifiers_to_names(hass, eid, SLOTS) == 0

    assert ent_reg.async_get(bare).unique_id == eid
    assert ent_reg.async_get(other_entry).unique_id == "other|1|pin"
    assert ent_reg.async_get(unconfigured).unique_id == f"{eid}|99|pin"


async def test_rewrite_leaves_entity_alone_on_collision(
    hass: HomeAssistant, entry, caplog
) -> None:
    """A taken target is logged and skipped, never resolved by deleting.

    Deleting the old row would take the user's automations with it, which is
    strictly worse than an entity that keeps working under its old ID.
    """
    ent_reg = er.async_get(hass)
    eid = entry.entry_id
    old = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|1|pin")
    _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Raman|pin")

    async_migrate_identifiers_to_names(hass, eid, SLOTS)

    assert ent_reg.async_get(old) is not None
    assert ent_reg.async_get(old).unique_id == f"{eid}|1|pin"
    assert "already in use" in caplog.text


async def test_rewrite_leaves_device_alone_on_collision(
    hass: HomeAssistant, entry, caplog
) -> None:
    """Same for a device whose target identifier already exists."""
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|1")}, name="old"
    )
    dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|Raman")}, name="new"
    )

    async_migrate_identifiers_to_names(hass, eid, SLOTS)

    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{eid}|1")}) is not None
    assert "already exists" in caplog.text


async def test_rewrite_without_names_is_a_noop(hass: HomeAssistant, entry) -> None:
    """Nothing to map means nothing is touched."""
    assert async_migrate_identifiers_to_names(hass, entry.entry_id, {}) == 0


async def test_rename_moves_entities_and_device(hass: HomeAssistant, entry) -> None:
    """Renaming a user moves their identifiers and keeps their entity IDs."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    pin_entity = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Raman|pin")
    per_lock = _seed_entity(
        hass, entry, BINARY_SENSOR_DOMAIN, f"{eid}|Raman|{ATTR_IN_SYNC}|lock.front"
    )
    untouched = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Alice|pin")
    dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|Raman")}, name="Raman"
    )

    async_rename_identifiers(hass, eid, "Raman", "Raman Smith")

    assert ent_reg.async_get(pin_entity).unique_id == f"{eid}|Raman Smith|pin"
    assert (
        ent_reg.async_get(per_lock).unique_id
        == f"{eid}|Raman Smith|{ATTR_IN_SYNC}|lock.front"
    )
    assert ent_reg.async_get(untouched).unique_id == f"{eid}|Alice|pin"
    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{eid}|Raman Smith")})


async def test_rename_does_not_match_a_name_prefix(hass: HomeAssistant, entry) -> None:
    """ "Ram" must not drag "Raman" along with it.

    Matching on the separator-terminated prefix is what prevents that; a bare
    startswith on the name alone would rewrite every user whose name begins
    with the renamed one.
    """
    ent_reg = er.async_get(hass)
    eid = entry.entry_id
    longer = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Raman|pin")
    exact = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Ram|pin")

    async_rename_identifiers(hass, eid, "Ram", "Bob")

    assert ent_reg.async_get(exact).unique_id == f"{eid}|Bob|pin"
    assert ent_reg.async_get(longer).unique_id == f"{eid}|Raman|pin"


async def test_rename_leaves_entity_alone_on_collision(
    hass: HomeAssistant, entry, caplog
) -> None:
    """A taken target during rename is skipped, not raised.

    Without this the loop would raise part-way, leaving some of the user's
    rows on the new name and the rest on the old -- a split the migration
    path explicitly guards against and this path used not to.
    """
    ent_reg = er.async_get(hass)
    eid = entry.entry_id
    old = _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Raman|pin")
    _seed_entity(hass, entry, TEXT_DOMAIN, f"{eid}|Bob|pin")

    async_rename_identifiers(hass, eid, "Raman", "Bob")

    assert ent_reg.async_get(old).unique_id == f"{eid}|Raman|pin"
    assert "already in use" in caplog.text


async def test_rename_leaves_device_alone_on_collision(
    hass: HomeAssistant, entry, caplog
) -> None:
    """Device renames are pre-checked, because HA does not check them.

    ``async_update_device`` just assigns new identifiers, so two devices
    would end up sharing one and every lookup after that would be wrong.
    """
    dev_reg = dr.async_get(hass)
    eid = entry.entry_id
    dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|Raman")}, name="Raman"
    )
    dev_reg.async_get_or_create(
        config_entry_id=eid, identifiers={(DOMAIN, f"{eid}|Bob")}, name="Bob"
    )

    async_rename_identifiers(hass, eid, "Raman", "Bob")

    assert dev_reg.async_get_device(identifiers={(DOMAIN, f"{eid}|Raman")}) is not None
    assert "already exists" in caplog.text
