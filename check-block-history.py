#!/usr/bin/env python3
#
# Fast block history and tx indexer checker for Bor/Polygon nodes.
# Python equivalent with threaded sampling for speed.
#
# Usage: ./check-block-history.py [RPC_URL]
# Example: ./check-block-history.py http://localhost:8745
#
# With auth path: ./check-block-history.py http://localhost:8745/yourAuthPath/

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

try:
    import requests
except ImportError:
    print("Error: requests required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Colors
RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[1;33m"
CYAN = "\033[0;36m"
NC = "\033[0m"

RPC_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8545"
TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "10"))


def rpc(method: str, *params) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "method": method, "params": list(params), "id": 1}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return data.get("result")
    except Exception:
        return None


def block_exists(block_num: int) -> bool:
    result = rpc("eth_getBlockByNumber", hex(block_num), False)
    return result is not None


def get_block_tx_hash(block_num: int) -> Optional[str]:
    result = rpc("eth_getBlockByNumber", hex(block_num), False)
    if not result or "transactions" not in result:
        return None
    txs = result["transactions"]
    if not txs:
        return None
    tx = txs[0]
    return tx if isinstance(tx, str) else tx.get("hash")


def tx_lookup_works(tx_hash: Optional[str]) -> bool:
    if not tx_hash:
        return False
    result = rpc("eth_getTransactionByHash", tx_hash)
    return result is not None


def get_tx_from_block_or_nearby(
    block: int, radius: int, current_dec: int
) -> Optional[tuple[int, str]]:
    for i in range(block, max(0, block - radius) - 1, -1):
        if i < 1:
            break
        h = get_block_tx_hash(i)
        if h:
            return (i, h)
    for i in range(block + 1, min(block + radius + 1, current_dec + 1)):
        if i > current_dec:
            break
        h = get_block_tx_hash(i)
        if h:
            return (i, h)
    return None


def sample_one(
    s: int, earliest_block: int, current_dec: int
) -> tuple[int, str | tuple[int, int] | tuple[int, str], Optional[tuple[int, int]]]:
    """Returns (sample, 'skipped', None) if out of range; (sample, 'no_tx', (low, high)) if no tx; (sample, (blk, h), None) if found."""
    if s > current_dec or s < earliest_block:
        return (s, "skipped", None)
    result = get_tx_from_block_or_nearby(s, 50, current_dec)
    if result is None:
        low = max(1, s - 50)
        high = min(current_dec, s + 50)
        return (s, "no_tx", (low, high))
    return (s, result, None)


