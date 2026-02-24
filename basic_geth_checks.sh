#!/bin/bash

# Script: basic_geth_checks_with_timestamps.sh
# This script performs basic checks on a Geth node by calling various JSON-RPC methods.
# It checks peer count, sync status, current block number, finalized block, and earliest block along with their timestamps.
# The general_check now also prints the chain ID before executing other checks.

# Function for verbose logging
log() {
  echo -e "$1"
}

# Execute RPC call, sets RPC_RESULT and RPC_ELAPSED globals (must be called in current shell)
timed_rpc() {
  local payload=$1
  local start end
  start=$(python3 -c 'import time; print(time.time())' 2>/dev/null || date +%s)
  RPC_RESULT=$(curl -s -X POST -H "Content-Type: application/json" -m 2 -d "$payload" "$URL")
  end=$(python3 -c 'import time; print(time.time())' 2>/dev/null || date +%s)
  RPC_ELAPSED=$(echo "$end - $start" | bc -l 2>/dev/null || echo "0")
}

# Function to display usage information
usage() {
  cat << EOF

Usage: $0 <full_url_or_port> <command> [command_options]

Commands:
  general_check                  Perform all basic Geth checks (chain ID, peer count, sync status, blocks).
  monitor                        Same as general_check but loops every second (Ctrl+C to stop).
  block_summary <block_number>   Fetch details of a specific block by its number.
  get_block <block_number>       Print the full block content for the specified block number.
  get_balance <account> [block_height] Fetch the balance of an account at a specific block height (default: latest).
  tx <tx_hash>                   Fetch details of a specific transaction by its hash.
  prysm_peers                  Extract consensus layer PRYSM peers.
Examples:
  $0 8545 general_check
  $0 8545 monitor
  $0 127.0.0.1:8545 block_summary <block_number>
  $0 127.0.0.1:8545 get_block <block_number>
  $0 127.0.0.1:8545 tx <tx_hash>
  $0 127.0.0.1:8545 prysm_peers
  $0 127.0.0.1:8545 <command> <command_params>
EOF
  exit 1
}

# If the first argument is --help or -h, display usage information.
if [[ "$1" == "--help" || "$1" == "-h" ]]; then
  usage
fi

# Check if the first argument (URL or port) is provided
if [ -z "$1" ]; then
  log "Error: URL or Port number not provided."
  usage
fi

# Assign the argument to a variable
if [[ "$1" =~ ^[0-9]+$ ]]; then
  # If only a port is provided, default to 127.0.0.1:$port
  URL="127.0.0.1:$1"
else
  # Otherwise, expect a full URL
  URL="$1"
fi

# Log the start of the process
log "Starting Geth checks on URL: $URL"

# Function to fetch and parse block data; outputs "ELAPSED:X.XXX" on first line, then JSON (for subshell calls)
get_block_data() {
  block_number=$1
  timed_rpc '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params": ["'"$block_number"'", true],"id":1}'
  echo "ELAPSED:${RPC_ELAPSED}"
  echo "$RPC_RESULT"
}

# Function to extract field from block data
extract_field() {
  data=$1
  field=$2
  echo "$data" | jq -r ".result.$field"
}

# Function to safely convert hex to decimal
safe_hex_to_dec() {
  hex_value=$1
  if [[ $hex_value =~ ^0x ]]; then
    printf "%d" "$((16#${hex_value:2}))"
  else
    echo "0"
  fi
}

# Function to safely convert decimal to hex
safe_dec_to_hex() {
  dec_value=$1
  printf "0x%x" "$dec_value"
}

# Function to convert Unix timestamp to UTC format
timestamp_to_utc() {
  unix_timestamp=$1
  date -u -d "@$unix_timestamp" +"%Y-%m-%dT%H:%M:%SZ"
}

# Format seconds as human-readable duration (e.g. 125 -> "2m 5s")
format_seconds() {
  local sec=$1
  [ -z "$sec" ] || [ "$sec" -le 0 ] && echo "0s" && return
  if [ "$sec" -lt 60 ]; then
    echo "${sec}s"
  elif [ "$sec" -lt 3600 ]; then
    echo "$((sec/60))m $((sec%60))s"
  elif [ "$sec" -lt 86400 ]; then
    echo "$((sec/3600))h $((sec%3600/60))m"
  else
    echo "$((sec/86400))d $((sec%86400/3600))h"
  fi
}

