#!/bin/bash

show_help() {
  cat << 'EOF'
eth-logs.sh - Query Ethereum JSON-RPC for logs and receipts

USAGE:
  eth-logs.sh [URL] <block> [tx_hash] [options]
  eth-logs.sh [URL] -n <offset> [tx_hash] [options]
  eth-logs.sh [URL] -b [starting_block] [options]

MODES:
  Single block    Query one block (default: latest)
  -n <offset>     Query block at (latest - offset)
  -b [block]      Loop backwards from block (default: latest); Ctrl+C to stop

ARGUMENTS:
  URL             RPC endpoint (http://host:port or host:port). If omitted, uses http://127.0.0.1:18585
  block           Block number (hex 0xNNN or decimal) or "latest"
  tx_hash         Optional tx hash to also fetch eth_getTransactionReceipt

OPTIONS:
  -h, --help      Show this help
  -v, --verbose   Show full JSON responses (default: quiet mode)

EXAMPLES:
  eth-logs.sh                                    # latest block, local node
  eth-logs.sh http://88.198.52.126:18885         # latest block, remote node
  eth-logs.sh 157.90.91.196:8545                # host:port (http:// added automatically)
  eth-logs.sh http://localhost:8545 0x12345      # specific block (hex)
  eth-logs.sh 157.90.91.196:8545 99998          # block by decimal
  eth-logs.sh -n 5                               # 5 blocks behind latest
  eth-logs.sh -n 10 0xabc...                     # offset + tx receipt
  eth-logs.sh -b                                 # loop backwards from latest
  eth-logs.sh -b 0x1000                          # loop backwards from block
  eth-logs.sh -v                                 # verbose (full JSON)
EOF
}

URL="${1:-http://127.0.0.1:18585}"
VERBOSE=false

# Parse for help and verbose flags
for arg in "$@"; do
  if [ "$arg" == "-h" ] || [ "$arg" == "--help" ]; then
    show_help
    exit 0
  fi
  if [ "$arg" == "-v" ] || [ "$arg" == "--verbose" ]; then
    VERBOSE=true
  fi
done

# Remove help and verbose flags from arguments
ARGS=()
for arg in "$@"; do
  if [ "$arg" != "-h" ] && [ "$arg" != "--help" ] && [ "$arg" != "-v" ] && [ "$arg" != "--verbose" ]; then
    ARGS+=("$arg")
  fi
done

# If first arg looks like a URL, use it as URL and shift: block becomes next arg (default: latest)
if [[ "${ARGS[0]}" == http://* ]] || [[ "${ARGS[0]}" == https://* ]]; then
  URL="${ARGS[0]}"
  ARGS=("${ARGS[@]:1}")
  # If no block specified after URL, default to latest
  if [ ${#ARGS[@]} -eq 0 ]; then
    ARGS=("latest")
  fi
elif [[ "${ARGS[0]}" == *:* ]] && [[ "${ARGS[0]}" != 0x* ]]; then
  # host:port format (e.g. 157.90.91.196:8545) - add http://
  URL="http://${ARGS[0]}"
  ARGS=("${ARGS[@]:1}")
  if [ ${#ARGS[@]} -eq 0 ]; then
    ARGS=("latest")
  fi
fi

# Convert block to hex if decimal (JSON-RPC expects 0xNNN or "latest"/"earliest"/"pending")
to_hex_block() {
  local b="$1"
  case "$b" in
    latest|earliest|pending)  echo "$b" ;;
    0x*)                     echo "$b" ;;
    [0-9]*)                  printf "0x%x" "$b" ;;
    *)                       echo "$b" ;;
  esac
}

# Function to query a single block
query_block() {
  local BLOCK=$1
  local TX_HASH=$2
  BLOCK=$(to_hex_block "$BLOCK")
  
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
    if echo "$RESPONSE" | jq -e '.result' > /dev/null 2>&1; then
      RECEIPT_COUNT=$(echo "$RESPONSE" | jq -r '.result | length')
      echo "✓ Transactions: $RECEIPT_COUNT"
    fi
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
      echo "✓ Transactions: $RECEIPT_COUNT"
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
    echo "Error: -n requires an offset. Run '$0 --help' for usage."
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
  echo "Error: No block specified."
  echo "Run '$0 --help' for usage."
  exit 1
fi

query_block "$BLOCK" "$TX_HASH"
