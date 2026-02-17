#!/bin/bash
# Install systemd service to run eth-monitors (run_eth_monitors.py)
# Installs as root, enables on boot, starts the service.

set -e

SERVICE_NAME="eth-monitors"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/run_eth_monitors.py"
CONFIG_FILE="${SCRIPT_DIR}/eth-monitor-config.yaml"
UNIT_DIR="/etc/systemd/system"

# Sanity checks
if [[ ! -f "$PYTHON_SCRIPT" ]]; then
    echo "ERROR: run_eth_monitors.py not found at $PYTHON_SCRIPT" >&2
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: Config file not found at $CONFIG_FILE" >&2
    echo "Copy eth-monitor-config-example.yaml to eth-monitor-config.yaml and edit it." >&2
    exit 1
fi

PYTHON="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
if [[ -z "$PYTHON" ]]; then
    echo "ERROR: python3 not found in PATH" >&2
    exit 1
fi

# Generate and install unit file
TMP_UNIT=$(mktemp)
trap 'rm -f "$TMP_UNIT"' EXIT

cat > "$TMP_UNIT" << EOF
[Unit]
Description=Eth monitors (run_eth_monitors.py)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$PYTHON $PYTHON_SCRIPT
WorkingDirectory=$SCRIPT_DIR
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=eth-monitors

[Install]
WantedBy=multi-user.target
EOF

sudo cp "$TMP_UNIT" "${UNIT_DIR}/${SERVICE_NAME}.service"
echo "Installed ${SERVICE_NAME}.service"

sudo systemctl daemon-reload
echo "Reloaded systemd"

sudo systemctl enable "$SERVICE_NAME".service
echo "Enabled ${SERVICE_NAME}.service (start on boot)"

sudo systemctl start "$SERVICE_NAME".service
echo "Started ${SERVICE_NAME}.service"

echo ""
echo "Done. Useful commands:"
echo "  sudo systemctl status $SERVICE_NAME    # check status"
echo "  sudo systemctl start $SERVICE_NAME    # start"
echo "  sudo systemctl stop $SERVICE_NAME     # stop"
echo "  journalctl -u $SERVICE_NAME -f        # follow logs"
