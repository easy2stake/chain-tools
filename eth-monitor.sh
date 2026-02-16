#!/bin/bash

# Environment Variables (with defaults)
RPC_URL="${RPC_URL:-http://127.0.0.1:8545}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-10}"
BLOCK_LAG_THRESHOLD="${BLOCK_LAG_THRESHOLD:-120}"
TIMEOUT="${TIMEOUT:-2}"
VERBOSE="${VERBOSE:-false}"
LOG_FILE="${LOG_FILE:-$(dirname "$0")/log/eth-monitor.log}"
DOCKER_CONTAINER="${DOCKER_CONTAINER:-}"
CONTAINER_LOG_FOLDER="${CONTAINER_LOG_FOLDER:-}"
HOST_LOG_DEST="${HOST_LOG_DEST:-$(dirname "$0")/log/container-logs}"
SERVICE_NAME="${SERVICE_NAME:-}"
SERVICE_LOG_LINES="${SERVICE_LOG_LINES:-5000}"
HOST_SERVICE_LOG_DEST="${HOST_SERVICE_LOG_DEST:-$(dirname "$0")/log/service-logs}"
DRY_RUN="${DRY_RUN:-false}"

# Telegram configuration
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# Track last restart time to prevent too frequent restarts
LAST_RESTART_TIME=0
RESTART_COOLDOWN="${RESTART_COOLDOWN:-21600}"  # 6 hours default cooldown

# Function to display usage information
usage() {
  cat << EOF

Usage: $0 [OPTIONS]

Options:
  -h, --help              Show this help message
  -v, --verbose           Enable verbose output
  -d, --dry-run           Dry run mode (simulate restarts without actually restarting)
  -u, --url <url>         Override RPC_URL (default: $RPC_URL)
  -i, --interval <seconds> Override MONITOR_INTERVAL (default: $MONITOR_INTERVAL)
  -t, --threshold <seconds> Override BLOCK_LAG_THRESHOLD (default: $BLOCK_LAG_THRESHOLD)
  -l, --log-file <path>   Override LOG_FILE (default: log/eth-monitor.log, or log/<name>.log if -c/-s set)
  -c, --container <name>  Docker container name to restart when threshold exceeded (mutually exclusive with -s)
  -s, --service <name>    Systemd service name to restart when threshold exceeded (mutually exclusive with -c)
  --container-logs <path> Path to log folder inside container to copy to host
  --host-log-dest <path>  Destination folder on host for copied container logs (default: ./log/container-logs)
  --service-log-lines <n> Number of journal lines to capture for service before restart (default: 5000)
  --host-service-log-dest <path> Destination for service journal dumps (default: ./log/service-logs)

Environment Variables:
  RPC_URL                 RPC endpoint URL
  MONITOR_INTERVAL        Seconds between checks
  BLOCK_LAG_THRESHOLD     Max allowed lag in seconds
  TIMEOUT                 Request timeout in seconds
  VERBOSE                 Verbose output flag (true/false)
  LOG_FILE                Path to log file
  DOCKER_CONTAINER        Docker container name to restart when threshold exceeded (mutually exclusive with SERVICE_NAME)
  CONTAINER_LOG_FOLDER    Path to log folder inside container to copy to host
  HOST_LOG_DEST           Destination folder on host for copied container logs
  SERVICE_NAME            Systemd service name to restart when threshold exceeded (mutually exclusive with DOCKER_CONTAINER)
  SERVICE_LOG_LINES       Journal lines to capture before service restart (default: 5000)
  HOST_SERVICE_LOG_DEST   Destination for service journal dumps (default: ./log/service-logs)
  RESTART_COOLDOWN        Minimum seconds between restarts (default: 300)
  DRY_RUN                 Dry run mode flag (true/false)
  TELEGRAM_BOT_TOKEN      Bot token for Telegram notifications (optional)
  TELEGRAM_CHAT_ID        Chat ID for Telegram notifications (optional)

Examples:
  $0
  $0 -u http://localhost:8545 -i 60
  $0 --url http://localhost:8545 --interval 60
  RPC_URL=http://localhost:8545 MONITOR_INTERVAL=60 $0
  $0 -l /var/log/reth-monitor.log
  $0 -c reth-node --threshold 120
  $0 -c reth-node --dry-run  # Test without actually restarting
  $0 -c reth-node --container-logs /var/log/reth --host-log-dest ./logs
  $0 -s reth --threshold 120  # Systemd service
  $0 -s reth --service-log-lines 10000

EOF
  exit 0
}

