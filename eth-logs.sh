#!/bin/bash

URL="http://127.0.0.1:18585"

if [ "$1" == "-n" ]; then
  OFFSET=$2
  TX_HASH=$3
  
  if [ -z "$OFFSET" ]; then
    echo "Usage: $0 -n <offset> [tx_hash]"
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
else
  BLOCK=$1
  TX_HASH=$2
fi

if [ -z "$BLOCK" ]; then
  echo "Usage: $0 <block_number_or_hash> [tx_hash]"
  echo "   or: $0 -n <offset_from_latest> [tx_hash]"
  exit 1
fi

echo "--- 1. eth_getLogs (Block: $BLOCK) ---"
curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
  \"jsonrpc\": \"2.0\",
  \"method\": \"eth_getLogs\",
  \"params\": [
    {
      \"fromBlock\": \"$BLOCK\",
      \"toBlock\": \"$BLOCK\"
    }
  ],
  \"id\": 1
}" $URL | jq . | tee -a getLogs.tmp
echo ""

echo "--- 2. eth_getBlockReceipts (Block: $BLOCK) ---"
curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
  \"jsonrpc\": \"2.0\",
  \"method\": \"eth_getBlockReceipts\",
  \"params\": [\"$BLOCK\"],
  \"id\": 1
}" $URL | jq . | tee -a getBlockReceipts.tmp
echo ""

if [ -n "$TX_HASH" ]; then
  echo "--- 3. eth_getTransactionReceipt (Tx: $TX_HASH) ---"
  curl -s -X POST -H "Content-Type: application/json" -m 10 -d "{
    \"jsonrpc\": \"2.0\",
    \"method\": \"eth_getTransactionReceipt\",
    \"params\": [\"$TX_HASH\"],
    \"id\": 1
  }" $URL | jq . | tee -a getTransactionReceipt.tmp
  echo ""
fi
