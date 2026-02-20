#!/usr/bin/env python3
#
# Fast block history and tx indexer checker for any EVM-compatible chain.
# Uses binary search to find block history, tx indexer, and archival boundaries.

import argparse
import json
import sys
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


def parse_args() -> argparse.Namespace:
    script = sys.argv[0].split("/")[-1]
    parser = argparse.ArgumentParser(
        prog=script,
        description="Fast block history, tx indexer, archival state, log index, and block receipts checker for any EVM-compatible chain. "
        "Uses binary search to minimize RPC round trips. "
        "Tests: block history, tx index (eth_getTransactionByHash), archival (eth_getBalance), log index (eth_getLogs), block receipts (eth_getBlockReceipts).",
        epilog="""Flow:
  [1] Get current block (eth_blockNumber)
       |
       v
  [2] Binary search: earliest block (eth_getBlockByNumber)
       |
       v
  [3] Binary search: tx indexer + archival (eth_getTransactionByHash, eth_getBalance)
       |
       v
  [4] Binary search: log index + block receipts (eth_getLogs, eth_getBlockReceipts)
       |
       v
  [5] Summary

Examples:
  %(prog)s http://localhost:8545
  %(prog)s http://localhost:8745
  %(prog)s -t 5 localhost:8545""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "rpc_url",
        help="JSON-RPC endpoint (http:// is added if omitted)",
    )
    parser.add_argument(
        "-t", "--timeout",
        type=int,
        default=10,
        metavar="SECS",
        help="RPC timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Dump RPC requests and responses",
    )
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    return parser.parse_args()


_args = parse_args()
RPC_URL = _args.rpc_url
VERBOSE = _args.verbose
TIMEOUT = _args.timeout
if not RPC_URL.startswith(("http://", "https://")):
    RPC_URL = "http://" + RPC_URL


def rpc(method: str, *params) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "method": method, "params": list(params), "id": 1}
    try:
        if VERBOSE:
            print(f"  {YELLOW}[RPC] → {method}{NC} {json.dumps(list(params))}")
        r = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if VERBOSE:
            out = json.dumps(data)
            if len(out) > 200:
                out = out[:200] + "..."
            print(f"  {YELLOW}[RPC] ← {method}{NC} {out}")
        return data.get("result")
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


def _eth_get_logs_by_block_hash(block_hash: str):
    """Call eth_getLogs with blockHash filter. Returns (result, error) from response.
    Tries params as array [filter] first; if node returns 'cannot unmarshal array', retries with params as object."""
    filter_obj = {"blockHash": block_hash}
    last_error = None
    for params in ([filter_obj], filter_obj):
        payload = {"jsonrpc": "2.0", "method": "eth_getLogs", "params": params, "id": 1}
        try:
            if VERBOSE:
                print(f"  {YELLOW}[RPC] → eth_getLogs{NC} {json.dumps(params)}")
            resp = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            if VERBOSE:
                out = json.dumps(data)
                if len(out) > 200:
                    out = out[:200] + "..."
                print(f"  {YELLOW}[RPC] ← eth_getLogs{NC} {out}")
            err = data.get("error")
            if err is None:
                return (data.get("result"), None)
            last_error = err
            # Retry with params as object when node expects filter object not array
            msg = (err.get("message") or "") if isinstance(err, dict) else str(err)
            if "cannot unmarshal array" not in msg and "invalid argument" not in msg.lower():
                return (None, err)
        except Exception as e:
            if VERBOSE:
                print(f"  {RED}[RPC] ✗ eth_getLogs{NC} {e}")
            last_error = {"message": str(e)}
            if params is filter_obj:
                break
    return (None, last_error)


def logs_work_at_block(block_num: int) -> bool:
    """Check if eth_getLogs works at this block (uses blockHash per EIP-234; pruned blocks return error)."""
    result = rpc("eth_getBlockByNumber", hex(block_num), False)
    if not result or "hash" not in result:
        return False
    block_hash = result["hash"]
    logs_result, err = _eth_get_logs_by_block_hash(block_hash)
    return logs_result is not None


def block_receipts_work_at_block(block_num: int) -> bool:
    """Check if eth_getBlockReceipts works at this block."""
    result = rpc("eth_getBlockReceipts", hex(block_num))
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


def main() -> None:
    print(f"{CYAN}=== Block History & Tx Indexer Check ==={NC}")
    print(f"RPC: {RPC_URL}")
    print()

    # 1. Get current block
    print(f"{CYAN}[1/5]{NC} Fetching current block...")
    current_block = rpc("eth_blockNumber")
    if current_block is None:
        print(f"{RED}ERROR: Cannot connect to RPC or get block number{NC}")
        sys.exit(1)
    current_dec = int(current_block, 16)
    print(f"      Latest block: {current_dec} ({current_block})")
    print()

    # 2. Binary search for earliest available block
    print(f"{CYAN}[2/5]{NC} Checking block history (binary search)...")
    print("      Info: Binary search over block range; may issue many eth_getBlockByNumber calls for large chains.")
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
    print(f"{CYAN}[3/5]{NC} Checking tx indexer and archival state (binary search)...")
    print("      Info: Running tx indexer and archival binary searches; may issue many RPC calls for large chains.")
    tx_iterations = 0
    tx_indexer_earliest = "N/A"
    archival_earliest = "N/A"
    archival_tested = False

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

            # Tx indexer binary search from earliest_block to current_dec
            low, high = earliest_block, current_dec
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
            if tx_indexer_earliest == 1 or (earliest_block == 1 and tx_indexer_earliest <= 1000):
                tx_indexer_earliest = 1
                print(
                    f"      {GREEN}✓ Full tx index from block 1 (eth_getTransactionByHash works for entire chain){NC}"
                )
            else:
                print(f"      {YELLOW}Tx indexer available from block ~{tx_indexer_earliest}{NC}")

            # Archival binary search from earliest_block to current_dec
            archival_tested = True
            low, high = earliest_block, current_dec
            while low < high:
                mid = (low + high) // 2
                result = get_tx_from_block_or_nearby(mid, 20, current_dec)
                tx_iterations += 1
                if result is not None:
                    blk, h = result
                    tx_obj = get_tx_by_hash(h)
                    tx_iterations += 1
                    from_addr = tx_obj.get("from") if tx_obj else None
                    if from_addr:
                        tx_iterations += 1
                        if archival_balance_works(from_addr, blk):
                            high = mid
                        else:
                            low = mid + 1
                    else:
                        low = mid + 1
                else:
                    low = mid + 1
            archival_earliest = low
            if archival_earliest == 1 or (earliest_block == 1 and archival_earliest <= 1000):
                archival_earliest = 1
                print(
                    f"      {GREEN}✓ Full archival state from block 1 (eth_getBalance works for entire chain){NC}"
                )
            else:
                print(f"      {YELLOW}Archival state (eth_getBalance) available from block ~{archival_earliest}{NC}")
        else:
            print(f"      Block {recent_block}, tx {recent_tx} → {RED}✗ lookup failed{NC}")

    print(f"      (used ~{tx_iterations} queries)")
    print()

    # 4. Log index + block receipts
    print(f"{CYAN}[4/5]{NC} Checking log index and block receipts (binary search)...")
    print("      Info: eth_getLogs (blockHash) and eth_getBlockReceipts; may issue many RPC calls.")
    logs_earliest = "N/A"
    receipts_earliest = "N/A"
    lr_iterations = 0

    # If logs/receipts don't work at current block, method may be unsupported — skip binary search
    if not logs_work_at_block(current_dec):
        lr_iterations += 1
        print(f"      {YELLOW}eth_getLogs at latest block failed (unsupported or pruned) — skipping log index search{NC}")
    else:
        lr_iterations += 1
        low, high = earliest_block, current_dec
        while low < high:
            mid = (low + high) // 2
            lr_iterations += 1
            if logs_work_at_block(mid):
                high = mid
            else:
                low = mid + 1
        logs_earliest = low
        if logs_earliest == 1 or (earliest_block == 1 and logs_earliest <= 1000):
            logs_earliest = 1
            print(f"      {GREEN}✓ Full log index from block 1 (eth_getLogs works for entire chain){NC}")
        else:
            print(f"      {YELLOW}Log index (eth_getLogs) available from block ~{logs_earliest}{NC}")

    if not block_receipts_work_at_block(current_dec):
        lr_iterations += 1
        print(f"      {YELLOW}eth_getBlockReceipts at latest block failed (unsupported or pruned) — skipping receipts search{NC}")
    else:
        lr_iterations += 1
        low, high = earliest_block, current_dec
        while low < high:
            mid = (low + high) // 2
            lr_iterations += 1
            if block_receipts_work_at_block(mid):
                high = mid
            else:
                low = mid + 1
        receipts_earliest = low
        if receipts_earliest == 1 or (earliest_block == 1 and receipts_earliest <= 1000):
            receipts_earliest = 1
            print(f"      {GREEN}✓ Full block receipts from block 1 (eth_getBlockReceipts works for entire chain){NC}")
        else:
            print(f"      {YELLOW}Block receipts (eth_getBlockReceipts) available from block ~{receipts_earliest}{NC}")

    print(f"      (used ~{lr_iterations} queries)")
    print()

    # 5. Summary
    print(f"{CYAN}[5/5]{NC} Summary")
    print(f"{CYAN}=== Summary ==={NC}")
    eb = earliest_block or 1
    block_count = current_dec - eb + 1
    print(f"Block range:          {eb} → {current_dec} ({block_count:,} blocks)")
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
    print("Log index:             ", end="")
    if logs_earliest == 1:
        print(f"{GREEN}✓ FULL (eth_getLogs works from block 1){NC}")
    elif logs_earliest == "N/A":
        print(f"{YELLOW}N/A (could not verify){NC}")
    else:
        logs_blocks = current_dec - logs_earliest
        print(
            f"{YELLOW}Partial - from block ~{logs_earliest} ({logs_blocks:,} blocks, older logs not queryable){NC}"
        )
    print("Block receipts:        ", end="")
    if receipts_earliest == 1:
        print(f"{GREEN}✓ FULL (eth_getBlockReceipts works from block 1){NC}")
    elif receipts_earliest == "N/A":
        print(f"{YELLOW}N/A (could not verify){NC}")
    else:
        receipts_blocks = current_dec - receipts_earliest
        print(
            f"{YELLOW}Partial - from block ~{receipts_earliest} ({receipts_blocks:,} blocks, older receipts not queryable){NC}"
        )
    print()


if __name__ == "__main__":
    main()
