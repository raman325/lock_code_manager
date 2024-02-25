Dev:
- Track state updates to locks in event and sensor entities to update availability
- Figure out how to add which keys and corresponding states caused the binary sensor state change in the logs
- Reevaluate logging
- only create one BaseLock instance for each lock, even when used across multiple config entries - same with coordinators

Test:
- Test strategy

Docs:
- Document how to use the strategy, including the additional custom card dependencies
- Document strategy configuration options (include_code_slot_sensors)
