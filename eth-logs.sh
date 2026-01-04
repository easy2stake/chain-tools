#!/bin/bash

URL="http://127.0.0.1:18585"
VERBOSE=false

# Parse for verbose flag
for arg in "$@"; do
  if [ "$arg" == "-v" ] || [ "$arg" == "--verbose" ]; then
    VERBOSE=true
  fi
done

# Remove verbose flag from arguments
ARGS=()
for arg in "$@"; do
  if [ "$arg" != "-v" ] && [ "$arg" != "--verbose" ]; then
    ARGS+=("$arg")
  fi
done

# Function to query a single block
query_block() {
  local BLOCK=$1
  local TX_HASH=$2
  
  # 1. eth_getLogs
  if [ "$VERBOSE" == "true" ]; then
    echo "--- 1. eth_getLogs (Block: $BLOCK) ---"
    RESPONSE=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
      \"jsonrpc\": \"2.0\",
      \"method\": \"eth_getLogs\",
      \"params\": [
        {
          \"fromBlock\": \"$BLOCK\",
          \"toBlock\": \"$BLOCK\"
        }
      ],
      \"id\": 1
    }" $URL)
    echo "$RESPONSE" | jq . | tee -a getLogs.tmp
    echo ""
  else
    RESPONSE=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
      \"jsonrpc\": \"2.0\",
      \"method\": \"eth_getLogs\",
      \"params\": [
        {
          \"fromBlock\": \"$BLOCK\",
          \"toBlock\": \"$BLOCK\"
        }
      ],
      \"id\": 1
    }" $URL)
    echo "$RESPONSE" >> getLogs.tmp
    
    # Check if request was successful
    if echo "$RESPONSE" | jq -e '.result' > /dev/null 2>&1; then
      LOG_COUNT=$(echo "$RESPONSE" | jq -r '.result | length')
      echo "✓ eth_getLogs: $LOG_COUNT logs found"
    else
      echo "✗ eth_getLogs: Failed or error"
    fi
  fi

  # 2. eth_getBlockReceipts
  if [ "$VERBOSE" == "true" ]; then
    echo "--- 2. eth_getBlockReceipts (Block: $BLOCK) ---"
    RESPONSE=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
      \"jsonrpc\": \"2.0\",
      \"method\": \"eth_getBlockReceipts\",
      \"params\": [\"$BLOCK\"],
      \"id\": 1
    }" $URL)
    echo "$RESPONSE" | jq . | tee -a getBlockReceipts.tmp
    echo ""
  else
    RESPONSE=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
      \"jsonrpc\": \"2.0\",
      \"method\": \"eth_getBlockReceipts\",
      \"params\": [\"$BLOCK\"],
      \"id\": 1
    }" $URL)
    echo "$RESPONSE" >> getBlockReceipts.tmp
    
    # Check if request was successful
    if echo "$RESPONSE" | jq -e '.result' > /dev/null 2>&1; then
      RECEIPT_COUNT=$(echo "$RESPONSE" | jq -r '.result | length')
      echo "✓ eth_getBlockReceipts: $RECEIPT_COUNT receipts found"
    else
      echo "✗ eth_getBlockReceipts: Failed or error"
    fi
  fi

  # 3. eth_getTransactionReceipt (optional)
  if [ -n "$TX_HASH" ]; then
    if [ "$VERBOSE" == "true" ]; then
      echo "--- 3. eth_getTransactionReceipt (Tx: $TX_HASH) ---"
      RESPONSE=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
        \"jsonrpc\": \"2.0\",
        \"method\": \"eth_getTransactionReceipt\",
        \"params\": [\"$TX_HASH\"],
        \"id\": 1
      }" $URL)
      echo "$RESPONSE" | jq . | tee -a getTransactionReceipt.tmp
      echo ""
    else
      RESPONSE=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
        \"jsonrpc\": \"2.0\",
        \"method\": \"eth_getTransactionReceipt\",
        \"params\": [\"$TX_HASH\"],
        \"id\": 1
      }" $URL)
      echo "$RESPONSE" >> getTransactionReceipt.tmp
      
      # Check if request was successful
      if echo "$RESPONSE" | jq -e '.result' > /dev/null 2>&1; then
        echo "✓ eth_getTransactionReceipt: Receipt found"
      else
        echo "✗ eth_getTransactionReceipt: Failed or not found"
      fi
    fi
  fi
}

