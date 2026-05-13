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

# Execute HTTP GET call, sets HTTP_RESULT and HTTP_ELAPSED globals.
timed_http_get() {
  local endpoint=$1
  local start end
  start=$(python3 -c 'import time; print(time.time())' 2>/dev/null || date +%s)
  HTTP_RESULT=$(curl -s -m 2 "$URL$endpoint")
  end=$(python3 -c 'import time; print(time.time())' 2>/dev/null || date +%s)
  HTTP_ELAPSED=$(echo "$end - $start" | bc -l 2>/dev/null || echo "0")
}

# Function to display usage information
usage() {
  cat << EOF

Usage: $0 <full_url_or_port> <command> [command_options]

Commands:
  general_check                  Perform all basic Geth checks (chain ID, peer count, sync status, blocks).
  monitor                        Same as general_check but loops every second (Ctrl+C to stop).
  op                             Perform OP node checks (peers, sync status, L1/L2 heads where available).
  op_monitor                     Same as op but loops every second (Ctrl+C to stop).
  tendermint                     Perform Tendermint/CometBFT checks (peers, catching_up, latest/earliest blocks).
  tendermint_monitor             Same as tendermint but loops every second (Ctrl+C to stop).
  aptos                          Aptos fullnode REST checks (GET /v1: ledger time, versions, block heights, metadata).
  aptos_monitor                  Same as aptos but loops every second (Ctrl+C to stop).
  block_summary <block_number>   Fetch details of a specific block by its number.
  get_block <block_number>       Print the full block content for the specified block number.
  get_balance <account> [block_height] Fetch the balance of an account at a specific block height (default: latest).
  tx <tx_hash>                   Fetch details of a specific transaction by its hash.
  prysm_peers                  Extract consensus layer PRYSM peers.
Examples:
  $0 8545 general_check
  $0 8545 monitor
  $0 9545 op
  $0 9545 op_monitor
  $0 26657 tendermint
  $0 26657 tendermint_monitor
  $0 8080 aptos
  $0 http://127.0.0.1:8080 aptos_monitor
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

# Substrate (e.g. Bittensor): best block header — result.number is the latest block (eth_getBlockByNumber often unavailable)
get_substrate_best_header() {
  timed_rpc '{"jsonrpc":"2.0","method":"chain_getHeader","params":[],"id":1}'
  echo "ELAPSED:${RPC_ELAPSED}"
  echo "$RPC_RESULT"
}

# Substrate: block hash for a height — params are [block_number] as decimal (e.g. 2134948)
# Uses timed_rpc; prints hash hex to stdout; leaves RPC_ELAPSED for the hash request.
get_substrate_block_hash_by_number() {
  local block_dec=$1
  timed_rpc '{"jsonrpc":"2.0","method":"chain_getBlockHash","params":['"${block_dec}"'],"id":1}'
  echo "$RPC_RESULT" | jq -r '.result // empty'
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
  local unix_timestamp=$1
  if date -u -d "@$unix_timestamp" +"%Y-%m-%dT%H:%M:%SZ" >/dev/null 2>&1; then
    date -u -d "@$unix_timestamp" +"%Y-%m-%dT%H:%M:%SZ"
  elif command -v gdate >/dev/null 2>&1; then
    gdate -u -d "@$unix_timestamp" +"%Y-%m-%dT%H:%M:%SZ"
  else
    date -u -r "$unix_timestamp" +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "-"
  fi
}

# Convert RFC3339 timestamp to unix epoch seconds (returns 0 on parse failure).
rfc3339_to_epoch() {
  local ts=$1
  python3 - "$ts" <<'PY'
import sys
from datetime import datetime, timezone

raw = (sys.argv[1] or "").strip()
if not raw:
    print(0)
    raise SystemExit(0)

try:
    # Handle Z-form and up to nanoseconds by trimming to microseconds.
    v = raw.replace("Z", "+00:00")
    if "." in v:
        base, rest = v.split(".", 1)
        if "+" in rest:
            frac, tz = rest.split("+", 1)
            frac = (frac + "000000")[:6]
            v = f"{base}.{frac}+{tz}"
        elif "-" in rest:
            frac, tz = rest.split("-", 1)
            frac = (frac + "000000")[:6]
            v = f"{base}.{frac}-{tz}"
        else:
            frac = (rest + "000000")[:6]
            v = f"{base}.{frac}"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    print(int(dt.timestamp()))
except Exception:
    print(0)
PY
}

# Aptos ledger_timestamp is microseconds since Unix epoch (REST GET /v1).
aptos_ledger_micro_to_epoch() {
  local micro=$1
  python3 - "$micro" <<'PY'
import sys
raw = (sys.argv[1] or "").strip().strip('"')
if not raw or not raw.isdigit():
    print(0)
else:
    print(int(raw) // 1_000_000)
PY
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

# Check if value is a non-negative integer.
is_uint() {
  [[ "$1" =~ ^[0-9]+$ ]]
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

# Resolve chain identity for display: EVM uses eth_chainId (hex + decimal);
# Substrate-style nodes (e.g. Bittensor) often omit eth_chainId — use system_chain name instead.
# Sets CHAIN_RPC_MODE to evm or substrate (drives block RPC in perform_checks).
resolve_chain_identity() {
  CHAIN_RPC_MODE=evm
  timed_rpc '{"jsonrpc":"2.0","method":"eth_chainId","params": [],"id":1}'
  chain_id_elapsed="$RPC_ELAPSED"
  chain_id=$(echo "$RPC_RESULT" | jq -r ".result // empty")
  chain_id_int="-"
  if [ -n "$chain_id" ] && [ "$chain_id" != "null" ]; then
    chain_id_int=$(safe_hex_to_dec "$chain_id")
    return 0
  fi
  chain_id=""
  timed_rpc '{"jsonrpc":"2.0","method":"system_chain","params":[],"id":1}'
  local sc_elapsed="$RPC_ELAPSED"
  local sc_name
  sc_name=$(echo "$RPC_RESULT" | jq -r 'if (.result | type) == "string" then .result else empty end')
  if [ -n "$sc_name" ]; then
    chain_id="$sc_name"
    chain_id_int="$sc_name"
    chain_id_elapsed=$(echo "$chain_id_elapsed + $sc_elapsed" | bc -l 2>/dev/null || echo "$chain_id_elapsed")
    CHAIN_RPC_MODE=substrate
  else
    chain_id=""
    chain_id_int="-"
  fi
}

# Function to perform all checks
perform_checks() {
  # Chain ID (EVM hex/dec, or Substrate chain name)
  resolve_chain_identity

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
    if [ "${CHAIN_RPC_MODE:-evm}" = "substrate" ]; then
      if [ "$i" -eq 0 ]; then
        block_output=$(get_substrate_best_header)
        block_elapsed=$(echo "$block_output" | head -1 | sed 's/ELAPSED://')
        block_data=$(echo "$block_output" | tail -n +2)
        req_ms=$(echo "scale=0; $block_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "0")
        block_number_hex=$(extract_field "$block_data" "number")
        if [ -z "$block_number_hex" ] || [ "$block_number_hex" = "null" ]; then
          BT_HEX[0]="null"
          BT_DEC[0]="0"
          BT_HASH[0]="null"
          BT_TIME[0]="-"
          BT_TS[0]="0"
        else
          block_number_int=$(safe_hex_to_dec "$block_number_hex")
          BT_HEX[0]="$block_number_hex"
          BT_DEC[0]="$block_number_int"
          block_hash=$(get_substrate_block_hash_by_number "$block_number_int")
          hash_elapsed="$RPC_ELAPSED"
          if [ -n "$block_hash" ] && [ "$block_hash" != "null" ]; then
            BT_HASH[0]="$block_hash"
          else
            BT_HASH[0]="-"
          fi
          req_ms=$(echo "scale=0; ($block_elapsed + $hash_elapsed) * 1000 / 1" | bc -l 2>/dev/null || echo "$req_ms")
          BT_TIME[0]="-"
          BT_TS[0]="0"
        fi
        BT_MS[0]="$req_ms"
      else
        BT_HEX[$i]="-"
        BT_DEC[$i]="-"
        BT_HASH[$i]="-"
        BT_TIME[$i]="-"
        BT_TS[$i]="0"
        BT_MS[$i]="-"
      fi
      continue
    fi

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

# OP-node specific checks:
# - peers from opp2p_peerStats (fallback opp2p_peers)
# - sync/block status from optimism_syncStatus
# - rollup chain ids from optimism_rollupConfig (when available)
perform_op_node_checks() {
  local rollup_elapsed version_elapsed peers_elapsed sync_elapsed
  local rollup_data version_data peers_data sync_data
  local l1_chain_id="-" l2_chain_id="-" op_version="-"
  local peers_connected="-"
  local req_ms_rollup="-" req_ms_version="-" req_ms_peers="-" req_ms_sync="-"

  # Rollup config: derive L1/L2 chain IDs where available.
  timed_rpc '{"jsonrpc":"2.0","method":"optimism_rollupConfig","params":[],"id":1}'
  rollup_elapsed="$RPC_ELAPSED"
  rollup_data="$RPC_RESULT"
  if [ -n "$rollup_data" ] && [ "$rollup_data" != "null" ]; then
    l1_chain_id=$(echo "$rollup_data" | jq -r '.result.l1_chain_id // "-"')
    l2_chain_id=$(echo "$rollup_data" | jq -r '.result.l2_chain_id // "-"')
  fi
  req_ms_rollup=$(echo "scale=0; $rollup_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  # Version is useful context for operators.
  timed_rpc '{"jsonrpc":"2.0","method":"optimism_version","params":[],"id":1}'
  version_elapsed="$RPC_ELAPSED"
  version_data="$RPC_RESULT"
  if [ -n "$version_data" ] && [ "$version_data" != "null" ]; then
    op_version=$(echo "$version_data" | jq -r '.result // "-"')
  fi
  req_ms_version=$(echo "scale=0; $version_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  # Prefer aggregate peer stats first.
  timed_rpc '{"jsonrpc":"2.0","method":"opp2p_peerStats","params":[],"id":1}'
  peers_elapsed="$RPC_ELAPSED"
  peers_data="$RPC_RESULT"
  peers_connected=$(echo "$peers_data" | jq -r '.result.connected // "-"')
  if ! is_uint "$peers_connected"; then
    # Fallback for older/newer variants that expose totalConnected in opp2p_peers.
    timed_rpc '{"jsonrpc":"2.0","method":"opp2p_peers","params":[true],"id":1}'
    peers_elapsed=$(echo "$peers_elapsed + $RPC_ELAPSED" | bc -l 2>/dev/null || echo "$peers_elapsed")
    peers_data="$RPC_RESULT"
    peers_connected=$(echo "$peers_data" | jq -r '.result.totalConnected // "-"')
  fi
  req_ms_peers=$(echo "scale=0; $peers_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  log "\n"
  printf "%-12s %-12s %-22s %-8s %-34s\n" "L1 Chain ID" "L2 Chain ID" "Version" "Peers" "ReqTime(ms)"
  printf "%-12s %-12s %-22s %-8s %-34s\n" "------------" "------------" "----------------------" "--------" "----------------------------------"
  printf "%-12s %-12s %-22s %-8s %-34s\n" "$l1_chain_id" "$l2_chain_id" "$op_version" "$peers_connected" "rollup:${req_ms_rollup} version:${req_ms_version} peers:${req_ms_peers}"

  # Core sync status for op-node.
  timed_rpc '{"jsonrpc":"2.0","method":"optimism_syncStatus","params":[],"id":1}'
  sync_elapsed="$RPC_ELAPSED"
  sync_data="$RPC_RESULT"
  req_ms_sync=$(echo "scale=0; $sync_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  log "\n\nChecking op-node sync status... (took ${sync_elapsed}s)"
  if [ -z "$sync_data" ] || [ "$sync_data" = "null" ]; then
    echo "[ERROR] Failed to retrieve optimism_syncStatus"
    return
  fi

  local sync_ok
  sync_ok=$(echo "$sync_data" | jq -r 'if (.result | type) == "object" then "1" else "0" end' 2>/dev/null || echo "0")
  if [ "$sync_ok" != "1" ]; then
    echo "[ERROR] Invalid optimism_syncStatus response"
    echo "$sync_data"
    return
  fi

  declare -a OP_LABELS=("CurrentL1" "HeadL1" "SafeL1" "FinalizedL1" "UnsafeL2" "SafeL2" "FinalizedL2" "EngineTarget")
  declare -a OP_KEYS=("current_l1" "head_l1" "safe_l1" "finalized_l1" "unsafe_l2" "safe_l2" "finalized_l2" "engine_sync_target")

  log "\n"
  printf "%-12s %-20s %-12s %-12s %-66s\n" "Row" "BlockTime" "Block Age" "Block(dec)" "BlockHash"
  printf "%-12s %-20s %-12s %-12s %-66s\n" "------------" "--------------------" "------------" "------------" "------------------------------------------------------------------"

  local i key label number hash ts block_time age
  for i in "${!OP_KEYS[@]}"; do
    key="${OP_KEYS[$i]}"
    label="${OP_LABELS[$i]}"
    number=$(echo "$sync_data" | jq -r ".result.${key}.number // \"-\"")
    hash=$(echo "$sync_data" | jq -r ".result.${key}.hash // \"-\"")
    ts=$(echo "$sync_data" | jq -r ".result.${key}.timestamp // \"0\"")

    if is_uint "$ts" && [ "$ts" -gt 0 ]; then
      block_time=$(timestamp_to_utc "$ts")
      age=$(format_age "$ts")
    else
      block_time="-"
      age="-"
    fi

    printf "%-12s %-20s %-12s %-12s %-66s\n" "$label" "$block_time" "$age" "$number" "$hash"
  done

  local head_l1_num current_l1_num unsafe_l2_num safe_l2_num
  head_l1_num=$(echo "$sync_data" | jq -r '.result.head_l1.number // "-"')
  current_l1_num=$(echo "$sync_data" | jq -r '.result.current_l1.number // "-"')
  unsafe_l2_num=$(echo "$sync_data" | jq -r '.result.unsafe_l2.number // "-"')
  safe_l2_num=$(echo "$sync_data" | jq -r '.result.safe_l2.number // "-"')

  local l1_gap="-" l2_gap="-"
  if is_uint "$head_l1_num" && is_uint "$current_l1_num" && [ "$head_l1_num" -ge "$current_l1_num" ]; then
    l1_gap=$((head_l1_num - current_l1_num))
  fi
  if is_uint "$unsafe_l2_num" && is_uint "$safe_l2_num" && [ "$unsafe_l2_num" -ge "$safe_l2_num" ]; then
    l2_gap=$((unsafe_l2_num - safe_l2_num))
  fi

  log "\nSync gaps: l1_head-current_l1=${l1_gap} blocks, l2_unsafe-safe=${l2_gap} blocks (optimism_syncStatus req=${req_ms_sync}ms)"
}

# Tendermint/CometBFT checks:
# - /status for sync_info and node metadata
# - /net_info for peer count and listener state
perform_tendermint_checks() {
  local status_data net_data status_elapsed net_elapsed
  local req_ms_status req_ms_net
  local network moniker version catching_up
  local latest_height earliest_height latest_hash earliest_hash latest_time_raw earliest_time_raw
  local latest_epoch earliest_epoch latest_time_utc earliest_time_utc
  local peers listening

  timed_http_get "/status"
  status_data="$HTTP_RESULT"
  status_elapsed="$HTTP_ELAPSED"
  req_ms_status=$(echo "scale=0; $status_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  if [ -z "$status_data" ] || [ "$status_data" = "null" ]; then
    echo "[ERROR] Failed to retrieve Tendermint status endpoint: $URL/status"
    return
  fi

  timed_http_get "/net_info"
  net_data="$HTTP_RESULT"
  net_elapsed="$HTTP_ELAPSED"
  req_ms_net=$(echo "scale=0; $net_elapsed * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  network=$(echo "$status_data" | jq -r '.result.node_info.network // "-"')
  moniker=$(echo "$status_data" | jq -r '.result.node_info.moniker // "-"')
  version=$(echo "$status_data" | jq -r '.result.node_info.version // "-"')
  catching_up=$(echo "$status_data" | jq -r '.result.sync_info.catching_up // "-"')

  peers=$(echo "$net_data" | jq -r '.result.n_peers // "-"')
  listening=$(echo "$net_data" | jq -r '.result.listening // "-"')

  latest_height=$(echo "$status_data" | jq -r '.result.sync_info.latest_block_height // "-"')
  earliest_height=$(echo "$status_data" | jq -r '.result.sync_info.earliest_block_height // "-"')
  latest_hash=$(echo "$status_data" | jq -r '.result.sync_info.latest_block_hash // "-"')
  earliest_hash=$(echo "$status_data" | jq -r '.result.sync_info.earliest_block_hash // "-"')
  latest_time_raw=$(echo "$status_data" | jq -r '.result.sync_info.latest_block_time // ""')
  earliest_time_raw=$(echo "$status_data" | jq -r '.result.sync_info.earliest_block_time // ""')
  latest_epoch=$(rfc3339_to_epoch "$latest_time_raw")
  earliest_epoch=$(rfc3339_to_epoch "$earliest_time_raw")

  if is_uint "$latest_epoch" && [ "$latest_epoch" -gt 0 ]; then
    latest_time_utc=$(timestamp_to_utc "$latest_epoch")
  else
    latest_time_utc="-"
  fi
  if is_uint "$earliest_epoch" && [ "$earliest_epoch" -gt 0 ]; then
    earliest_time_utc=$(timestamp_to_utc "$earliest_epoch")
  else
    earliest_time_utc="-"
  fi

  log "\n"
  printf "%-24s %-22s %-10s %-8s %-10s %-28s\n" "Network" "Moniker" "Version" "Peers" "Listening" "ReqTime(ms)"
  printf "%-24s %-22s %-10s %-8s %-10s %-28s\n" "------------------------" "----------------------" "----------" "--------" "----------" "----------------------------"
  printf "%-24s %-22s %-10s %-8s %-10s %-28s\n" "$network" "$moniker" "$version" "$peers" "$listening" "status:${req_ms_status} net:${req_ms_net}"

  log "\nSync status: catching_up=${catching_up}"

  log "\n"
  printf "%-10s %-20s %-12s %-12s %-66s\n" "Row" "BlockTime" "Block Age" "Height" "BlockHash"
  printf "%-10s %-20s %-12s %-12s %-66s\n" "----------" "--------------------" "------------" "------------" "------------------------------------------------------------------"
  printf "%-10s %-20s %-12s %-12s %-66s\n" "Latest" "$latest_time_utc" "$(format_age "$latest_epoch")" "$latest_height" "$latest_hash"
  printf "%-10s %-20s %-12s %-12s %-66s\n" "Earliest" "$earliest_time_utc" "$(format_age "$earliest_epoch")" "$earliest_height" "$earliest_hash"

  if is_uint "$latest_height"; then
    print_blocks_per_sec "$latest_height" "$URL"
  fi
  if is_uint "$latest_epoch" && [ "$latest_epoch" -gt 0 ]; then
    print_time_to_sync "$latest_epoch" "$URL"
  fi
}

# Aptos fullnode REST — GET /v1 returns chain_id, epoch, ledger versions, ledger_timestamp
# (microseconds since Unix epoch), block heights, node_role, git_hash, etc.
perform_aptos_checks() {
  local data req_ms
  local chain_id epoch ledger_ver oldest_ledger ledger_ts_micro ledger_epoch
  local node_role oldest_bh block_height git_hash enc_key
  local ledger_ts_utc ledger_span bh_span

  timed_http_get "/v1"
  data="$HTTP_RESULT"
  req_ms=$(echo "scale=0; $HTTP_ELAPSED * 1000 / 1" | bc -l 2>/dev/null || echo "-")

  if [ -z "$data" ] || ! echo "$data" | jq -e . >/dev/null 2>&1; then
    echo "[ERROR] Failed to retrieve Aptos /v1 endpoint: $URL/v1"
    return
  fi

  chain_id=$(echo "$data" | jq -r '.chain_id // "-"')
  epoch=$(echo "$data" | jq -r '(.epoch // "-") | tostring')
  ledger_ver=$(echo "$data" | jq -r '(.ledger_version // "-") | tostring')
  oldest_ledger=$(echo "$data" | jq -r '(.oldest_ledger_version // "-") | tostring')
  ledger_ts_micro=$(echo "$data" | jq -r 'if (.ledger_timestamp | type) == "null" or .ledger_timestamp == null then empty else (.ledger_timestamp | tostring) end')
  node_role=$(echo "$data" | jq -r '.node_role // "-"')
  oldest_bh=$(echo "$data" | jq -r '(.oldest_block_height // "-") | tostring')
  block_height=$(echo "$data" | jq -r '(.block_height // "-") | tostring')
  git_hash=$(echo "$data" | jq -r '.git_hash // "-"')
  enc_key=$(echo "$data" | jq -r 'if .encryption_key == null then "null" else (.encryption_key | tostring) end')

  ledger_epoch=$(aptos_ledger_micro_to_epoch "$ledger_ts_micro")

  if is_uint "$ledger_epoch" && [ "$ledger_epoch" -gt 0 ]; then
    ledger_ts_utc=$(timestamp_to_utc "$ledger_epoch")
  else
    ledger_ts_utc="-"
  fi

  ledger_span="-"
  if is_uint "$ledger_ver" && is_uint "$oldest_ledger" && [ "$ledger_ver" -ge "$oldest_ledger" ]; then
    ledger_span=$((ledger_ver - oldest_ledger))
  fi

  bh_span="-"
  if is_uint "$block_height" && is_uint "$oldest_bh" && [ "$block_height" -ge "$oldest_bh" ]; then
    bh_span=$((block_height - oldest_bh))
  fi

  log "\n"
  printf "%-12s %-16s %-24s %-44s %-14s\n" "Chain ID" "Epoch" "Node role" "Git hash" "ReqTime(ms)"
  printf "%-12s %-16s %-24s %-44s %-14s\n" "------------" "----------------" "------------------------" "--------------------------------------------" "--------------"
  printf "%-12s %-16s %-24s %-44s %-14s\n" "$chain_id" "$epoch" "$node_role" "$git_hash" "$req_ms"

  log "\nLedger head (ledger_timestamp drives chain time / age below):"
  printf "%-14s %-22s %-16s %-22s %-28s\n" "Row" "Ledger time (UTC)" "Ledger age" "Ledger version" "(micro timestamp)"
  printf "%-14s %-22s %-16s %-22s %-28s\n" "--------------" "----------------------" "----------------" "----------------------" "----------------------------"
  printf "%-14s %-22s %-16s %-22s %-28s\n" "Head" "$ledger_ts_utc" "$(format_age "$ledger_epoch")" "$ledger_ver" "${ledger_ts_micro:--}"

  log "\nBlock heights (pruning window on node):"
  printf "%-14s %-22s %-22s %-14s\n" "Row" "Block height" "Oldest block height" "Span"
  printf "%-14s %-22s %-22s %-14s\n" "--------------" "----------------------" "----------------------" "--------------"
  printf "%-14s %-22s %-22s %-14s\n" "Range" "$block_height" "$oldest_bh" "$bh_span"

  log "\nLedger versions & extras:"
  echo "  oldest_ledger_version: $oldest_ledger"
  echo "  ledger_version_span (head - oldest): $ledger_span"
  echo "  encryption_key: $enc_key"

  if is_uint "$block_height"; then
    print_blocks_per_sec "$block_height" "$URL"
  fi
  if is_uint "$ledger_epoch" && [ "$ledger_epoch" -gt 0 ]; then
    print_time_to_sync "$ledger_epoch" "$URL"
  fi

  log "\nRaw /v1 JSON:"
  echo "$data" | jq .
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

# Monitor loop: run general_check every second (buffer output to reduce flicker)
monitor_loop() {
  while true; do
    output=$(perform_checks 2>&1)
    clear 2>/dev/null || true
    echo "$output"
    sleep 1
  done
}

# Monitor loop for OP-node checks.
monitor_op_node_loop() {
  while true; do
    output=$(perform_op_node_checks 2>&1)
    clear 2>/dev/null || true
    echo "$output"
    sleep 1
  done
}

# Monitor loop for Tendermint checks.
monitor_tendermint_loop() {
  while true; do
    output=$(perform_tendermint_checks 2>&1)
    clear 2>/dev/null || true
    echo "$output"
    sleep 1
  done
}

monitor_aptos_loop() {
  while true; do
    output=$(perform_aptos_checks 2>&1)
    clear 2>/dev/null || true
    echo "$output"
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
elif [ "$2" == "op" ]; then
  perform_op_node_checks
elif [ "$2" == "op_monitor" ]; then
  monitor_op_node_loop
elif [ "$2" == "tendermint" ]; then
  perform_tendermint_checks
elif [ "$2" == "tendermint_monitor" ]; then
  monitor_tendermint_loop
elif [ "$2" == "aptos" ]; then
  perform_aptos_checks
elif [ "$2" == "aptos_monitor" ]; then
  monitor_aptos_loop
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