def main() -> None:
    print(f"{CYAN}=== Block History & Tx Indexer Check ==={NC}")
    print(f"RPC: {RPC_URL}")
    print()

    # 1. Get current block
    print(f"{CYAN}[1/3]{NC} Fetching current block...")
    current_block = rpc("eth_blockNumber")
    if current_block is None:
        print(f"{RED}ERROR: Cannot connect to RPC or get block number{NC}")
        sys.exit(1)
    current_dec = int(current_block, 16)
    print(f"      Latest block: {current_dec} ({current_block})")
    print()

    # 2. Binary search for earliest available block
    print(f"{CYAN}[2/3]{NC} Checking block history (binary search)...")
    low, high = 1, current_dec
    iterations = 0
    earliest_block = 1

    if block_exists(1):
        iterations += 1
        print("      Block 1 exists - spot-checking range...")
        check_blocks = [1, 100, 1000, 10000, 100000]
        if current_dec > 1_000_000:
            check_blocks.append(1_000_000)
        if current_dec > 5_000_000:
            check_blocks.append(5_000_000)
        check_blocks.append(current_dec // 2)
        all_ok = True
        for b in check_blocks:
            if b > current_dec or b < 1:
                continue
            iterations += 1
            if block_exists(b):
                print(f"      ✓ Block {b}")
            else:
                print(f"      {RED}✗ Block {b} missing{NC}")
                all_ok = False
                low = b
                break
        if all_ok:
            earliest_block = 1
            print(f"      {GREEN}✓ Full block history from block 1{NC}")
        else:
            high = current_dec
            while low < high:
                mid = (low + high) // 2
                iterations += 1
                if block_exists(mid):
                    high = mid
                else:
                    low = mid + 1
            earliest_block = low
            print(f"      {YELLOW}Earliest block: {earliest_block}{NC}")
    else:
        iterations += 1
        print("      Block 1 missing - finding earliest available block...")
        while low < high:
            mid = (low + high) // 2
            iterations += 1
            if block_exists(mid):
                high = mid
            else:
                low = mid + 1
        earliest_block = low
        print(f"      {YELLOW}Earliest block: {earliest_block}{NC}")

    print(f"      (used {iterations} block queries)")
    print()

    # 3. Tx indexer check
    print(f"{CYAN}[3/3]{NC} Checking tx indexer (binary search)...")
    tx_iterations = 0
    tx_indexer_earliest = "N/A"

    samples = [1, 100, 1000, 10000, 100000, 500000, 1000000, 2000000, 3000000, 4000000, 5000000]

    recent_result = get_tx_from_block_or_nearby(current_dec, 500, current_dec)
    tx_iterations += 1

    if recent_result is None:
        print(f"      {YELLOW}Could not find block with transactions to test{NC}")
    else:
        recent_block, recent_tx = recent_result
        tx_iterations += 1
        if tx_lookup_works(recent_tx):
            print(f"      Testing block {recent_block}, tx {recent_tx} → {GREEN}✓ lookup OK{NC}")
            tx_working = current_dec
            tx_failing = earliest_block

            # Threaded sampling
            print("      Sampling blocks to find tx indexer boundary (parallel)...")
            sample_results: list[
                tuple[int, str | tuple[int, int] | tuple[int, str], tuple[int, int] | None]
            ] = []

            max_workers = min(8, len(samples))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(sample_one, s, earliest_block, current_dec): s
                    for s in samples
                }
                for future in as_completed(futures):
                    s = futures[future]
                    try:
                        sample, status, extra = future.result()
                        tx_iterations += 1
                        if status == "skipped":
                            sample_results.append((sample, "skipped", None))
                        elif status == "no_tx":
                            sample_results.append((sample, "no_tx", extra))
                        else:
                            sample_results.append((sample, status, None))
                    except Exception as e:
                        sample_results.append((s, ("error", str(e)), None))

            # Sort by sample and print in order; stop at first failure
            sample_results.sort(key=lambda x: x[0])
            for sample, status, extra in sample_results:
                if status == "skipped":
                    print(f"        Sample {sample}: skipped (out of block range)")
                elif status == "no_tx":
                    low, high = extra
                    print(
                        f"        Sample block {sample}: no block with txs in range (blocks {low}..{high})"
                    )
                elif isinstance(status, tuple) and len(status) == 2 and isinstance(status[0], int):
                    blk, h = status
                    tx_iterations += 1
                    indexed = tx_lookup_works(h)
                    if indexed:
                        print(
                            f"        Sample block {sample}: block {blk}, tx {h} → {GREEN}✓ indexed{NC}"
                        )
                        if blk < tx_working:
                            tx_working = blk
                    else:
                        print(
                            f"        Sample block {sample}: block {blk}, tx {h} → {RED}✗ not indexed{NC}"
                        )
                        tx_failing = blk
                        break
                elif isinstance(status, tuple) and status[0] == "error":
                    print(f"        Sample {sample}: error - {status[1]}")

            # Binary search between tx_working and tx_failing
            if tx_failing > tx_working:
                print(
                    f"      Binary search between block {tx_working} (✓) and {tx_failing} (✗)..."
                )
                low, high = tx_working, tx_failing
                while low < high:
                    mid = (low + high) // 2
                    result = get_tx_from_block_or_nearby(mid, 20, current_dec)
                    tx_iterations += 1
                    if result is not None:
                        blk, h = result
                        tx_iterations += 1
                        if tx_lookup_works(h):
                            high = mid
                        else:
                            low = mid + 1
                    else:
                        low = mid + 1
                tx_indexer_earliest = low
                print(f"      {YELLOW}Tx indexer available from block ~{tx_indexer_earliest}{NC}")
            elif tx_working == 1 or (earliest_block == 1 and tx_working <= 1000):
                tx_indexer_earliest = 1
                print(
                    f"      {GREEN}✓ Full tx index from block 1 (eth_getTransactionByHash works for entire chain){NC}"
                )
            else:
                tx_indexer_earliest = tx_working
                print(f"      {GREEN}Tx indexer available from block ~{tx_indexer_earliest}{NC}")
        else:
            print(f"      Block {recent_block}, tx {recent_tx} → {RED}✗ lookup failed{NC}")

    print(f"      (used ~{tx_iterations} queries)")
    print()

    # Summary
    print(f"{CYAN}=== Summary ==={NC}")
    eb = earliest_block or 1
    print(f"Block range:          {eb} → {current_dec}")
    if eb == 1:
        print(f"Block history:        {GREEN}✓ FULL (from genesis){NC}")
    else:
        print(f"Block history:        {YELLOW}Pruned from block {earliest_block}{NC}")
    print("Tx indexer:           ", end="")
    if tx_indexer_earliest == 1:
        print(f"{GREEN}✓ FULL (eth_getTransactionByHash works from block 1){NC}")
    elif tx_indexer_earliest == "N/A":
        print(f"{YELLOW}N/A (could not verify){NC}")
    else:
        print(
            f"{YELLOW}Partial - from block ~{tx_indexer_earliest} (older txs not lookupable by hash){NC}"
        )
    print()


if __name__ == "__main__":
    main()
