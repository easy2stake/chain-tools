#!/usr/bin/env python3
#
# Fast block history and tx indexer checker for Bor/Polygon nodes.
# Python equivalent with threaded sampling for speed.

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Tuple

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


def print_help() -> None:
    script = sys.argv[0].split("/")[-1]
    print(f"""Usage: {script} RPC_URL

Fast block history, tx indexer, and archival state checker for Bor/Polygon nodes.
Uses binary search + threaded sampling to minimize round trips.
Tests: block history, tx index (eth_getTransactionByHash), archival state (eth_getBalance).

Arguments:
  RPC_URL    JSON-RPC endpoint (required)

Examples:
  {script} http://localhost:8545
  {script} http://localhost:8745
  {script} http://localhost:8745/yourAuthPath

Environment:
  CHECK_TIMEOUT  RPC timeout in seconds (default: 10)

Options:
  -h, --help     Show this help
  -v, --verbose  Dump RPC requests and responses
""")


def parse_args() -> Optional[Tuple[str, bool]]:
    if "-h" in sys.argv or "--help" in sys.argv:
        print_help()
        return None
    verbose = "-v" in sys.argv or "--verbose" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print_help()
        return None
    return (args[0], verbose)


_parsed = parse_args()
if _parsed is None:
    sys.exit(0)
RPC_URL, VERBOSE = _parsed

TIMEOUT = int(os.environ.get("CHECK_TIMEOUT", "10"))


def rpc(method: str, *params) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "method": method, "params": list(params), "id": 1}
    try:
        if VERBOSE:
            print(f"  {YELLOW}[RPC] → {method}{NC} {json.dumps(list(params))}")
        r = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        result = data.get("result")
        if VERBOSE:
            out = json.dumps(result)
            if len(out) > 500:
                out = out[:500] + "..."
            print(f"  {YELLOW}[RPC] ← {method}{NC} {out}")
        return result
    except Exception as e:
        if VERBOSE:
            print(f"  {RED}[RPC] ✗ {method}{NC} {e}")
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


def get_tx_by_hash(tx_hash: Optional[str]) -> Optional[dict]:
    """Fetch full tx by hash. Returns tx dict or None."""
    if not tx_hash:
        return None
    return rpc("eth_getTransactionByHash", tx_hash)


def tx_lookup_works(tx_hash: Optional[str]) -> bool:
    return get_tx_by_hash(tx_hash) is not None


def _short_addr(addr: str, head: int = 6, tail: int = 4) -> str:
    """Shorten address for display: 0x1234...abcd."""
    if not addr or len(addr) <= head + tail + 2:
        return addr
    return f"{addr[:head+2]}...{addr[-tail:]}"


