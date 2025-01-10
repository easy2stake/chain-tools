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
  log "Usage: $0 <full_url_or_port>"
  exit 1
}

# Check if the argument is provided
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

# Function to convert Unix timestamp to UTC format
timestamp_to_utc() {
  unix_timestamp=$1
  date -u -d "@$unix_timestamp" +"%Y-%m-%dT%H:%M:%SZ"
}

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