# Function to log messages (to both console and file)
log() {
  local message="$1"
  local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
  local log_entry="[${timestamp}] ${message}"
  
  # Print to console
  echo "$log_entry"
  
  # Append to log file
  mkdir -p "$(dirname "$LOG_FILE")"
  echo "$log_entry" >> "$LOG_FILE"
}

# Function to send a message to Telegram chat.
# Returns 0 on success, 1 if not configured or on failure.
send_telegram_message() {
  local message="$1"
  local url
  local payload
  local response

  if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
    log "WARN: Telegram not configured. TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing."
    return 1
  fi

  url="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage"
  payload=$(jq -n --arg chat_id "$TELEGRAM_CHAT_ID" --arg text "$message" \
    '{chat_id: $chat_id, text: $text, parse_mode: "HTML"}') || {
    log "WARN: Failed to build Telegram payload"
    return 1
  }

  response=$(curl -s -w "\n%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d "$payload" \
    -m 10 \
    "$url" 2>&1) || {
    log "WARN: Failed to send Telegram message: curl failed"
    return 1
  }

  local http_code
  http_code=$(echo "$response" | tail -n1)
  response=$(echo "$response" | sed '$d')

  if [ "$http_code" != "200" ]; then
    log "WARN: Failed to send Telegram message: HTTP ${http_code}"
    return 1
  fi

  if ! echo "$response" | jq -e '.ok == true' >/dev/null 2>&1; then
    log "WARN: Failed to send Telegram message: API returned error"
    return 1
  fi

  return 0
}

# Function to convert hex to decimal
hex_to_dec() {
  local hex_value="$1"
  if [[ $hex_value =~ ^0x ]]; then
    printf "%d" "$((16#${hex_value:2}))"
  else
    printf "%d" "$((16#${hex_value}))"
  fi
}

# Function to format Unix timestamp to human-readable UTC format
format_timestamp() {
  local timestamp="$1"
  if command -v date >/dev/null 2>&1; then
    date -u -d "@${timestamp}" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || date -u -r "${timestamp}" '+%Y-%m-%d %H:%M:%S UTC' 2>/dev/null || echo "${timestamp}"
  else
    echo "${timestamp}"
  fi
}

# Function to make JSON-RPC call
make_rpc_call() {
  local method="$1"
  local params="$2"
  local response
  
  response=$(curl -s -X POST \
    -H "Content-Type: application/json" \
    -m "$TIMEOUT" \
    -d "{
      \"jsonrpc\": \"2.0\",
      \"method\": \"${method}\",
      \"params\": ${params},
      \"id\": 1
    }" "$RPC_URL" 2>&1)
  
  if [ $? -ne 0 ]; then
    echo "{\"error\":{\"message\":\"curl failed: ${response}\"}}"
    return 1
  fi
  
  echo "$response"
}

# Function to get latest block
get_latest_block() {
  local response
  local result
  
  response=$(make_rpc_call "eth_getBlockByNumber" '["latest", false]')
  
  if [ $? -ne 0 ] || [ -z "$response" ]; then
    return 1
  fi
  
  # Check for JSON-RPC error
  if echo "$response" | jq -e '.error' > /dev/null 2>&1; then
    return 1
  fi
  
  result=$(echo "$response" | jq -r '.result')
  
  if [ "$result" == "null" ] || [ -z "$result" ]; then
    return 1
  fi
  
  echo "$response"
}

