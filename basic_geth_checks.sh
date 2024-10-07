#!/bin/bash

# Script: basic_geth_checks.sh
# This script performs basic checks on a Geth node by calling various JSON-RPC methods.
# It checks peer count, syncing status, and current block number.

# Function to display usage information
usage() {
  echo "Usage: $0 <port>"
  exit 1
}

# Check if the port argument is provided
if [ -z "$1" ]; then
  echo "Error: Port number not provided."
  usage
fi

# Assigning the port to a variable
PORT=$1
URL="127.0.0.1:$PORT"

# Function for verbose logging
log() {
  echo -e "$1"
}

# Log the start of the process
log "Starting Geth checks on URL: $URL"

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

# Current block number check
log "\n\nChecking current block number..."
block_data=$(curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params": ["latest", true],"id":1}' $URL)

block_number_hex=$( echo "$block_data" | jq -r '.result.number')

if [ -z "$block_number_hex" ]; then
  echo "[ERROR] Failed to retrieve block number"
else
  block_number_int=$(echo "$block_number_hex" | xargs printf "%d\n")
  block_hash=$(echo $block_data  | jq -r '.result.hash')
  echo "Current Block Number (Hex): $block_number_hex"
  echo "Current Block Number (Int): $block_number_int"
  log "Current Block Hash: $block_hash"
fi


# Current block number check
log "\nChecking earliest block number..."
block_data=$(curl -s -X POST -H "Content-Type: application/json" -m 2 -d '{"jsonrpc":"2.0","method":"eth_getBlockByNumber","params": ["earliest", true],"id":1}' $URL)

block_number_hex=$(echo "$block_data" | jq -r '.result.number')
block_hash=$(echo "$block_data" | jq -r '.result.hash')

if [ -z "$block_number_hex" ]; then
  echo "[ERROR] Failed to retrieve block number"
else
  block_number_int=$(echo "$block_number_hex" | xargs printf "%d\n")
  echo "Current Block Number (Hex): $block_number_hex"
  echo "Current Block Number (Int): $block_number_int"
  echo "Block Hash: $block_hash"
fi

