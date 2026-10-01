#!/usr/bin/env python3
#
# Benchmark an EVM JSON-RPC endpoint with eth_getBlockByNumber calls on random blocks,
# or (--logs SPAN) with unfiltered eth_getLogs over random SPAN-block windows.
# Reports throughput, latency percentiles, and error breakdown.

import argparse
import random
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
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


def display_url(url: str) -> str:
    """Show scheme://host[:port] only, so API keys in the path/query are not printed."""
    scheme, _, rest = url.partition("://")
    host = rest.split("/", 1)[0].split("?", 1)[0]
    if "@" in host:
        host = host.split("@", 1)[1]
    suffix = "/..." if len(rest) > len(host) else ""
    return f"{scheme}://{host}{suffix}"


def parse_args() -> argparse.Namespace:
    script = sys.argv[0].split("/")[-1]
    parser = argparse.ArgumentParser(
        prog=script,
        description="Benchmark an EVM JSON-RPC endpoint with eth_getBlockByNumber on random block numbers, "
        "or with unfiltered eth_getLogs over random block windows (--logs). "
        "Reports throughput, latency percentiles (p50/p90/p99), and errors.",
        epilog="""Block range:
  Default is 1 → latest. Use --recent N for the last N blocks (useful on pruned nodes),
  or --from/--to for an explicit range. Blocks returning null are counted as "missing".
  With --logs SPAN, each request is eth_getLogs (no address/topics) over
  [start, start+SPAN-1], with start random so the whole window stays inside the range.

Examples:
  %(prog)s 8545
  %(prog)s -n 5000 -c 32 http://localhost:8545
  %(prog)s -d 60 -c 16 --full localhost:8545
  %(prog)s --recent 100000 -c 8 8545
  %(prog)s --from 1000000 --to 2000000 --seed 42 8545
  %(prog)s --logs 100 --recent 100000 -c 8 8545""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "rpc_url",
        help="JSON-RPC endpoint (port only, host:port, or full http(s) URL)",
    )
    parser.add_argument(
        "-n", "--requests",
        type=int,
        default=1000,
        metavar="N",
        help="Total number of requests (default: %(default)s; ignored with --duration)",
    )
    parser.add_argument(
        "-d", "--duration",
        type=float,
        metavar="SECS",
        help="Run for SECS seconds instead of a fixed request count",
    )
    parser.add_argument(
        "-c", "--concurrency",
        type=int,
        default=10,
        metavar="N",
        help="Number of concurrent workers (default: %(default)s)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Request full transaction objects (second param true)",
    )
    parser.add_argument(
        "--logs",
        type=int,
        metavar="SPAN",
        help="Benchmark unfiltered eth_getLogs over random SPAN-block windows instead of eth_getBlockByNumber",
    )
    parser.add_argument(
        "--from",
        dest="from_block",
        type=int,
        metavar="BLOCK",
        help="Lowest block number to sample (default: 1)",
    )
    parser.add_argument(
        "--to",
        dest="to_block",
        type=int,
        metavar="BLOCK",
        help="Highest block number to sample (default: latest)",
    )
    parser.add_argument(
        "--recent",
        type=int,
        metavar="N",
        help="Sample only the last N blocks (overrides --from)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        help="Random seed for reproducible block selection",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=0,
        metavar="N",
        help="Warmup requests per worker, excluded from stats (default: %(default)s)",
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
        help="Print every failed request",
    )
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.duration is None and args.requests < 1:
        parser.error("--requests must be >= 1")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be > 0")
    if args.recent is not None and args.recent < 1:
        parser.error("--recent must be >= 1")
    if args.logs is not None and args.logs < 1:
        parser.error("--logs must be >= 1")
    if args.logs is not None and args.full:
        parser.error("--full only applies to eth_getBlockByNumber, not --logs")
    return args


@dataclass
class Stats:
    latencies: list[float] = field(default_factory=list)  # successful requests, seconds
    errors: Counter = field(default_factory=Counter)
    ok: int = 0
    missing: int = 0
    items: int = 0  # txs (getBlock) or logs (getLogs) in successful responses
    bytes: int = 0
    last_done: float = 0.0  # perf_counter() when the last request finished
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def total(self) -> int:
        return self.ok + self.missing + sum(self.errors.values())


def make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=pool_size)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def get_latest_block(session: requests.Session, url: str, timeout: int) -> Optional[int]:
    payload = {"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}
    try:
        r = session.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return int(r.json()["result"], 16)
    except Exception:
        return None


def rpc_call(
    session: requests.Session, url: str, payload: dict, timeout: int
) -> tuple[str, float, int, object, str]:
    """Returns (kind, elapsed_sec, bytes, result, detail). kind: ok | missing | error label."""
    t0 = time.perf_counter()
    try:
        r = session.post(url, json=payload, timeout=timeout)
        elapsed = time.perf_counter() - t0
        size = len(r.content)
        if r.status_code != 200:
            return (f"HTTP {r.status_code}", elapsed, size, None, r.text[:200])
        data = r.json()
    except requests.Timeout:
        return ("timeout", time.perf_counter() - t0, 0, None, "")
    except requests.ConnectionError as e:
        return ("connection error", time.perf_counter() - t0, 0, None, str(e)[:200])
    except ValueError as e:
        return ("invalid JSON", time.perf_counter() - t0, 0, None, str(e)[:200])
    except Exception as e:
        return (type(e).__name__, time.perf_counter() - t0, 0, None, str(e)[:200])

    err = data.get("error")
    if err is not None:
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        code = err.get("code") if isinstance(err, dict) else None
        label = f"RPC error {code}" if code is not None else "RPC error"
        return (label, elapsed, size, None, msg[:200])
    result = data.get("result")
    if result is None:
        return ("missing", elapsed, size, None, "")
    return ("ok", elapsed, size, result, "")


def get_block(
    session: requests.Session, url: str, block: int, full: bool, timeout: int
) -> tuple[str, float, int, int, str]:
    """Returns (kind, elapsed_sec, bytes, tx_count, detail)."""
    payload = {"jsonrpc": "2.0", "method": "eth_getBlockByNumber", "params": [hex(block), full], "id": 1}
    kind, elapsed, size, result, detail = rpc_call(session, url, payload, timeout)
    ntx = len(result.get("transactions") or []) if isinstance(result, dict) else 0
    return (kind, elapsed, size, ntx, detail)


def get_logs(
    session: requests.Session, url: str, start: int, span: int, timeout: int
) -> tuple[str, float, int, int, str]:
    """Unfiltered eth_getLogs over [start, start+span-1]. Returns (kind, elapsed_sec, bytes, log_count, detail)."""
    params = [{"fromBlock": hex(start), "toBlock": hex(start + span - 1)}]
    payload = {"jsonrpc": "2.0", "method": "eth_getLogs", "params": params, "id": 1}
    kind, elapsed, size, result, detail = rpc_call(session, url, payload, timeout)
    nlogs = len(result) if isinstance(result, list) else 0
    return (kind, elapsed, size, nlogs, detail)


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def main() -> None:
    args = parse_args()
    url = normalize_rpc_url(args.rpc_url)
    session = make_session(args.concurrency)

    method = "eth_getLogs" if args.logs is not None else "eth_getBlockByNumber"
    print(f"{CYAN}=== {method} Benchmark ==={NC}")
    print(f"RPC: {display_url(url)}")

    latest = get_latest_block(session, url, args.timeout)
    if latest is None:
        print(f"{RED}ERROR: Cannot connect to RPC or get block number{NC}")
        sys.exit(1)

    hi = min(args.to_block, latest) if args.to_block is not None else latest
    if args.recent is not None:
        lo = max(0, hi - args.recent + 1)
    else:
        lo = args.from_block if args.from_block is not None else 1
    if lo > hi:
        print(f"{RED}ERROR: Empty block range {lo} → {hi} (latest: {latest}){NC}")
        sys.exit(1)
    span = args.logs or 1  # blocks per request
    if hi - lo + 1 < span:
        print(f"{RED}ERROR: --logs {span} is wider than the block range {lo:,} → {hi:,} ({hi - lo + 1:,} blocks){NC}")
        sys.exit(1)
    max_start = hi - span + 1

    mode = f"{args.duration:g}s" if args.duration is not None else f"{args.requests:,} requests"
    print(f"Latest block: {latest:,}")
    print(f"Block range:  {lo:,} → {hi:,} ({hi - lo + 1:,} blocks)")
    if args.logs is not None:
        print(f"Run:          {mode}, {args.concurrency} workers, {span:,} blocks per eth_getLogs (unfiltered)")
    else:
        print(f"Run:          {mode}, {args.concurrency} workers, full txs: {'yes' if args.full else 'no'}")
    if args.seed is not None:
        print(f"Seed:         {args.seed}")
    print()

    stats = Stats()
    issued = 0
    issued_lock = threading.Lock()
    stop = threading.Event()
    base_rng = random.Random(args.seed)
    worker_seeds = [base_rng.randrange(2**63) for _ in range(args.concurrency)]

    def take_slot() -> bool:
        nonlocal issued
        if stop.is_set():
            return False
        if args.duration is not None:
            return True
        with issued_lock:
            if issued >= args.requests:
                return False
            issued += 1
            return True

    def fetch(start: int) -> tuple[str, float, int, int, str]:
        if args.logs is not None:
            return get_logs(session, url, start, span, args.timeout)
        return get_block(session, url, start, args.full, args.timeout)

    def worker(idx: int) -> None:
        rng = random.Random(worker_seeds[idx])
        for _ in range(args.warmup):
            fetch(rng.randint(lo, max_start))
        start_barrier.wait()
        while take_slot():
            block = rng.randint(lo, max_start)
            kind, elapsed, size, nitems, detail = fetch(block)
            with stats.lock:
                stats.last_done = time.perf_counter()
                stats.bytes += size
                if kind == "ok":
                    stats.ok += 1
                    stats.items += nitems
                    stats.latencies.append(elapsed)
                elif kind == "missing":
                    stats.missing += 1
                    stats.latencies.append(elapsed)
                else:
                    stats.errors[kind] += 1
            if args.verbose and kind not in ("ok", "missing"):
                where = f"blocks {block}-{block + span - 1}" if args.logs is not None else f"block {block}"
                print(f"\n  {RED}✗ {where}: {kind}{NC} {detail}")

    start_barrier = threading.Barrier(args.concurrency + 1)
    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.concurrency)]
    for t in threads:
        t.start()
    if args.warmup:
        print(f"Warming up ({args.warmup} requests per worker)...")
    start_barrier.wait()
    t_start = time.perf_counter()
    deadline = t_start + args.duration if args.duration is not None else None

    try:
        while any(t.is_alive() for t in threads):
            wait = 0.5
            if deadline is not None and not stop.is_set():
                wait = max(0.0, min(wait, deadline - time.perf_counter()))
            time.sleep(wait)
            now = time.perf_counter()
            if deadline is not None and now >= deadline:
                stop.set()
            with stats.lock:
                done = stats.total
                errs = sum(stats.errors.values())
            rate = done / (now - t_start) if now > t_start else 0.0
            target = f"/{args.requests:,}" if args.duration is None else ""
            print(
                f"\r  {done:,}{target} requests | {rate:,.1f} req/s | errors: {errs:,}   ",
                end="",
                flush=True,
            )
    except KeyboardInterrupt:
        stop.set()
        print(f"\n{YELLOW}Interrupted — waiting for in-flight requests...{NC}", end="")
        for t in threads:
            t.join(timeout=args.timeout + 1)
    # Measure to the last completed request, not to when the progress loop noticed
    wall = (stats.last_done or time.perf_counter()) - t_start
    print("\n")

    # Summary
    lat = sorted(stats.latencies)
    total = stats.total
    errs = sum(stats.errors.values())
    print(f"{CYAN}=== Summary ==={NC}")
    print(f"Requests:     {total:,} in {wall:.2f}s")
    print(f"Throughput:   {GREEN}{total / wall:,.1f} req/s{NC}" if wall > 0 else "Throughput:   n/a")
    print(f"Success:      {stats.ok:,} ({stats.ok / total * 100:.1f}%)" if total else "Success:      0")
    if stats.missing:
        print(f"Missing:      {YELLOW}{stats.missing:,} (null result — pruned or unavailable){NC}")
    if errs:
        print(f"Errors:       {RED}{errs:,} ({errs / total * 100:.1f}%){NC}")
        for kind, count in stats.errors.most_common():
            print(f"  {kind}: {count:,}")
    else:
        print(f"Errors:       {GREEN}0{NC}")
    if stats.ok and args.logs is not None:
        print(f"Logs:         {stats.items:,} ({stats.items / stats.ok:,.1f}/req, {stats.items / stats.ok / span:,.1f}/blk avg)")
    elif stats.ok:
        print(f"Avg txs/blk:  {stats.items / stats.ok:,.1f}")
    if total:
        print(f"Data:         {stats.bytes / 1_048_576:,.2f} MiB ({stats.bytes / total / 1024:,.1f} KiB/req avg)")
    print()

    if lat:
        print(f"{CYAN}=== Latency (successful + missing responses) ==={NC}")
        print(f"  min:  {lat[0] * 1000:8.1f} ms")
        print(f"  avg:  {sum(lat) / len(lat) * 1000:8.1f} ms")
        for p in (50, 90, 95, 99):
            print(f"  p{p}:  {percentile(lat, p) * 1000:8.1f} ms")
        print(f"  max:  {lat[-1] * 1000:8.1f} ms")
        print()

    sys.exit(1 if total == 0 or errs == total else 0)


if __name__ == "__main__":
    main()