# Function to check block lag
check_block_lag() {
  local block_data="$1"
  local block_number_hex
  local block_timestamp_hex
  local block_timestamp
  local current_time
  local lag
  local status
  local block_time_formatted
  
  # Extract block number and timestamp
  block_number_hex=$(echo "$block_data" | jq -r '.result.number // empty')
  block_timestamp_hex=$(echo "$block_data" | jq -r '.result.timestamp // empty')
  
  if [ -z "$block_number_hex" ] || [ -z "$block_timestamp_hex" ] || [ "$block_number_hex" == "null" ] || [ "$block_timestamp_hex" == "null" ]; then
    log "ERROR: Failed to extract block data"
    send_telegram_message "⚠️ <b>eth-monitor</b>: Failed to extract block data from RPC ($RPC_URL)"
    return 1
  fi
  
  # Convert hex to decimal
  block_number=$(hex_to_dec "$block_number_hex")
  block_timestamp=$(hex_to_dec "$block_timestamp_hex")
  
  # Get current time
  current_time=$(date +%s)
  
  # Calculate lag
  lag=$((current_time - block_timestamp))
  
  # Format block timestamp
  block_time_formatted=$(format_timestamp "$block_timestamp")
  
  # Determine status (single threshold: OK or ERROR)
  if [ $lag -le $BLOCK_LAG_THRESHOLD ]; then
    status="OK"
  else
    status="ERROR"
  fi
  
  # Holdoff remaining (when container/service is used)
  local holdoff_remaining=0
  if [ -n "$DOCKER_CONTAINER" ] || [ -n "$SERVICE_NAME" ]; then
    local time_since_restart=$((current_time - LAST_RESTART_TIME))
    holdoff_remaining=$((RESTART_COOLDOWN - time_since_restart))
    [ "$holdoff_remaining" -lt 0 ] && holdoff_remaining=0
  fi
  
  # Log the status (include container or service and holdoff remaining if provided)
  if [ -n "$DOCKER_CONTAINER" ]; then
    log "Block: ${block_number} | Block Time: ${block_time_formatted} | Lag: ${lag}s | Status: ${status} | Container: ${DOCKER_CONTAINER} | Holdoff remaining: ${holdoff_remaining}s"
  elif [ -n "$SERVICE_NAME" ]; then
    log "Block: ${block_number} | Block Time: ${block_time_formatted} | Lag: ${lag}s | Status: ${status} | Service: ${SERVICE_NAME} | Holdoff remaining: ${holdoff_remaining}s"
  else
    log "Block: ${block_number} | Block Time: ${block_time_formatted} | Lag: ${lag}s | Status: ${status}"
  fi
  
  # Verbose mode: show full JSON
  if [ "$VERBOSE" == "true" ]; then
    echo "$block_data" | jq .
  fi
  
  # Return status code: 0=OK, 1=ERROR
  if [ "$status" == "OK" ]; then
    return 0
  else
    return 1
  fi
}

# Function to copy logs from container to host
copy_container_logs() {
  local container_name="$1"
  local container_log_path="$2"
  local host_dest="$3"
  
  if [ -z "$container_name" ] || [ -z "$container_log_path" ]; then
    return 0  # Not an error, just skip if not configured
  fi
  
  # Create destination directory if it doesn't exist
  mkdir -p "$host_dest"
  
  # Generate timestamped destination folder
  local timestamp=$(date '+%Y%m%d_%H%M%S')
  local dest_folder="${host_dest}/${container_name}_${timestamp}"
  
  # Check if container exists
  if ! docker ps -a --format '{{.Names}}' | grep -q "^${container_name}$"; then
    log "WARN: Docker container '${container_name}' not found, skipping log copy"
    return 1
  fi
  
  # Copy logs from container to host (works even if container is stopped)
  # In dry-run mode, we still copy logs (only restart is skipped)
  log "Copying logs from ${container_name}:${container_log_path} to ${dest_folder}"
  if docker cp "${container_name}:${container_log_path}" "$dest_folder" > /dev/null 2>&1; then
    if [ "$DRY_RUN" == "true" ]; then
      log "[DRY RUN] Successfully copied logs to ${dest_folder}"
    else
      log "Successfully copied logs to ${dest_folder}"
    fi
    return 0
  else
    log "WARN: Failed to copy logs from container '${container_name}' (container may be stopped or path doesn't exist)"
    return 1
  fi
}

# Function to copy systemd service journal to host before restart
copy_service_logs() {
  local service_name="$1"
  local host_dest="$2"
  local lines="${SERVICE_LOG_LINES:-5000}"

  if [ -z "$service_name" ]; then
    return 0
  fi

  mkdir -p "$host_dest"
  local timestamp=$(date '+%Y%m%d_%H%M%S')
  local dest_file="${host_dest}/${service_name}_${timestamp}.journal.log"

  if ! systemctl show "$service_name" &>/dev/null; then
    log "WARN: Systemd service '${service_name}' not found, skipping journal copy"
    return 1
  fi

  log "Copying journal for ${service_name} (last ${lines} lines) to ${dest_file}"
  if journalctl -u "$service_name" -n "$lines" --no-pager > "$dest_file" 2>/dev/null; then
    if [ "$DRY_RUN" == "true" ]; then
      log "[DRY RUN] Successfully copied service journal to ${dest_file}"
    else
      log "Successfully copied service journal to ${dest_file}"
    fi
    return 0
  else
    log "WARN: Failed to copy journal for service '${service_name}'"
    return 1
  fi
}