def archival_balance_works(address: str, block_num: int) -> bool:
    """Check if eth_getBalance works at historical block (archival state test)."""
    if not address:
        return False
    result = rpc("eth_getBalance", address, hex(block_num))
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
    print(f"{CYAN}[1/4]{NC} Fetching current block...")
    current_block = rpc("eth_blockNumber")
    if current_block is None:
        print(f"{RED}ERROR: Cannot connect to RPC or get block number{NC}")
        sys.exit(1)
    current_dec = int(current_block, 16)
    print(f"      Latest block: {current_dec} ({current_block})")
    print()

    # 2. Binary search for earliest available block
    print(f"{CYAN}[2/4]{NC} Checking block history (binary search)...")
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

    # 3. Tx indexer + archival check
    print(f"{CYAN}[3/4]{NC} Checking tx indexer and archival state (binary search)...")
    tx_iterations = 0
    tx_indexer_earliest = "N/A"
    archival_earliest = "N/A"
    archival_working = current_dec
    archival_failing = earliest_block
    archival_tested = False

    # Derive samples from current block inwards towards 1 (halving each step)
    samples = []
    b = current_dec
    while b >= 1:
        samples.append(int(b))
        if len(samples) >= 12:
            break
        b = b // 2
    if 1 not in samples:
        samples.append(1)
    # Always include past 200000 blocks in 10000 increments
    for i in range(21):
        b = current_dec - i * 10000
        if b >= 1:
            samples.append(b)
    # Always include past 1000 blocks in 100 increments
    for i in range(11):
        b = current_dec - i * 100
        if b >= 1:
            samples.append(b)
    # Always include past 130 blocks in 10 increments
    for i in range(14):
        b = current_dec - i * 10
        if b >= 1:
            samples.append(b)
    samples = sorted(set(samples))

    recent_result = get_tx_from_block_or_nearby(current_dec, 500, current_dec)
    tx_iterations += 1

    if recent_result is None:
        print(f"      {YELLOW}Could not find block with transactions to test{NC}")
    else:
        recent_block, recent_tx = recent_result
        tx_iterations += 1
        recent_tx_obj = get_tx_by_hash(recent_tx)
        if recent_tx_obj is not None:
            print(f"      Testing blk {recent_block}")
            print(f"        tx: {recent_tx} → {GREEN}✓ lookup OK{NC}")
            if recent_tx_obj.get("from"):
                from_addr = recent_tx_obj["from"]
                tx_iterations += 1
                ar = archival_balance_works(from_addr, recent_block)
                archival_tested = True
                ar_icon = f"{GREEN}✓{NC}" if ar else f"{RED}✗{NC}"
                print(f"        {ar_icon} archival: [eth_getBalance({from_addr}, {recent_block})]")
            tx_working = current_dec
            tx_failing = earliest_block

            # Threaded sampling
            print("      Sampling blocks to find tx indexer / archival boundary (parallel)...")
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

            # Sort by sample and print in order; stop at first tx indexer failure
            sample_results.sort(key=lambda x: x[0])
            for sample, status, extra in sample_results:
                if status == "skipped":
                    print(f"        Sample {sample}: skipped (out of block range)")
                elif status == "no_tx":
                    low, high = extra
                    print(
                        f"        Sample {sample}: no block with txs in range (blocks {low}..{high})"
                    )
                elif isinstance(status, tuple) and len(status) == 2 and isinstance(status[0], int):
                    blk, h = status
                    tx_iterations += 1
                    tx_obj = get_tx_by_hash(h)
                    indexed = tx_obj is not None
                    if indexed:
                        archival_ok = None
                        from_addr = tx_obj.get("from") if tx_obj else None
                        if tx_obj and from_addr:
                            tx_iterations += 1
                            archival_ok = archival_balance_works(from_addr, blk)
                            archival_tested = True
                            if archival_ok:
                                archival_working = min(archival_working, blk)
                            else:
                                archival_failing = blk
                        print(f"        Sample {sample}: blk {blk}")
                        print(f"            tx: {h} → {GREEN}✓ indexed{NC}")
                        if archival_ok is not None:
                            ar_icon = f"{GREEN}✓{NC}" if archival_ok else f"{RED}✗{NC}"
                            print(f"            {ar_icon} archival: [eth_getBalance({from_addr}, {blk})]")
                        if blk < tx_working:
                            tx_working = blk
                    else:
                        print(f"        Sample {sample}: blk {blk}")
                        print(f"            tx: {h} → {RED}✗ not indexed{NC}")
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
                        tx_obj = get_tx_by_hash(h)
                        if tx_obj is not None:
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

            # Archival binary search (if we found a boundary)
            if archival_tested and archival_failing > archival_working:
                print(
                    f"      Binary search for archival boundary (block {archival_working} ✓ vs {archival_failing} ✗)..."
                )
                low, high = archival_working, archival_failing
                while low < high:
                    mid = (low + high) // 2
                    result = get_tx_from_block_or_nearby(mid, 20, current_dec)
                    tx_iterations += 1
                    if result is not None:
                        blk, h = result
                        tx_obj = get_tx_by_hash(h)
                        tx_iterations += 1
                        if tx_obj and tx_obj.get("from"):
                            tx_iterations += 1
                            if archival_balance_works(tx_obj["from"], blk):
                                high = mid
                            else:
                                low = mid + 1
                        else:
                            low = mid + 1
                    else:
                        low = mid + 1
                archival_earliest = low
                print(f"      {YELLOW}Archival state (eth_getBalance) available from block ~{archival_earliest}{NC}")
            elif archival_tested and (archival_working == 1 or (earliest_block == 1 and archival_working <= 1000)):
                archival_earliest = 1
                print(
                    f"      {GREEN}✓ Full archival state from block 1 (eth_getBalance works for entire chain){NC}"
                )
            elif archival_tested and archival_failing <= archival_working:
                archival_earliest = archival_working
                print(f"      {GREEN}Archival state available from block ~{archival_earliest}{NC}")
        else:
            print(f"      Block {recent_block}, tx {recent_tx} → {RED}✗ lookup failed{NC}")

    print(f"      (used ~{tx_iterations} queries)")
    print()

    # 4. Summary
    print(f"{CYAN}[4/4]{NC} Summary")
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
        tx_indexed_blocks = current_dec - tx_indexer_earliest
        print(
            f"{YELLOW}Partial - from block ~{tx_indexer_earliest} ({tx_indexed_blocks:,} blocks indexed, older txs not lookupable by hash){NC}"
        )
    print("Archival state:        ", end="")
    if archival_earliest == 1:
        print(f"{GREEN}✓ FULL (eth_getBalance works at historical blocks){NC}")
    elif archival_earliest == "N/A":
        print(f"{YELLOW}N/A (could not verify){NC}")
    else:
        archival_blocks = current_dec - archival_earliest
        print(
            f"{YELLOW}Partial - from block ~{archival_earliest} ({archival_blocks:,} blocks with archival state, older state not queryable){NC}"
        )
    print()


if __name__ == "__main__":
    main()
