#!/bin/bash

# Script: basic_geth_checks_with_timestamps.sh
# This script performs basic checks on a Geth node by calling various JSON-RPC methods.
# It checks peer count, syncing status, current block number, finalized block, and earliest block along with their timestamps.

# Function for verbose logging
log() {
  echo -e "$1"
}

# Function to display usage information
usage() {
  cat << EOF

Usage: $0 <full_url_or_port> <command> [block_number]

Commands:
  general_check                  Perform all basic Geth checks (peer count, sync status, blocks).
  block_summary <block_number>   Fetch details of a specific block by its number.
  get_block <block_number>       Print the full block content for the specified block number.
  get_balance <account> [block_height] Fetch the balance of an account at a specific block height (default: latest).
  tx <tx_hash>                   Fetch details of a specific transaction by its hash.

Examples:
  $0 8545 general_check
  $0 127.0.0.1:8545 block_summary <block_number>
  $0 127.0.0.1:8545 get_block <block_number>
  $0 127.0.0.1:8545 tx <tx_hash>
  $0 127.0.0.1:8545 <command <command_params>>
EOF
  exit 1
}

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

# Function to fetch and parse block data
get_block_data() {
  block_number=$1
  curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params": ["'$block_number'", true],"id":1}' $URL
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

# Function to perform all checks
perform_checks() {
  # Peer count check
  log "\nChecking peer count..."
  curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"net_peerCount","params": [],"id":1}' $URL || {
    echo "[ERROR] Failed to retrieve peer count"
  }

  # Sync status check
  log "\n\nChecking sync status..."
  curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"eth_syncing","params": [],"id":1}' $URL || {
    echo "[ERROR] Failed to retrieve sync status"
  }

  # Block checks
  for block_type in "latest" "finalized" "earliest"; do
    log "\n\nChecking $block_type block..."
    block_data=$(get_block_data "$block_type")

    block_number_hex=$(extract_field "$block_data" "number")
    if [ -z "$block_number_hex" ]; then
      echo "[ERROR] Failed to retrieve block number for $block_type block"
    else
      block_number_int=$(safe_hex_to_dec "$block_number_hex")
      block_hash=$(extract_field "$block_data" "hash")
      timestamp_hex=$(extract_field "$block_data" "timestamp")
      timestamp=$(safe_hex_to_dec "$timestamp_hex")
      timestamp_utc=$(timestamp_to_utc "$timestamp")

      echo "${block_type^} Block Number (Hex): $block_number_hex"
      echo "${block_type^} Block Number (Int): $block_number_int"
      echo "${block_type^} Block Hash: $block_hash"
      echo "${block_type^} Block Timestamp: $timestamp_utc"
    fi
  done
}

# Function to check a specific block by number
check_block_by_number() {
  block_number=$1
  if [[ "$block_number" =~ ^[0-9]+$ ]]; then
    block_number=$(safe_dec_to_hex "$block_number")
  fi

  log "\nChecking block number: $block_number..."
  block_data=$(get_block_data "$block_number")

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

  log "\nFetching full block content for block number: $block_number..."
  block_data=$(get_block_data "$block_number")
  if [ -z "$block_data" ]; then
    echo "[ERROR] Failed to retrieve block content for block number: $block_number"
  else
    echo "$block_data" | jq
  fi
}

# Function to get a transaction by hash
get_transaction_by_hash() {
  tx_hash=$1

  log "\nFetching transaction details for hash: $tx_hash..."
  tx_data=$(curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"eth_getTransactionByHash","params": ["'$tx_hash'"],"id":1}' $URL)

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

  if [[ "$block_height" =~ ^[0-9]+$ ]]; then
    block_height=$(safe_dec_to_hex "$block_height")
  fi

  log "
Fetching balance for account: $account at block height: $block_height..."
  balance_data=$(curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"eth_getBalance","params": ["'$account'", "'$block_height'"],"id":1}' $URL)

  balance_hex=$(echo "$balance_data" | jq -r ".result")
  if [ -z "$balance_hex" ] || [ "$balance_hex" == "null" ]; then
    echo "[ERROR] Failed to retrieve balance for account: $account"
    echo "Raw Response: $balance_data"
  else
    balance_wei=$(safe_hex_to_dec "$balance_hex")
    echo "Balance (in Wei): $balance_wei"
  fi
}
safe_dec_to_hex() {
  dec_value=$1
  printf "0x%x" "$dec_value"
}



# Main logic
if [ -z "$2" ]; then
  log "Error: Command not provided."
  usage
elif [ "$2" == "general_check" ]; then
  perform_checks
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
else
  log "Error: Invalid command."
  usage
fi
