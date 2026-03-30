# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Home Assistant Blueprints for Lock Code Manager

## Context

LCM currently has built-in features for number-of-uses tracking and condition entities. The user wants to externalize these as HA blueprints so LCM stays lean and users get more flexibility. The built-in `number_of_uses` entity will eventually be deprecated in favor of the blueprint.

## Architecture

Three blueprints. Template blueprints can only create ONE entity type (enforced by HA core), so cale...

### Prompt 2

create PR using PR template in draft state

