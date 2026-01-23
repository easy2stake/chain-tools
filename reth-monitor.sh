#!/bin/bash

# Environment Variables (with defaults)
RETH_URL="${RETH_URL:-http://127.0.0.1:8545}"
MONITOR_INTERVAL="${MONITOR_INTERVAL:-30}"
BLOCK_LAG_THRESHOLD="${BLOCK_LAG_THRESHOLD:-60}"
TIMEOUT="${TIMEOUT:-10}"
VERBOSE="${VERBOSE:-false}"
LOG_FILE="${LOG_FILE:-$(dirname "$0")/reth-monitor.log}"
DOCKER_CONTAINER="${DOCKER_CONTAINER:-}"

# Track last restart time to prevent too frequent restarts
LAST_RESTART_TIME=0
RESTART_COOLDOWN="${RESTART_COOLDOWN:-300}"  # 5 minutes default cooldown

# Function to display usage information
usage() {
  cat << EOF

Usage: $0 [OPTIONS]

Options:
  -h, --help              Show this help message
  -v, --verbose           Enable verbose output
  -u, --url <url>         Override RETH_URL (default: $RETH_URL)
  -i, --interval <seconds> Override MONITOR_INTERVAL (default: $MONITOR_INTERVAL)
  -t, --threshold <seconds> Override BLOCK_LAG_THRESHOLD (default: $BLOCK_LAG_THRESHOLD)
  -l, --log-file <path>   Override LOG_FILE (default: $LOG_FILE)
  -c, --container <name>  Docker container name to restart when threshold exceeded

Environment Variables:
  RETH_URL                RPC endpoint URL
  MONITOR_INTERVAL        Seconds between checks
  BLOCK_LAG_THRESHOLD     Max allowed lag in seconds
  TIMEOUT                 Request timeout in seconds
  VERBOSE                 Verbose output flag (true/false)
  LOG_FILE                Path to log file
  DOCKER_CONTAINER        Docker container name to restart when threshold exceeded
  RESTART_COOLDOWN        Minimum seconds between restarts (default: 300)

Examples:
  $0
  $0 -u http://localhost:8545 -i 60
  $0 --url http://localhost:8545 --interval 60
  RETH_URL=http://localhost:8545 MONITOR_INTERVAL=60 $0
  $0 -l /var/log/reth-monitor.log
  $0 -c reth-node --threshold 120

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
    }" "$RETH_URL" 2>&1)
  
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
  
  # Determine status
  if [ $lag -le $BLOCK_LAG_THRESHOLD ]; then
    status="OK"
  elif [ $lag -le $((BLOCK_LAG_THRESHOLD * 2)) ]; then
    status="WARN"
  else
    status="ERROR"
  fi
  
  # Log the status
  log "Block: ${block_number} | Block Time: ${block_time_formatted} | Lag: ${lag}s | Status: ${status}"
  
  # Verbose mode: show full JSON
  if [ "$VERBOSE" == "true" ]; then
    echo "$block_data" | jq .
  fi
  
  # Return status code: 0=OK, 1=WARN, 2=ERROR
  if [ "$status" == "OK" ]; then
    return 0
  elif [ "$status" == "WARN" ]; then
    return 1
  else
    return 2
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
    return 1
  fi
  
  # Check if container exists
  if ! docker ps -a --format '{{.Names}}' | grep -q "^${container_name}$"; then
    log "ERROR: Docker container '${container_name}' not found"
    return 1
  fi
  
  # Restart the container
  log "Restarting Docker container: ${container_name}"
  if docker restart "$container_name" > /dev/null 2>&1; then
    LAST_RESTART_TIME=$current_time
    log "Successfully restarted Docker container: ${container_name}"
    return 0
  else
    log "ERROR: Failed to restart Docker container: ${container_name}"
    return 1
  fi
}

# Function to validate RPC endpoint
validate_endpoint() {
  local response
  
  log "Validating RPC endpoint: $RETH_URL"
  
  response=$(make_rpc_call "eth_blockNumber" "[]")
  
  if [ $? -ne 0 ]; then
    log "ERROR: Failed to connect to RPC endpoint"
    return 1
  fi
  
  if echo "$response" | jq -e '.error' > /dev/null 2>&1; then
    local error_msg=$(echo "$response" | jq -r '.error.message // "Unknown error"')
    log "ERROR: RPC error: $error_msg"
    return 1
  fi
  
  local block_number=$(echo "$response" | jq -r '.result')
  if [ "$block_number" == "null" ] || [ -z "$block_number" ]; then
    log "ERROR: Invalid response from RPC endpoint"
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

# Main monitoring loop
monitor_loop() {
  local block_data
  
  log "Starting continuous monitoring (interval: ${MONITOR_INTERVAL}s, threshold: ${BLOCK_LAG_THRESHOLD}s)"
  log "Log file: $LOG_FILE"
  if [ -n "$DOCKER_CONTAINER" ]; then
    log "Docker container restart enabled: ${DOCKER_CONTAINER} (cooldown: ${RESTART_COOLDOWN}s)"
  fi
  
  # Set up signal handlers
  trap cleanup SIGINT SIGTERM
  
  while true; do
    # Get latest block
    block_data=$(get_latest_block)
    
    if [ $? -eq 0 ] && [ -n "$block_data" ]; then
      # Check block lag
      check_block_lag "$block_data"
      local lag_status=$?
      
      # Restart container if threshold exceeded (ERROR status)
      if [ $lag_status -eq 2 ] && [ -n "$DOCKER_CONTAINER" ]; then
        restart_docker_container "$DOCKER_CONTAINER"
      fi
    else
      log "ERROR: Failed to fetch latest block"
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
    -u|--url)
      RETH_URL="$2"
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
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

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

# Validate endpoint before starting
if ! validate_endpoint; then
  exit 1
fi

# Start monitoring
monitor_loop
