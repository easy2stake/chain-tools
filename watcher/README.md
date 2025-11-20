# EVM Chain Watcher

A modular Python script to monitor EVM-compatible chain endpoints. It detects stale blocks, stuck chains, and syncing states, automatically triggering restart commands when configured thresholds are exceeded.

## Usage

```bash
# Run with default config (watcher_config.json)
./evm_watcher.py

# Run with specific config
./evm_watcher.py my_config.json
```

## Configuration (`watcher_config.json`)

### Global Settings
These apply to all chains unless overridden.

| Variable | Description | Default |
|----------|-------------|---------|
| `request_timeout` | Time (seconds) to wait for an RPC response. | `2` |
| `max_retries` | Number of retry attempts for failed RPC calls. | `3` |
| `retry_backoff` | Time (seconds) to wait between retries. | `1` |
| `default_lag_threshold` | Max allowed delay (seconds) between block timestamp and current time. | `60` |
| `stuck_check_interval` | Time (seconds) to wait between two block checks to confirm a chain is truly stuck. | `30` |
| `min_restart_interval` | Minimum time (seconds) between two consecutive restarts for the same chain. | `600` (10m) |

### Chain Configuration
Map specific Chain IDs to restart commands.

| Variable | Description |
|----------|-------------|
| `restart_command` | **Required.** Shell command to execute when the chain is unhealthy (e.g., `docker restart ...`). |
| `lag_threshold` | *Optional.* Overrides the global lag threshold for this specific chain. |
| `stuck_check_interval` | *Optional.* Overrides the global stuck check interval. |
| `min_restart_interval` | *Optional.* Overrides the global restart cooldown. |

### Example Config

```json
{
  "urls": ["http://localhost:8545"],
  "global_settings": {
    "default_lag_threshold": 60,
    "min_restart_interval": 600
  },
  "chains": {
    "137": {
      "name": "Polygon",
      "restart_command": "docker restart polygon-node",
      "lag_threshold": 120
    }
  }
}
```

## Logic Flow
1. **Sync Check:** If `eth_syncing` is true, the script assumes the node is recovering and **does not restart**.
2. **Lag Check:** If `current_time - block_timestamp > lag_threshold`, it flags the node as potentially unhealthy.
3. **Stuck Check:** It waits `stuck_check_interval` seconds and checks if the block number has increased. If not, the node is "stuck".
4. **Cooldown:** It checks if `min_restart_interval` has passed since the last restart.
5. **Action:** If all conditions are met, the `restart_command` is executed.