# Function to format block age (current time - block time)
format_age() {
  block_ts=$1
  [ -z "$block_ts" ] || [ "$block_ts" = "0" ] && echo "-" && return
  now=$(date +%s 2>/dev/null || echo "0")
  age=$((now - block_ts))
  [ "$age" -lt 0 ] && echo "-" && return
  if [ "$age" -lt 60 ]; then
    echo "${age}s"
  elif [ "$age" -lt 3600 ]; then
    echo "$((age/60))m $((age%60))s"
  elif [ "$age" -lt 86400 ]; then
    echo "$((age/3600))h $((age%3600/60))m"
  else
    echo "$((age/86400))d $((age%86400/3600))h"
  fi
}

# Blocks/sec from latest block increments (persisted in /tmp for watch -n1)
# Keeps a 60s rolling window of (timestamp, block) samples for a smoother average.
# Second arg is endpoint (URL or port) so each chain has its own state file.
print_blocks_per_sec() {
  local latest_block=$1
  local endpoint=${2:-default}
  local suffix=$(echo "$endpoint" | tr -c '[:alnum:]' '_')
  local RATE_FILE="/tmp/geth_block_rate_${suffix}.tmp"
  local now=$(date +%s)
  local bps="-"
  local window_sec=60
  local min_ts="" max_ts="" block_at_min="" block_at_max=""

  # Read existing samples, keep only from last 60s
  if [ -f "$RATE_FILE" ]; then
    while read -r ts block; do
      [ -z "$ts" ] && continue
      age=$((now - ts))
      [ "$age" -gt "$window_sec" ] && continue
      if [ -z "$min_ts" ] || [ "$ts" -lt "$min_ts" ]; then min_ts=$ts; block_at_min=$block; fi
      if [ -z "$max_ts" ] || [ "$ts" -gt "$max_ts" ]; then max_ts=$ts; block_at_max=$block; fi
    done < "$RATE_FILE" 2>/dev/null
  fi

  # Include current sample
  if [ -z "$min_ts" ] || [ "$now" -lt "$min_ts" ]; then min_ts=$now; block_at_min=$latest_block; fi
  if [ -z "$max_ts" ] || [ "$now" -gt "$max_ts" ]; then max_ts=$now; block_at_max=$latest_block; fi

  # Average bps over window (oldest vs newest in window)
  if [ -n "$min_ts" ] && [ -n "$max_ts" ] && [ "$max_ts" -gt "$min_ts" ] && [ "$block_at_max" -ge "$block_at_min" ]; then
    local delta_blocks=$((block_at_max - block_at_min))
    local delta_sec=$((max_ts - min_ts))
    bps=$(echo "scale=4; $delta_blocks / $delta_sec" | bc -l 2>/dev/null || echo "-")
  fi

  # Append new sample and rewrite file with only samples from last 60s
  {
    if [ -f "$RATE_FILE" ]; then
      while read -r ts block; do
        [ -z "$ts" ] && continue
        [ $((now - ts)) -le "$window_sec" ] && echo "$ts $block"
      done < "$RATE_FILE" 2>/dev/null
    fi
    echo "$now $latest_block"
  } | sort -n > "${RATE_FILE}.$$" && mv "${RATE_FILE}.$$" "$RATE_FILE"

  log "\nBlocks/sec: $bps"
}