# Function to restart Docker container
restart_docker_container() {
  local container_name="$1"
  
  if [ -z "$container_name" ]; then
    log "WARN: Docker container name not provided, skipping restart"
    return 1
  fi
  
  # Check if cooldown period has passed
  local current_time=$(date +%s)
  local time_since_restart=$((current_time - LAST_RESTART_TIME))
  
  if [ $time_since_restart -lt $RESTART_COOLDOWN ]; then
    log "WARN: Restart cooldown active. Last restart was ${time_since_restart}s ago (cooldown: ${RESTART_COOLDOWN}s). Skipping restart."
    send_telegram_message "⏳ <b>eth-monitor</b>: Restart skipped (cooldown). Container: ${container_name}. Last restart ${time_since_restart}s ago (cooldown: ${RESTART_COOLDOWN}s)."
    return 1
  fi
  
  # Check if container exists
  if ! docker ps -a --format '{{.Names}}' | grep -q "^${container_name}$"; then
    log "ERROR: Docker container '${container_name}' not found"
    send_telegram_message "❌ <b>eth-monitor</b>: Docker container '${container_name}' not found. Cannot restart."
    return 1
  fi
  
  # Copy logs from container before restarting (if configured)
  if [ -n "$CONTAINER_LOG_FOLDER" ]; then
    copy_container_logs "$container_name" "$CONTAINER_LOG_FOLDER" "$HOST_LOG_DEST"
  fi
  
  # Restart the container
  if [ "$DRY_RUN" == "true" ]; then
    log "[DRY RUN] Would restart Docker container: ${container_name}"
    send_telegram_message "🔄 <b>eth-monitor</b> [DRY RUN]: Would restart container: ${container_name} (block lag exceeded on $RPC_URL)"
    LAST_RESTART_TIME=$current_time
    return 0
  else
    log "Restarting Docker container: ${container_name}"
    send_telegram_message "🔄 <b>eth-monitor</b>: Restarting container: ${container_name} (block lag exceeded on $RPC_URL)"
    if docker restart "$container_name" > /dev/null 2>&1; then
      LAST_RESTART_TIME=$current_time
      log "Successfully restarted Docker container: ${container_name}"
      send_telegram_message "✅ <b>eth-monitor</b>: Successfully restarted container: ${container_name}"
      return 0
    else
      log "ERROR: Failed to restart Docker container: ${container_name}"
      send_telegram_message "❌ <b>eth-monitor</b>: Failed to restart container: ${container_name}"
      return 1
    fi
  fi
}

# Function to restart systemd service
restart_systemd_service() {
  local service_name="$1"

  if [ -z "$service_name" ]; then
    log "WARN: Systemd service name not provided, skipping restart"
    return 1
  fi

  local current_time=$(date +%s)
  local time_since_restart=$((current_time - LAST_RESTART_TIME))

  if [ $time_since_restart -lt $RESTART_COOLDOWN ]; then
    log "WARN: Restart cooldown active. Last restart was ${time_since_restart}s ago (cooldown: ${RESTART_COOLDOWN}s). Skipping restart."
    send_telegram_message "⏳ <b>eth-monitor</b>: Restart skipped (cooldown). Service: ${service_name}. Last restart ${time_since_restart}s ago (cooldown: ${RESTART_COOLDOWN}s)."
    return 1
  fi

  if ! systemctl show "$service_name" &>/dev/null; then
    log "ERROR: Systemd service '${service_name}' not found"
    send_telegram_message "❌ <b>eth-monitor</b>: Systemd service '${service_name}' not found. Cannot restart."
    return 1
  fi

  copy_service_logs "$service_name" "$HOST_SERVICE_LOG_DEST"

  if [ "$DRY_RUN" == "true" ]; then
    log "[DRY RUN] Would restart systemd service: ${service_name}"
    send_telegram_message "🔄 <b>eth-monitor</b> [DRY RUN]: Would restart service: ${service_name} (block lag exceeded on $RPC_URL)"
    LAST_RESTART_TIME=$current_time
    return 0
  else
    log "Restarting systemd service: ${service_name}"
    send_telegram_message "🔄 <b>eth-monitor</b>: Restarting service: ${service_name} (block lag exceeded on $RPC_URL)"
    if systemctl restart "$service_name" 2>/dev/null; then
      LAST_RESTART_TIME=$current_time
      log "Successfully restarted systemd service: ${service_name}"
      send_telegram_message "✅ <b>eth-monitor</b>: Successfully restarted service: ${service_name}"
      return 0
    else
      log "ERROR: Failed to restart systemd service: ${service_name}"
      send_telegram_message "❌ <b>eth-monitor</b>: Failed to restart service: ${service_name}"
      return 1
    fi
  fi
}

