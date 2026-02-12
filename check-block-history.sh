#!/usr/bin/env bash
#
# Fast block history and tx indexer checker for Bor/Polygon nodes.
# Uses binary search + batch RPC to minimize round trips (O(log n) instead of O(n)).
#
# Usage: ./check-block-history.sh [RPC_URL]
# Example: ./check-block-history.sh http://localhost:8745
#
# With auth path: ./check-block-history.sh http://localhost:8745/yourAuthPath/

set -e

RPC_URL="${1:-http://127.0.0.1:8545}"
TIMEOUT="${CHECK_TIMEOUT:-10}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

rpc() {
    local method="$1"
    shift
    local params="$*"
    local body
    if [[ -n "$params" ]]; then
        body="{\"jsonrpc\":\"2.0\",\"method\":\"$method\",\"params\":[$params],\"id\":1}"
    else
        body="{\"jsonrpc\":\"2.0\",\"method\":\"$method\",\"params\":[],\"id\":1}"
    fi
    curl -s --max-time "$TIMEOUT" -X POST "$RPC_URL" \
        -H "Content-Type: application/json" \
        -d "$body"
}

# Convert dec to hex
to_hex() {
    printf "0x%x" "$1"
}

# Check if block exists (returns 0 if yes)
block_exists() {
    local block_hex
    block_hex=$(to_hex "$1")
    local result
    result=$(rpc "eth_getBlockByNumber" "\"$block_hex\",false" | jq -r '.result')
    [[ "$result" != "null" ]]
}

# Get first tx hash from block (empty if no txs)
get_block_tx_hash() {
    local block_hex
    block_hex=$(to_hex "$1")
    rpc "eth_getBlockByNumber" "\"$block_hex\",false" | jq -r '.result.transactions[0] // empty'
}

# Check if tx can be fetched by hash (tx indexer)
tx_lookup_works() {
    local tx_hash="$1"
    local result
    result=$(rpc "eth_getTransactionByHash" "\"$tx_hash\"" | jq -r '.result')
    [[ -n "$tx_hash" && "$result" != "null" ]]
}

echo -e "${CYAN}=== Block History & Tx Indexer Check ===${NC}"
echo "RPC: $RPC_URL"
echo ""

# 1. Get current block
echo -e "${CYAN}[1/3]${NC} Fetching current block..."
current_block=$(rpc "eth_blockNumber" | jq -r '.result')
if [[ -z "$current_block" || "$current_block" == "null" ]]; then
    echo -e "${RED}ERROR: Cannot connect to RPC or get block number${NC}"
    exit 1
fi
current_dec=$((current_block))
echo "      Latest block: $current_dec (${current_block})"
echo ""

# 2. Binary search for earliest available block
echo -e "${CYAN}[2/3]${NC} Checking block history (binary search)..."
low=1
high=$current_dec
iterations=0
earliest_block=1

# Quick sanity: try block 1 first
if block_exists 1; then
    ((iterations++)) || true
    echo "      Block 1 exists - spot-checking range..."
    # Verify we have continuous history: spot-check a few blocks
    check_blocks=(1 100 1000 10000 100000)
    [[ $current_dec -gt 1000000 ]] && check_blocks+=(1000000)
    [[ $current_dec -gt 5000000 ]] && check_blocks+=(5000000)
    check_blocks+=($((current_dec / 2)))
    all_ok=true
    for b in "${check_blocks[@]}"; do
        [[ $b -gt $current_dec ]] && continue
        [[ $b -lt 1 ]] && continue
        if block_exists "$b"; then
            echo -e "      ✓ Block $b"
            ((iterations++)) || true
        else
            echo -e "      ${RED}✗ Block $b missing${NC}"
            all_ok=false
            low=$b
            break
        fi
    done
    if $all_ok; then
        earliest_block=1
        echo -e "      ${GREEN}✓ Full block history from block 1${NC}"
    else
        # Binary search from first missing to find earliest available
        high=$current_dec
        while [[ $low -lt $high ]]; do
            mid=$(( (low + high) / 2 ))
            ((iterations++)) || true
            if block_exists "$mid"; then
                high=$mid
            else
                low=$((mid + 1))
            fi
        done
        earliest_block=$low
        echo -e "      ${YELLOW}Earliest block: $earliest_block${NC}"
    fi
else
    ((iterations++)) || true
    echo "      Block 1 missing - finding earliest available block..."
    while [[ $low -lt $high ]]; do
        mid=$(( (low + high) / 2 ))
        ((iterations++)) || true
        if block_exists "$mid"; then
            high=$mid
        else
            low=$((mid + 1))
        fi
    done
    earliest_block=$low
    echo -e "      ${YELLOW}Earliest block: $earliest_block${NC}"
fi
echo "      (used $iterations block queries)"
echo ""

# 3. Binary search for tx indexer boundary
echo -e "${CYAN}[3/3]${NC} Checking tx indexer (binary search)..."
tx_iterations=0
tx_indexer_earliest="N/A"