# Time-to-sync estimate using block timestamps only (TS_delta / Date_delta logic).
# Stores (wall_clock_ts, block_timestamp) samples. TS_delta/Date_delta = chain-sec per real-sec.
# Time to sync = gap / (catch_up_rate - 1) where gap = now - block_ts.
print_time_to_sync() {
  local block_ts=$1
  local endpoint=${2:-default}
  local suffix=$(echo "$endpoint" | tr -c '[:alnum:]' '_')
  local SYNC_FILE="/tmp/geth_sync_eta_${suffix}.tmp"
  local now=$(date +%s)
  local eta="-"
  local window_sec=60
  local min_date="" max_date="" ts_at_min="" ts_at_max=""

  # Read existing (wall_clock, block_ts) samples, keep only from last 60s
  if [ -f "$SYNC_FILE" ]; then
    while read -r date_ts block_ts_val; do
      [ -z "$date_ts" ] || [ -z "$block_ts_val" ] && continue
      age=$((now - date_ts))
      [ "$age" -gt "$window_sec" ] && continue
      if [ -z "$min_date" ] || [ "$date_ts" -lt "$min_date" ]; then min_date=$date_ts; ts_at_min=$block_ts_val; fi
      if [ -z "$max_date" ] || [ "$date_ts" -gt "$max_date" ]; then max_date=$date_ts; ts_at_max=$block_ts_val; fi
    done < "$SYNC_FILE" 2>/dev/null
  fi

  # Include current sample
  if [ -z "$min_date" ] || [ "$now" -lt "$min_date" ]; then min_date=$now; ts_at_min=$block_ts; fi
  if [ -z "$max_date" ] || [ "$now" -gt "$max_date" ]; then max_date=$now; ts_at_max=$block_ts; fi

  # gap = chain-seconds behind head; catch_up_rate = chain-sec gained per real-sec
  local gap=$((now - block_ts))
  if [ "$gap" -le 0 ]; then
    eta="synced"
  elif [ -n "$min_date" ] && [ -n "$max_date" ] && [ "$max_date" -gt "$min_date" ]; then
    local date_delta=$((max_date - min_date))
    local ts_delta=$((ts_at_max - ts_at_min))
    if [ "$ts_delta" -gt 0 ] && [ "$date_delta" -gt 0 ]; then
      local catch_up=$(echo "scale=6; $ts_delta / $date_delta" | bc -l 2>/dev/null)
      if [ -n "$catch_up" ] && [ "$(echo "$catch_up > 1" | bc -l 2>/dev/null)" = "1" ]; then
        local net_rate=$(echo "scale=6; $catch_up - 1" | bc -l 2>/dev/null)
        local secs=$(echo "scale=0; $gap / $net_rate / 1" | bc -l 2>/dev/null)
        eta=$(format_seconds "$secs")
      else
        eta="∞"
      fi
    fi
  fi

  # Append new sample and rewrite file with only samples from last 60s
  {
    if [ -f "$SYNC_FILE" ]; then
      while read -r date_ts block_ts_val; do
        [ -z "$date_ts" ] && continue
        [ $((now - date_ts)) -le "$window_sec" ] && echo "$date_ts $block_ts_val"
      done < "$SYNC_FILE" 2>/dev/null
    fi
    echo "$now $block_ts"
  } | sort -n > "${SYNC_FILE}.$$" && mv "${SYNC_FILE}.$$" "$SYNC_FILE"

  log "\nTime to sync: $eta"
}

