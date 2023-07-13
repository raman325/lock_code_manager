# Lock Code Manager

Lock Code Manager is a Home Assistant integration that allows you to more easily manage your usercodes on your locks. Once you have configured it, the integration will set and clear codes on your locks as needed depending on how you decide to configure them.

Features:
- Synchronize multiple locks with a single set of codes
- Optionally use a calendar entity to activate and deactivate a code
- Optionally define a maximum number of uses for a code before the code is disabled

Locks from the following integrations are currently supported:
- Z-Wave JS

The code was written to make it (I think) easy to add support for locks in other integrations. Check the Wiki if you want to learn more about that and take a stab at it. Contributors welcome!

## Installation

The best way to install this integration is via HACS.

1. Add this repository as a custom integration repository in HACS
2. Go to Settings > Devices & Services > Add Integration
3. Select Lock Code Manager
4. Follow the prompts - additional information about the configuration options is available in the Wiki

## Inspiration

I spent some time contributing to [keymaster](https://github.com/FutureTense/keymaster), and what I learned working on it, and the regular complaints users had about it generating too many automations, entities, etc. led me to take a different approach. This isn't a knock on `keymaster`, unfortunately a lot of what is built in this integration wasn't possible for most of `keymaster`'s life. I briefly considered implementing this into `keymaster` but:
1. `keymaster` is still a great solution that works as is, and is more feature rich than this integration will likely ever be.
2. `keymaster` is surprisingly simple under the hood because it makes Home Assistant do a lot of the heavy lifting for figuring out when to enable and disable a usercode. This integration, on the otherhand, attempts to do all of the heavy lifting internally in code, which means it will generate less entities and automations but it is likely more fragile to changes in HA Core or even changes in the codebase.
3. It would be impossible to seamlessly migrate users from the current implementation of `keymaster` to this integration's implementation. Rewriting `keymaster` to do this would have been the equivalent of creating a new integration anyway, and since it's a separate integration, users have a choice of what implementation they want to use. Additionally, you can install the integrations side by side and slowly migrate your locks over in either direction as needed.

## Thanks

A big thank you to the other `keymaster` maintainers:
- @FutureTense
- @firstof9

As well as the person who created the base concept that `keymaster` evolved from: @ptdalen
