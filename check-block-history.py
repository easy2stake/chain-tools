#!/usr/bin/env python3
#
# Fast block history and tx indexer checker for any EVM-compatible chain.
# Uses binary search to find block history, tx indexer, and archival boundaries.

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

ProbeKind = Literal["skip", "error", "empty", "unavailable", "data"]

# Per-method RPC timing (method -> list of response times in seconds)
_rpc_timings: dict[str, list[float]] = {}

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


def normalize_rpc_url(url: str) -> str:
    """If only a port is given, default to 127.0.0.1:port; add http:// when omitted."""
    if url.isdigit():
        url = f"127.0.0.1:{url}"
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


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
  %(prog)s localhost:8545
  %(prog)s 8545
  %(prog)s -t 5 8545""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "rpc_url",
        help="JSON-RPC endpoint (port only, host:port, or full http(s) URL)",
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
RPC_URL = normalize_rpc_url(_args.rpc_url)
VERBOSE = _args.verbose
TIMEOUT = _args.timeout


def _record_rpc_time(method: str, elapsed_sec: float) -> None:
    if method not in _rpc_timings:
        _rpc_timings[method] = []
    _rpc_timings[method].append(elapsed_sec)


def rpc(method: str, *params) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "method": method, "params": list(params), "id": 1}
    t0 = time.perf_counter()
    try:
        if VERBOSE:
            print(f"  {YELLOW}[RPC] → {method}{NC} {json.dumps(list(params))}")
        r = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        _record_rpc_time(method, time.perf_counter() - t0)
        if VERBOSE:
            out = json.dumps(data)
            if len(out) > 200:
                out = out[:200] + "..."
            print(f"  {YELLOW}[RPC] ← {method}{NC} {out}")
        return data.get("result")
    except Exception as e:
        _record_rpc_time(method, time.perf_counter() - t0)
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
            t0 = time.perf_counter()
            if VERBOSE:
                print(f"  {YELLOW}[RPC] → eth_getLogs{NC} {json.dumps(params)}")
            resp = requests.post(RPC_URL, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            _record_rpc_time("eth_getLogs", time.perf_counter() - t0)
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
            _record_rpc_time("eth_getLogs", time.perf_counter() - t0)
            if VERBOSE:
                print(f"  {RED}[RPC] ✗ eth_getLogs{NC} {e}")
            last_error = {"message": str(e)}
            if params is filter_obj:
                break
    return (None, last_error)


@dataclass
class ProbeResult:
    """Result of probing index availability at a block height."""

    works: bool  # method available (empty or data counts as works)
    kind: ProbeKind
    probed_block: Optional[int] = None


@dataclass
class ZoneSamples:
    """Block numbers seen for each response kind during binary search (sampled, not exhaustive)."""

    error: list[int] = field(default_factory=list)
    empty: list[int] = field(default_factory=list)
    unavailable: list[int] = field(default_factory=list)
    data: list[int] = field(default_factory=list)

    def record(self, result: ProbeResult) -> None:
        if result.probed_block is None or result.kind == "skip":
            return
        bucket = getattr(self, result.kind)
        bucket.append(result.probed_block)

    def infer_zone_summary(self, current_dec: int) -> Optional[str]:
        """Best-effort zone line: error | empty [] | with data (from probe samples)."""
        error_hi = max(self.error + self.unavailable) if (self.error or self.unavailable) else None
        empty_blocks = self.empty
        data_lo = min(self.data) if self.data else None

        if error_hi is None and not empty_blocks and data_lo is None:
            return None

        parts: list[str] = []
        if error_hi is not None:
            parts.append(f"1–{error_hi:,}: error/unavailable")
        if empty_blocks:
            empty_lo = (error_hi + 1) if error_hi is not None else min(empty_blocks)
            empty_hi = max(empty_blocks)
            if data_lo is not None:
                empty_hi = min(empty_hi, data_lo - 1)
            if empty_lo <= empty_hi:
                parts.append(f"{empty_lo:,}–{empty_hi:,}: empty []")
        if data_lo is not None:
            parts.append(f"{data_lo:,}–{current_dec:,}: with data")
        return " | ".join(parts) if parts else None

    def format_details(self, current_dec: int) -> list[str]:
        lines: list[str] = []
        summary = self.infer_zone_summary(current_dec)
        if summary:
            lines.append(f"  Zones (sampled): {summary}")
        if self.error:
            lo, hi = min(self.error), max(self.error)
            lines.append(f"  Error RPC:       blocks {lo:,}–{hi:,}")
        if self.unavailable:
            lo, hi = min(self.unavailable), max(self.unavailable)
            lines.append(f"  Unavailable []:  blocks {lo:,}–{hi:,} (tx block, likely pruned)")
        if self.empty:
            lo, hi = min(self.empty), max(self.empty)
            lines.append(f"  Empty []:        blocks {lo:,}–{hi:,} (RPC OK, no events)")
        if self.data:
            lo, hi = min(self.data), max(self.data)
            lines.append(f"  With data:       blocks {lo:,}–{hi:,}")
        return lines


def logs_probe_at_block(
    block_num: int, current_dec: int, radius: int = 20
) -> ProbeResult:
    """Probe eth_getLogs on a tx-bearing block near block_num."""
    pair = get_tx_from_block_or_nearby(block_num, radius, current_dec)
    if pair is None:
        return ProbeResult(works=False, kind="skip", probed_block=None)

    probed_block, _ = pair
    block = rpc("eth_getBlockByNumber", hex(probed_block), False)
    if not block or "hash" not in block:
        return ProbeResult(works=False, kind="error", probed_block=probed_block)

    logs_result, _err = _eth_get_logs_by_block_hash(block["hash"])
    if logs_result is None:
        return ProbeResult(works=False, kind="error", probed_block=probed_block)

    if isinstance(logs_result, list) and len(logs_result) > 0:
        return ProbeResult(works=True, kind="data", probed_block=probed_block)

    receipts = rpc("eth_getBlockReceipts", hex(probed_block))
    if isinstance(receipts, list) and len(receipts) > 0:
        return ProbeResult(works=True, kind="empty", probed_block=probed_block)

    return ProbeResult(works=False, kind="unavailable", probed_block=probed_block)


def receipts_probe_at_block(
    block_num: int, current_dec: int, radius: int = 20
) -> ProbeResult:
    """Probe eth_getBlockReceipts on a tx-bearing block near block_num."""
    pair = get_tx_from_block_or_nearby(block_num, radius, current_dec)
    if pair is None:
        return ProbeResult(works=False, kind="skip", probed_block=None)

    probed_block, _ = pair
    result = rpc("eth_getBlockReceipts", hex(probed_block))
    if result is None:
        return ProbeResult(works=False, kind="error", probed_block=probed_block)

    if isinstance(result, list) and len(result) > 0:
        return ProbeResult(works=True, kind="data", probed_block=probed_block)

    return ProbeResult(works=False, kind="unavailable", probed_block=probed_block)


def binary_search_index_boundary(
    earliest_block: int,
    current_dec: int,
    probe: Callable[[int], ProbeResult],
    zones: ZoneSamples,
) -> int:
    """Binary search for earliest block where probe.works is True."""
    low, high = earliest_block, current_dec
    while low < high:
        mid = (low + high) // 2
        result = probe(mid)
        zones.record(result)
        if result.works:
            high = mid
        else:
            low = mid + 1
    return low


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
    print("      Info: Probes tx-bearing blocks; [] on a tx block = unavailable (pruned).")
    print("      Tracks error vs empty [] vs data responses from sampled probes.")
    logs_earliest = "N/A"
    receipts_earliest = "N/A"
    logs_zones = ZoneSamples()
    receipts_zones = ZoneSamples()
    lr_iterations = 0

    head_logs = logs_probe_at_block(current_dec, current_dec)
    lr_iterations += 1
    logs_zones.record(head_logs)
    if not head_logs.works:
        kind_label = head_logs.kind if head_logs.kind != "skip" else "error/unavailable"
        print(
            f"      {YELLOW}eth_getLogs at latest block failed ({kind_label}) — skipping log index search{NC}"
        )
    else:
        logs_earliest = binary_search_index_boundary(
            earliest_block,
            current_dec,
            lambda mid: logs_probe_at_block(mid, current_dec),
            logs_zones,
        )
        if logs_earliest == 1 or (earliest_block == 1 and logs_earliest <= 1000):
            logs_earliest = 1
        if logs_earliest == 1:
            print(f"      {GREEN}✓ Full log index from block 1 (eth_getLogs works for entire chain){NC}")
        else:
            print(f"      {YELLOW}Log index (eth_getLogs) available from block ~{logs_earliest}{NC}")
        for line in logs_zones.format_details(current_dec):
            print(f"        {line.strip()}")

    head_receipts = receipts_probe_at_block(current_dec, current_dec)
    lr_iterations += 1
    receipts_zones.record(head_receipts)
    if not head_receipts.works:
        kind_label = head_receipts.kind if head_receipts.kind != "skip" else "error/unavailable"
        print(
            f"      {YELLOW}eth_getBlockReceipts at latest block failed ({kind_label}) — skipping receipts search{NC}"
        )
    else:
        receipts_earliest = binary_search_index_boundary(
            earliest_block,
            current_dec,
            lambda mid: receipts_probe_at_block(mid, current_dec),
            receipts_zones,
        )
        if receipts_earliest == 1 or (earliest_block == 1 and receipts_earliest <= 1000):
            receipts_earliest = 1
        if receipts_earliest == 1:
            print(
                f"      {GREEN}✓ Full block receipts from block 1 (eth_getBlockReceipts works for entire chain){NC}"
            )
        else:
            print(
                f"      {YELLOW}Block receipts (eth_getBlockReceipts) available from block ~{receipts_earliest}{NC}"
            )
        for line in receipts_zones.format_details(current_dec):
            print(f"        {line.strip()}")

    print(f"      (used ~{lr_iterations}+ queries)")
    print()

    # 5. Summary
    print(f"{CYAN}[5/5]{NC} Summary")
    print(f"{CYAN}=== Summary ==={NC}")
    print("(Log/receipt zones = inferred from sampled probes during binary search, not exhaustive scans)")
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
    if isinstance(logs_earliest, int):
        for line in logs_zones.format_details(current_dec):
            print(line)
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
    if isinstance(receipts_earliest, int):
        for line in receipts_zones.format_details(current_dec):
            print(line)
    print()
    # Average RPC response time by method
    if _rpc_timings:
        print(f"{CYAN}=== RPC avg response time (by method) ==={NC}")
        for method in sorted(_rpc_timings.keys()):
            times = _rpc_timings[method]
            avg_ms = (sum(times) / len(times)) * 1000
            print(f"  {method}: {avg_ms:.1f} ms avg ({len(times)} calls)")
        print()


if __name__ == "__main__":
    main()