# Sync LAST_RESTART_TIME from container/service start time (Linux).
# When running in the loop, detects external restarts (start time > our recorded + 10s),
# updates cooldown state, and logs + sends Telegram.
sync_restart_time_from_target() {
  local current_started_at=0
  local target_name=""
  local target_type=""

  if [ -n "$DOCKER_CONTAINER" ]; then
    target_type="container"
    target_name="$DOCKER_CONTAINER"
    local started_at
    started_at=$(docker inspect --format '{{.State.StartedAt}}' "$DOCKER_CONTAINER" 2>/dev/null)
    if [ -n "$started_at" ] && [[ "$started_at" != "0001-01-01"* ]]; then
      current_started_at=$(date -d "$started_at" +%s 2>/dev/null)
    fi
  elif [ -n "$SERVICE_NAME" ]; then
    target_type="service"
    target_name="$SERVICE_NAME"
    local ts
    ts=$(systemctl show -p ActiveEnterTimestamp --value "$SERVICE_NAME" 2>/dev/null)
    if [ -n "$ts" ] && [ "$ts" != "n/a" ]; then
      current_started_at=$(date -d "$ts" +%s 2>/dev/null)
    fi
  else
    return 0
  fi

  [ -z "$current_started_at" ] || [ "$current_started_at" -le 0 ] && return 0

  if [ "$current_started_at" -gt "$((LAST_RESTART_TIME + 10))" ] && [ "$LAST_RESTART_TIME" -gt 0 ]; then
    LAST_RESTART_TIME=$current_started_at
    log "Detected external restart of ${target_type} '${target_name}'. Cooldown applied (${RESTART_COOLDOWN}s)."
    send_telegram_message "🔄 <b>eth-monitor</b>: Detected external restart of ${target_type} <b>${target_name}</b>. Cooldown applied (${RESTART_COOLDOWN}s)."
  elif [ "$current_started_at" -gt "$LAST_RESTART_TIME" ]; then
    LAST_RESTART_TIME=$current_started_at
  fi
}

# Function to validate RPC endpoint
validate_endpoint() {
  local response
  
  log "Validating RPC endpoint: $RPC_URL"
  
  response=$(make_rpc_call "eth_blockNumber" "[]")
  
  if [ $? -ne 0 ]; then
    log "ERROR: Failed to connect to RPC endpoint"
    send_telegram_message "❌ <b>eth-monitor</b>: Startup validation failed — cannot connect to RPC endpoint $RPC_URL"
    return 1
  fi
  
  if echo "$response" | jq -e '.error' > /dev/null 2>&1; then
    local error_msg=$(echo "$response" | jq -r '.error.message // "Unknown error"')
    log "ERROR: RPC error: $error_msg"
    send_telegram_message "❌ <b>eth-monitor</b>: Startup validation failed — RPC error: $error_msg ($RPC_URL)"
    return 1
  fi
  
  local block_number=$(echo "$response" | jq -r '.result')
  if [ "$block_number" == "null" ] || [ -z "$block_number" ]; then
    log "ERROR: Invalid response from RPC endpoint"
    send_telegram_message "❌ <b>eth-monitor</b>: Startup validation failed — invalid RPC response from $RPC_URL"
    return 1
  fi
  
  log "RPC endpoint validated successfully"
  return 0
}

# Function to handle cleanup on exit
cleanup() {
  log "Monitoring stopped"
  exit 0
}