# Backwards mode: loop from latest or given block backwards
if [ "${ARGS[0]}" == "-b" ]; then
  START_BLOCK=${ARGS[1]:-"latest"}
  
  # Get starting block number
  if [ "$START_BLOCK" == "latest" ]; then
    echo "Fetching latest block number..."
    LATEST_HEX=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' $URL | jq -r .result)
    
    if [ "$LATEST_HEX" == "null" ] || [ -z "$LATEST_HEX" ]; then
      echo "Error: Failed to fetch latest block number."
      exit 1
    fi
    
    CURRENT_DEC=$(printf "%d" "$LATEST_HEX")
    echo "Starting from latest block: $CURRENT_DEC ($LATEST_HEX)"
  else
    # Convert provided block to decimal
    if [[ "$START_BLOCK" =~ ^0x ]]; then
      CURRENT_DEC=$(printf "%d" "$START_BLOCK")
    else
      CURRENT_DEC=$START_BLOCK
    fi
    echo "Starting from block: $CURRENT_DEC (0x$(printf "%x" "$CURRENT_DEC"))"
  fi
  
  echo "Press Ctrl+C to stop..."
  echo "========================================"
  echo ""
  
  # Loop backwards
  while [ $CURRENT_DEC -ge 0 ]; do
    BLOCK_HEX=$(printf "0x%x" "$CURRENT_DEC")
    echo "========== Block $CURRENT_DEC ($BLOCK_HEX) =========="
    
    query_block "$BLOCK_HEX"
    
    echo "========================================"
    echo ""
    
    # Decrement block number
    CURRENT_DEC=$((CURRENT_DEC - 1))
    
    # Optional: add a small delay to avoid overwhelming the node
    # sleep 0.1
  done
  
  exit 0
fi

# Offset mode: -n <offset>
if [ "${ARGS[0]}" == "-n" ]; then
  OFFSET=${ARGS[1]}
  TX_HASH=${ARGS[2]}
  
  if [ -z "$OFFSET" ]; then
    echo "Usage: $0 -n <offset> [tx_hash] [-v|--verbose]"
    exit 1
  fi

  # Fetch latest block number
  LATEST_HEX=$(curl -s -X POST -H "Content-Type: application/json" -m 10 -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' $URL | jq -r .result)
  
  if [ "$LATEST_HEX" == "null" ] || [ -z "$LATEST_HEX" ]; then
    echo "Error: Failed to fetch latest block number."
    exit 1
  fi

  # Convert hex to decimal
  LATEST_DEC=$(printf "%d" "$LATEST_HEX")
  
  # Calculate target block
  TARGET_DEC=$((LATEST_DEC - OFFSET))
  
  # Convert back to hex
  BLOCK=$(printf "0x%x" "$TARGET_DEC")
  
  echo "Latest block: $LATEST_DEC ($LATEST_HEX)"
  echo "Target block: $TARGET_DEC ($BLOCK) (Latest - $OFFSET)"
  echo ""
  
  query_block "$BLOCK" "$TX_HASH"
  exit 0
fi

# Single block mode
BLOCK=${ARGS[0]}
TX_HASH=${ARGS[1]}

if [ -z "$BLOCK" ]; then
  echo "Usage: $0 <block_number_or_hash> [tx_hash] [-v|--verbose]"
  echo "   or: $0 -n <offset_from_latest> [tx_hash] [-v|--verbose]"
  echo "   or: $0 -b [starting_block] [-v|--verbose]  # Loop backwards (default: latest)"
  echo ""
  echo "Options:"
  echo "  -v, --verbose    Show full JSON responses (default: quiet mode)"
  exit 1
fi

query_block "$BLOCK" "$TX_HASH"
