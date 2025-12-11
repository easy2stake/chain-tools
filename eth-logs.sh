#!/bin/bash

URL="http://127.0.0.1:18545"
BLOCK=$1
TX_HASH=$2

if [ -z "$BLOCK" ]; then
  echo "Usage: $0 <block_number_or_hash> [tx_hash]"
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
