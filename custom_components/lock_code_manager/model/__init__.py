"""
Everything shared by providers and the HA layer.

Data types, rules, and read-only queries over config entries. It may use Home
Assistant (hass, the bus, the config entries) but never imports ``ha/`` or
``providers/``.
"""
