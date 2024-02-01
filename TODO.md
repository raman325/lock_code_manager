Dev:
    - Two options for UI:
        1. Add strategy for a lock management dashboard (https://developers.home-assistant.io/docs/frontend/custom-ui/custom-strategy?_highlight=strategy). References:
            - https://github.com/home-assistant/frontend/blob/dev/src/panels/lovelace/strategies/original-states-dashboard-strategy.ts#L7
            - https://discord.com/channels/330944238910963714/674164904298676225/1198739607756492930
            - https://github.com/hacs/integration/blob/main/custom_components/hacs/frontend.py#L56-L60
            - https://github.com/home-assistant/core/blob/dev/homeassistant/components/frontend/__init__.py#L277
        2. 'Generate Lovelace' service to generate static config using Jinja (https://jinja.palletsprojects.com/en/3.1.x/)
    - Reevaluate logging
    - Fix calendar checking logic since we rely on an external entity now
    - Figure out how the strategy can know about the calendar since we don't have access to the config entry data (add state attribute to binary sensor? add sensor that indicates what the calendar value is?)
    - Add calendar entities to strategy entity list so we can do things
    - Figure out how to expose strategy to HA instance, ideally add module automatically
Test:
    - Test enabling and disabling calendar/number of uses, adding and removing locks, etc.
    - Test allowing two different config entries to use the same lock if they don't use overlapping slots
    - Test invalid lock entity ID:
        1. Test when there are multiple locks results in the lock being removed and a persistent notification automatically being created.
        2. Test when there are no valid locks that a reauth config flow is started.
    - Test strategy
Docs:
    - Document how to use the strategy, including the additional custom card dependencies