# Function to perform all checks
perform_checks() {
  # Chain ID
  timed_rpc '{"jsonrpc":"2.0","method":"eth_chainId","params": [],"id":1}'
  chain_id_elapsed="$RPC_ELAPSED"
  chain_id=$(echo "$RPC_RESULT" | jq -r ".result")
  chain_id_int="-"
  if [ -n "$chain_id" ] && [ "$chain_id" != "null" ]; then
    chain_id_int=$(safe_hex_to_dec "$chain_id")
  fi

  # Peer count
  timed_rpc '{"jsonrpc":"2.0","method":"net_peerCount","params": [],"id":1}'
  peers_elapsed="$RPC_ELAPSED"
  peers_hex=$(echo "$RPC_RESULT" | jq -r ".result")
  peers_int="-"
  if [ -n "$peers_hex" ] && [ "$peers_hex" != "null" ]; then
    peers_int=$(safe_hex_to_dec "$peers_hex")
  fi

  # Header table: Chain ID and Peers
  chain_id_ms=$(echo "scale=0; $chain_id_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "0")
  peers_ms=$(echo "scale=0; $peers_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "0")
  log "\n"
  printf "%-16s %-14s %-8s %-18s\n" "Chain ID (hex)" "Chain ID (int)" "Peers" "ReqTime(ms)"
  printf "%-16s %-14s %-8s %-18s\n" "----------------" "--------------" "--------" "------------------"
  printf "%-16s %-14s %-8s %-18s\n" "${chain_id:-[ERROR]}" "${chain_id_int}" "${peers_int}" "chain:${chain_id_ms} peers:${peers_ms}"

  # Sync status check
  timed_rpc '{"jsonrpc":"2.0","method":"eth_syncing","params": [],"id":1}'
  log "\n\nChecking sync status... (took ${RPC_ELAPSED}s)"
  if [ -z "$RPC_RESULT" ] || [ "$RPC_RESULT" == "null" ]; then
    echo "[ERROR] Failed to retrieve sync status"
  else
    echo "$RPC_RESULT"
  fi

  # Block checks: latest, safe, finalized, earliest
  declare -a BT_LABELS=("Latest" "Safe" "Finalized" "Earliest")
  declare -a BT_KEYS=("latest" "safe" "finalized" "earliest")
  declare -a BT_HEX BT_DEC BT_HASH BT_TIME BT_TS BT_MS
  for i in 0 1 2 3; do
    block_type="${BT_KEYS[$i]}"
    block_output=$(get_block_data "$block_type")
    block_elapsed=$(echo "$block_output" | head -1 | sed 's/ELAPSED://')
    block_data=$(echo "$block_output" | tail -n +2)
    req_ms=$(echo "scale=0; $block_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "0")

    block_number_hex=$(extract_field "$block_data" "number")
    if [ -z "$block_number_hex" ] || [ "$block_number_hex" == "null" ]; then
      BT_HEX[$i]="null"
      BT_DEC[$i]="0"
      BT_HASH[$i]="null"
      BT_TIME[$i]="1970-01-01T00:00:00Z"
      BT_TS[$i]="0"
    else
      block_number_int=$(safe_hex_to_dec "$block_number_hex")
      block_hash=$(extract_field "$block_data" "hash")
      timestamp_hex=$(extract_field "$block_data" "timestamp")
      timestamp=$(safe_hex_to_dec "$timestamp_hex")
      BT_HEX[$i]="$block_number_hex"
      BT_DEC[$i]="$block_number_int"
      BT_HASH[$i]="${block_hash:-null}"
      BT_TIME[$i]=$(timestamp_to_utc "$timestamp")
      BT_TS[$i]="$timestamp"
    fi
    BT_MS[$i]="$req_ms"
  done

  log "\n"
  printf "%-10s %-20s %-12s %-12s %-10s %-66s %-10s\n" "Row" "BlockTime" "Block Age" "Block(hex)" "Block(dec)" "BlockHash" "ReqTime(ms)"
  printf "%-10s %-20s %-12s %-12s %-10s %-66s %-10s\n" "----------" "--------------------" "------------" "------------" "----------" "------------------------------------------------------------------" "----------"
  for i in 0 1 2 3; do
    age=$(format_age "${BT_TS[$i]}")
    printf "%-10s %-20s %-12s %-12s %-10s %-66s %-10s\n" "${BT_LABELS[$i]}" "${BT_TIME[$i]}" "$age" "${BT_HEX[$i]}" "${BT_DEC[$i]}" "${BT_HASH[$i]}" "${BT_MS[$i]}"
  done

  print_blocks_per_sec "${BT_DEC[0]}" "$URL"
  if [ -n "${BT_TS[0]}" ] && [ "${BT_TS[0]}" != "0" ]; then
    print_time_to_sync "${BT_TS[0]}" "$URL"
  fi
}

# Function to check a specific block by number
check_block_by_number() {
  block_number=$1
  if [[ "$block_number" =~ ^[0-9]+$ ]]; then
    block_number=$(safe_dec_to_hex "$block_number")
  fi

  block_output=$(get_block_data "$block_number")
  block_elapsed=$(echo "$block_output" | head -1 | sed 's/ELAPSED://')
  block_data=$(echo "$block_output" | tail -n +2)
  log "\nChecking block number: $block_number... (took ${block_elapsed}s)"

  block_number_hex=$(extract_field "$block_data" "number")
  if [ -z "$block_number_hex" ]; then
    echo "[ERROR] Failed to retrieve block data for block number: $block_number"
  else
    block_number_int=$(safe_hex_to_dec "$block_number_hex")
    block_hash=$(extract_field "$block_data" "hash")
    timestamp_hex=$(extract_field "$block_data" "timestamp")
    timestamp=$(safe_hex_to_dec "$timestamp_hex")
    timestamp_utc=$(timestamp_to_utc "$timestamp")

    echo "Block Number (Hex): $block_number_hex"
    echo "Block Number (Int): $block_number_int"
    echo "Block Hash: $block_hash"
    echo "Block Timestamp: $timestamp_utc"
  fi
}

# Function to print the full block content
get_block() {
  block_number=$1
  if [[ "$block_number" =~ ^[0-9]+$ ]]; then
    block_number=$(safe_dec_to_hex "$block_number")
  fi

  block_output=$(get_block_data "$block_number")
  block_elapsed=$(echo "$block_output" | head -1 | sed 's/ELAPSED://')
  block_data=$(echo "$block_output" | tail -n +2)
  log "\nFetching full block content for block number: $block_number... (took ${block_elapsed}s)"
  if [ -z "$block_data" ]; then
    echo "[ERROR] Failed to retrieve block content for block number: $block_number"
  else
    echo "$block_data" | jq
  fi
}

# Function to get a transaction by hash
get_transaction_by_hash() {
  tx_hash=$1

  timed_rpc '{"jsonrpc":"2.0","method":"eth_getTransactionByHash","params": ["'"$tx_hash"'"],"id":1}'
  log "\nFetching transaction details for hash: $tx_hash... (took ${RPC_ELAPSED}s)"
  tx_data="$RPC_RESULT"

  if [ -z "$tx_data" ]; then
    echo "[ERROR] Failed to retrieve transaction details for hash: $tx_hash"
  else
    echo "$tx_data" | jq
  fi
}

# Function to get the balance of an account at a specific block height
get_balance() {
  account=$1
  block_height=${2:-"latest"} # Default to "latest" if no height is provided

  # Validate the Ethereum account address
  if [[ -z "$account" || ! "$account" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    echo "[ERROR] Invalid or missing account address. Ensure it is a valid Ethereum address (0x followed by 40 hex characters)."
    return 1
  fi

  if [[ "$block_height" =~ ^[0-9]+$ ]]; then
    block_height=$(safe_dec_to_hex "$block_height")
  fi

  timed_rpc '{"jsonrpc":"2.0","method":"eth_getBalance","params": ["'"$account"'", "'"$block_height"'"],"id":1}'
  log "\nFetching balance for account: $account at block height: $block_height... (took ${RPC_ELAPSED}s)"
  balance_data="$RPC_RESULT"

  balance_hex=$(echo "$balance_data" | jq -r ".result")
  if [ -z "$balance_hex" ] || [ "$balance_hex" == "null" ]; then
    echo "[ERROR] Failed to retrieve balance for account: $account"
    echo "Raw Response: $balance_data"
  else
    balance_wei=$(safe_hex_to_dec "$balance_hex")
    balance_eth=$(echo "scale=18; $balance_wei / 1000000000000000000" | bc -l)

    if (( $(echo "$balance_eth < 1" | bc -l) )); then
      printf "Balance (in ETH): %.18f\n" "$balance_eth"
    else
      printf "Balance (in ETH): %.4f\n" "$balance_eth"
    fi
    echo "Balance (in Wei): $balance_wei"
  fi
}

# New function to extract consensus layer PRYSM peers
get_prysm_peers() {
  local start end
  start=$(python3 -c 'import time; print(time.time())' 2>/dev/null || date +%s)
  peers=$(curl -s "$URL/eth/v1/node/peers" | jq -r '.data[].enr')
  end=$(python3 -c 'import time; print(time.time())' 2>/dev/null || date +%s)
  RPC_ELAPSED=$(echo "$end - $start" | bc -l 2>/dev/null || echo "0")
  log "\nFetching PRYSM peers from consensus layer... (took ${RPC_ELAPSED}s)"
  if [ -z "$peers" ]; then
    echo "[ERROR] No peers found or error retrieving peers."
  else
    echo "PRYSM Peers:"
    echo "$peers"
  fi
}

# Monitor loop: run general_check every second
monitor_loop() {
  while true; do
    clear 2>/dev/null || true
    perform_checks
    sleep 1
  done
}

# Main logic
if [ -z "$2" ]; then
  # No command provided: default to general_check
  perform_checks
elif [ "$2" == "general_check" ]; then
  perform_checks
elif [ "$2" == "monitor" ]; then
  monitor_loop
elif [ "$2" == "block_summary" ]; then
  if [ -z "$3" ]; then
    log "Error: Block number not provided."
    usage
  fi
  check_block_by_number "$3"
elif [ "$2" == "get_block" ]; then
  if [ -z "$3" ]; then
    log "Error: Block number not provided."
    usage
  fi
  get_block "$3"
elif [ "$2" == "tx" ]; then
  if [ -z "$3" ]; then
    log "Error: Transaction hash not provided."
    usage
  fi
  get_transaction_by_hash "$3"
elif [ "$2" == "get_balance" ]; then
  if [ -z "$3" ]; then
    log "Error: Account address not provided."
    usage
  fi
  get_balance "$3" "$4"
elif [ "$2" == "prysm_peers" ]; then
  get_prysm_peers
else
  log "Error: Invalid command."
  usage
fi
