Dev:
    - Add lock list as attribute to binary sensor and add cards to UI to list locks and slots
    - Add event entity to make it easy to see what happened last
    - add repair issue if calendar isn’t found - solution is to pick a new calendar
    - add repair issue if something else isn’t found (e.g. because someone renamed a lock_code_manager entity)
    - Reevaluate logging
    - Figure out how to handle builds and releases
    - Use HACS websocket commands to check whether the dependent components are installed, and if not, install them.
      - https://github.com/hacs/integration/blob/main/custom_components/hacs/websocket/repository.py#L19
      - https://github.com/hacs/integration/blob/main/custom_components/hacs/websocket/repository.py#L211
Test:
    - Test enabling and disabling calendar/number of uses, adding and removing locks, etc.
    - Test allowing two different config entries to use the same lock if they don't use overlapping slots
    - Test invalid lock entity ID:
        1. Test when there are multiple locks results in the lock being removed and a persistent notification automatically being created.
        2. Test when there are no valid locks that a reauth config flow is started.
    - Test strategy
Docs:
    - Document how to use the strategy, including the additional custom card dependencies