# Send structured startup info to Telegram when a new instance starts
send_telegram_startup() {
  local holdoff_status="—"
  if [ -n "$DOCKER_CONTAINER" ] || [ -n "$SERVICE_NAME" ]; then
    local current_time=$(date +%s)
    local time_since_restart=$((current_time - LAST_RESTART_TIME))
    if [ "$LAST_RESTART_TIME" -gt 0 ] && [ $time_since_restart -lt $RESTART_COOLDOWN ]; then
      holdoff_status="active (${time_since_restart}s since restart)"
    else
      holdoff_status="inactive"
    fi
  fi

  local target="—"
  [ -n "$DOCKER_CONTAINER" ] && target="Container: ${DOCKER_CONTAINER}"
  [ -n "$SERVICE_NAME" ] && target="Service: ${SERVICE_NAME}"

  local dry=""
  [ "$DRY_RUN" == "true" ] && dry="\n<b>DRY RUN</b>"

  local msg="🟢 <b>eth-monitor started</b>${dry}
<b>RPC:</b> ${RPC_URL}
<b>Interval:</b> ${MONITOR_INTERVAL}s · <b>Threshold:</b> ${BLOCK_LAG_THRESHOLD}s
<b>Target:</b> ${target}
<b>Cooldown:</b> ${RESTART_COOLDOWN}s · <b>Holdoff:</b> ${holdoff_status}
<b>Log:</b> ${LOG_FILE}"
  send_telegram_message "$msg"
}

# Main monitoring loop
monitor_loop() {
  local block_data
  
  log "Starting continuous monitoring (interval: ${MONITOR_INTERVAL}s, threshold: ${BLOCK_LAG_THRESHOLD}s)"
  log "Log file: $LOG_FILE"
  if [ "$DRY_RUN" == "true" ]; then
    log "DRY RUN MODE: Container/service restarts will be simulated only"
  fi
  if [ -n "$DOCKER_CONTAINER" ]; then
    if [ "$DRY_RUN" == "true" ]; then
      log "Docker container restart enabled (DRY RUN): ${DOCKER_CONTAINER} (cooldown: ${RESTART_COOLDOWN}s)"
    else
      log "Docker container restart enabled: ${DOCKER_CONTAINER} (cooldown: ${RESTART_COOLDOWN}s)"
    fi
    if [ -n "$CONTAINER_LOG_FOLDER" ]; then
      log "Container log copying enabled: ${CONTAINER_LOG_FOLDER} -> ${HOST_LOG_DEST}"
    fi
  fi
  if [ -n "$SERVICE_NAME" ]; then
    if [ "$DRY_RUN" == "true" ]; then
      log "Systemd service restart enabled (DRY RUN): ${SERVICE_NAME} (cooldown: ${RESTART_COOLDOWN}s)"
    else
      log "Systemd service restart enabled: ${SERVICE_NAME} (cooldown: ${RESTART_COOLDOWN}s)"
    fi
    log "Service journal capture: last ${SERVICE_LOG_LINES} lines -> ${HOST_SERVICE_LOG_DEST}"
  fi
  
  # Set up signal handlers
  trap cleanup SIGINT SIGTERM
  
  while true; do
    sync_restart_time_from_target
    # Get latest block
    block_data=$(get_latest_block)
    
    if [ $? -eq 0 ] && [ -n "$block_data" ]; then
      # Check block lag
      check_block_lag "$block_data"
      local lag_status=$?
      
      # Restart container or service if threshold exceeded (ERROR status)
      if [ $lag_status -eq 1 ]; then
        if [ -n "$DOCKER_CONTAINER" ]; then
          send_telegram_message "⚠️ <b>eth-monitor</b>: Block lag exceeded threshold (${BLOCK_LAG_THRESHOLD}s) on $RPC_URL | Container: $DOCKER_CONTAINER"
          restart_docker_container "$DOCKER_CONTAINER"
        elif [ -n "$SERVICE_NAME" ]; then
          send_telegram_message "⚠️ <b>eth-monitor</b>: Block lag exceeded threshold (${BLOCK_LAG_THRESHOLD}s) on $RPC_URL | Service: $SERVICE_NAME"
          restart_systemd_service "$SERVICE_NAME"
        else
          send_telegram_message "⚠️ <b>eth-monitor</b>: Block lag exceeded threshold (${BLOCK_LAG_THRESHOLD}s) on $RPC_URL"
        fi
      fi
    else
      log "ERROR: Failed to fetch latest block"
      send_telegram_message "⚠️ <b>eth-monitor</b>: Failed to fetch latest block from $RPC_URL"
    fi
    
    # Sleep for the specified interval
    sleep "$MONITOR_INTERVAL"
  done
}

