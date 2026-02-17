# eth-monitor

Monitors Ethereum RPC endpoints for block lag and optionally restarts containers or systemd services when thresholds are exceeded.

## Systemd service

To run the monitors as a systemd service (runs as root, starts on boot):

1. **Create config:** Copy the example and edit:
   ```bash
   cp config-example.yaml config.yaml
   # Edit config.yaml with your chains and RPC URLs
   ```

2. **Install:** Run the install script (requires sudo):
   ```bash
   ./install-eth-monitors-service.sh
   ```
   This installs the service, enables it on boot, and starts it.

3. **Manage the service:**
   ```bash
   sudo systemctl status eth-monitors   # check status
   sudo systemctl start eth-monitors    # start
   sudo systemctl stop eth-monitors     # stop
   journalctl -u eth-monitors -f       # follow logs
   ```