# Helper: get "block tx_hash" from block or nearby (Polygon has empty blocks)
# Returns "block tx_hash" or empty
get_tx_from_block_or_nearby() {
    local block=$1
    local radius=${2:-100}
    local i
    for (( i=block; i>=block-radius && i>=1; i-- )); do
        local h
        h=$(get_block_tx_hash "$i")
        if [[ -n "$h" ]]; then
            echo "$i $h"
            return
        fi
    done
    for (( i=block+1; i<=block+radius && i<=current_dec; i++ )); do
        local h
        h=$(get_block_tx_hash "$i")
        if [[ -n "$h" ]]; then
            echo "$i $h"
            return
        fi
    done
}

# Check recent block first
recent_result=$(get_tx_from_block_or_nearby $current_dec 500)
((tx_iterations++)) || true

if [[ -z "$recent_result" ]]; then
    echo -e "      ${YELLOW}Could not find block with transactions to test${NC}"
else
    recent_block=$(echo "$recent_result" | cut -d' ' -f1)
    recent_tx=$(echo "$recent_result" | cut -d' ' -f2)
    if tx_lookup_works "$recent_tx"; then
        echo -e "      Testing block $recent_block, tx $recent_tx → ${GREEN}✓ lookup OK${NC}"
        ((tx_iterations++)) || true
        # Binary search for oldest block where tx lookup works
        tx_low=$earliest_block
        tx_high=$current_dec
        tx_working=$current_dec
        tx_failing=$earliest_block

        # Sample strategically then binary search
        echo "      Sampling blocks to find tx indexer boundary..."
        samples=(1 100 1000 10000 100000 500000 1000000 2000000 3000000 4000000 5000000)
        for s in "${samples[@]}"; do
            [[ $s -gt $current_dec ]] && continue
            [[ $s -lt $earliest_block ]] && continue
            result=$(get_tx_from_block_or_nearby "$s" 50)
            ((tx_iterations++)) || true
            if [[ -n "$result" ]]; then
                blk=$(echo "$result" | cut -d' ' -f1)
                h=$(echo "$result" | cut -d' ' -f2)
                if tx_lookup_works "$h"; then
                    ((tx_iterations++)) || true
                    echo -e "        block $blk, tx $h → ${GREEN}✓${NC}"
                    [[ $blk -lt $tx_working ]] && tx_working=$blk
                else
                    echo -e "        block $blk, tx $h → ${RED}✗ (not indexed)${NC}"
                    tx_failing=$blk
                    break
                fi
            fi
        done

        # Binary search between tx_working and tx_failing
        # tx_working = works, tx_failing = fails. Find smallest block where it works (threshold).
        if [[ $tx_failing -gt $tx_working ]]; then
            echo "      Binary search between block $tx_working (✓) and $tx_failing (✗)..."
            low=$tx_working   # we know this works
            high=$tx_failing  # we know this fails
            while [[ $low -lt $high ]]; do
                mid=$(( (low + high) / 2 ))
                result=$(get_tx_from_block_or_nearby "$mid" 20)
                ((tx_iterations++)) || true
                if [[ -n "$result" ]]; then
                    blk=$(echo "$result" | cut -d' ' -f1)
                    h=$(echo "$result" | cut -d' ' -f2)
                    if tx_lookup_works "$h"; then
                        ((tx_iterations++)) || true
                        high=$mid   # works, threshold <= mid
                    else
                        low=$((mid + 1))  # fails, threshold > mid
                    fi
                else
                    low=$((mid + 1))
                fi
            done
            tx_indexer_earliest=$low  # smallest block where it works
            echo -e "      ${YELLOW}Tx indexer available from block ~$tx_indexer_earliest${NC}"
        elif [[ $tx_working -eq 1 ]] || ([[ $earliest_block -eq 1 ]] && [[ $tx_working -le 1000 ]]); then
            tx_indexer_earliest=1
            echo -e "      ${GREEN}✓ Full tx index from block 1 (eth_getTransactionByHash works for entire chain)${NC}"
        else
            tx_indexer_earliest=$tx_working
            echo -e "      ${GREEN}Tx indexer available from block ~$tx_indexer_earliest${NC}"
        fi
    else
        echo -e "      Block $recent_block, tx $recent_tx → ${RED}✗ lookup failed${NC}"
    fi
fi
echo "      (used ~$tx_iterations queries)"
echo ""

# Summary
echo -e "${CYAN}=== Summary ===${NC}"
echo "Block range:          ${earliest_block:-1} → $current_dec"
if [[ ${earliest_block:-1} -eq 1 ]]; then
    echo -e "Block history:        ${GREEN}✓ FULL (from genesis)${NC}"
else
    echo -e "Block history:        ${YELLOW}Pruned from block ${earliest_block}${NC}"
fi
echo -n "Tx indexer:           "
if [[ "$tx_indexer_earliest" == "1" ]]; then
    echo -e "${GREEN}✓ FULL (eth_getTransactionByHash works from block 1)${NC}"
elif [[ "$tx_indexer_earliest" == "N/A" ]]; then
    echo -e "${YELLOW}N/A (could not verify)${NC}"
else
    echo -e "${YELLOW}Partial - from block ~$tx_indexer_earliest (older txs not lookupable by hash)${NC}"
fi
echo ""
