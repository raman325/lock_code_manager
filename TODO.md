Dev:
    - Track state updates to locks in event and sensor entities to update availability
    - Figure out how to handle builds and releases
    - Figure out how to add which keys and corresponding states caused the binary sensor state change in the logs
    - Instead of requiring the integration provider code to provide the device entry, leverage the entity and device registry
    - Reevaluate logging
Test:
    - Test enabling and disabling calendar/number of uses, adding and removing locks, etc.
    - Test allowing two different config entries to use the same lock if they don't use overlapping slots
    - Test invalid lock entity ID:
        1. Test when there are multiple locks results in the lock being removed and a persistent notification automatically being created.
        2. Test when there are no valid locks that a reauth config flow is started.
    - Test strategy
Docs:
    - Document how to use the strategy, including the additional custom card dependencies
    - Document strategy configuration options (include_code_slot_sensors)
