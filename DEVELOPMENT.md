# Development

## Dev Container Setup

This project uses a VS Code Dev Container for development, which provides a
consistent development environment with all necessary dependencies
pre-installed. The container includes a debug-enabled Home Assistant instance
with Lock Code Manager integration for testing and development purposes.

When set up, the container automatically runs `scripts/setup-devcontainer` to
install dependencies into the container's system Python and creates a `.ha`
folder containing the Lock Code Manager component and required configurations.

### Getting Started

1. Clone this repository to your local machine
2. Open the project in Visual Studio Code
3. When prompted, click "Reopen in Container" or select
   `Dev Containers: Open in Container` from the Command Palette (F1)
4. Wait for the container to build and initialize (this may take a few minutes
   on first run)
5. Once ready, open the Run and Debug panel (Ctrl+Shift+D) and select
   "Debug Home Assistant"
6. Complete the initial Home Assistant setup wizard

### Integration Setup

1. Navigate to Home Assistant Settings > Devices & services
2. Add the Virtual Components integration:
   - Click "Add Integration"
   - Search for and select "Virtual Components"
   - Accept the default settings when prompted
3. Add the Lock Code Manager integration:
   - Click "Add Integration" again
   - Search for and select "Lock Code Manager"
   - Enter a configuration name
   - Select the "Test Lock" from the Virtual Components
   - Click "Next" to proceed to code slot configuration

### Example Configuration

Below is a basic example configuration for testing with two code slots:

```yaml
1:
  name: code_slot_1
  pin: "1234"
  enabled: true
2:
  name: code_slot_2
  pin: "4321"
  enabled: true
```

This configuration creates two enabled code slots with different PINs, which you can use to test the integration's functionality.