# Parse command-line arguments
ARGS=()
while [[ $# -gt 0 ]]; do
  case $1 in
    -h|--help)
      usage
      ;;
    -v|--verbose)
      VERBOSE=true
      shift
      ;;
    -d|--dry-run)
      DRY_RUN=true
      shift
      ;;
    -u|--url)
      RPC_URL="$2"
      shift 2
      ;;
    -i|--interval)
      MONITOR_INTERVAL="$2"
      shift 2
      ;;
    -t|--threshold)
      BLOCK_LAG_THRESHOLD="$2"
      shift 2
      ;;
    -l|--log-file)
      LOG_FILE="$2"
      shift 2
      ;;
    -c|--container)
      DOCKER_CONTAINER="$2"
      shift 2
      ;;
    -s|--service)
      SERVICE_NAME="$2"
      shift 2
      ;;
    --container-logs)
      CONTAINER_LOG_FOLDER="$2"
      shift 2
      ;;
    --host-log-dest)
      HOST_LOG_DEST="$2"
      shift 2
      ;;
    --service-log-lines)
      SERVICE_LOG_LINES="$2"
      shift 2
      ;;
    --host-service-log-dest)
      HOST_SERVICE_LOG_DEST="$2"
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

# Docker container and systemd service are mutually exclusive
if [ -n "$DOCKER_CONTAINER" ] && [ -n "$SERVICE_NAME" ]; then
  echo "ERROR: DOCKER_CONTAINER and SERVICE_NAME are mutually exclusive. Set only one of -c/--container or -s/--service."
  exit 1
fi

# When using a container or service and log file was not explicitly set, use log/<name>.log
default_log_file="$(dirname "$0")/log/eth-monitor.log"
if [ -n "$DOCKER_CONTAINER" ] && [ "$LOG_FILE" = "$default_log_file" ]; then
  LOG_FILE="$(dirname "$0")/log/${DOCKER_CONTAINER}.log"
elif [ -n "$SERVICE_NAME" ] && [ "$LOG_FILE" = "$default_log_file" ]; then
  LOG_FILE="$(dirname "$0")/log/${SERVICE_NAME}.log"
fi

# Check if jq is available
if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required but not installed. Please install jq to use this script."
  exit 1
fi

# Check if docker is available (if container name is provided)
if [ -n "$DOCKER_CONTAINER" ] && ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is required but not installed. Please install docker to use container restart feature."
  exit 1
fi

# Check if systemctl is available (if service name is provided)
if [ -n "$SERVICE_NAME" ] && ! command -v systemctl >/dev/null 2>&1; then
  echo "ERROR: systemctl is required but not available. Please run with a systemd system to use service restart feature."
  exit 1
fi

# Validate container exists (if container name is provided)
if [ -n "$DOCKER_CONTAINER" ]; then
  if ! docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${DOCKER_CONTAINER}$"; then
    echo "ERROR: Docker container '${DOCKER_CONTAINER}' not found. List containers with: docker ps -a"
    exit 1
  fi
fi

# Validate systemd service exists (if service name is provided)
if [ -n "$SERVICE_NAME" ]; then
  if ! systemctl show "$SERVICE_NAME" &>/dev/null; then
    echo "ERROR: Systemd service '${SERVICE_NAME}' not found. List units with: systemctl list-units -t service"
    exit 1
  fi
fi

# Initialize LAST_RESTART_TIME from container/service so cooldown accounts for manual restarts (Linux)
sync_restart_time_from_target

# Log last restart time and holdoff status at startup (when container/service is used)
if [ -n "$DOCKER_CONTAINER" ] || [ -n "$SERVICE_NAME" ]; then
  if [ "$LAST_RESTART_TIME" -gt 0 ]; then
    log "Last restart time: $(format_timestamp "$LAST_RESTART_TIME")"
    current_time=$(date +%s)
    time_since_restart=$((current_time - LAST_RESTART_TIME))
    if [ $time_since_restart -lt $RESTART_COOLDOWN ]; then
      log "Holdoff: active (${time_since_restart}s since restart, cooldown ${RESTART_COOLDOWN}s)"
    else
      log "Holdoff: inactive (${time_since_restart}s since restart, cooldown ${RESTART_COOLDOWN}s)"
    fi
  else
    log "Last restart time: unknown (no previous start time from target)"
    log "Holdoff: inactive"
  fi
fi

# Validate endpoint before starting
if ! validate_endpoint; then
  exit 1
fi

# Notify Telegram of new instance startup (structured minimal info)
send_telegram_startup

# Start monitoring
monitor_loop
