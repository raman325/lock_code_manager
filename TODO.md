Dev:
- Track state updates to locks in event and sensor entities to update availability
- Figure out how to add which keys and corresponding states caused the binary sensor state change in the logs
- Reevaluate logging

Test:
- Test strategy
- Test handling when a state is missing for binary sensor
- Test lock providers

Docs:
- Document how to use the strategy, including the additional custom card dependencies
- Document strategy configuration options (include_code_slot_sensors)
