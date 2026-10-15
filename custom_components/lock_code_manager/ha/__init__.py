"""
The Home Assistant runtime that drives the sync engine and the providers.

Entry runtime data, the lock runtime and its adapter, slot coordinators,
services and repairs. It may import everything; nothing in ``sync/``,
``model/`` or ``providers/`` imports it.
"""
