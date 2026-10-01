#!/usr/bin/env python3
#
# Quick health checks for blockchain nodes: EVM execution clients (chain ID, peers, sync status,
# latest/safe/finalized/earliest blocks), OP node, Tendermint/CometBFT, Aptos and beacon (consensus
# layer) REST APIs. One-shot, or *_monitor to refresh every --interval seconds.
#
# All requests share one keep-alive HTTP session. Connection setup (TCP + TLS) is paid once in an
# untimed warm-up request, so ReqTime shows request latency rather than handshake cost.

import argparse
import io
import json
import re
import sys
import time
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("Error: requests required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

# Mainnet genesis time fallback when /eth/v1/beacon/genesis is unavailable.
BEACON_GENESIS_TIME_MAINNET = 1606824023
BEACON_SLOT_SECONDS = 12

# Rolling window for blocks/sec and time-to-sync estimates (monitor mode).
WINDOW_SEC = 60

# Canonical Reth pipeline stage order for display sorting.
RETH_STAGE_ORDER = [
    "Era", "Headers", "Header", "Bodies", "Body", "SenderRecovery", "Execution",
    "PruneSenderRecovery", "MerkleUnwind", "AccountHashing", "StorageHashing", "MerkleExecute",
    "MerkleChangeSets",
    "TransactionLookup", "IndexStorageHistory", "IndexAccountHistory", "Prune", "Finish",
]
# MerkleUnwind only unwinds, MerkleChangeSets is deprecated (checkpoint stays frozen), Finish and
# Era are bookkeeping: none of these is ever reported as the active stage.
RETH_NEVER_ACTIVE = {"Era", "MerkleUnwind", "MerkleChangeSets", "Finish"}

OP_SYNC_ROWS = [
    ("CurrentL1", "current_l1"), ("HeadL1", "head_l1"), ("SafeL1", "safe_l1"),
    ("FinalizedL1", "finalized_l1"), ("UnsafeL2", "unsafe_l2"), ("SafeL2", "safe_l2"),
    ("FinalizedL2", "finalized_l2"), ("EngineTarget", "engine_sync_target"),
]
OP_PEER_DIRECTIONS = ["unknown", "inbound", "outbound"]

HASH_COL = 66


# --- URL handling ---

def normalize_url(url: str) -> str:
    """If only a port is given, default to 127.0.0.1:port; add http:// when omitted."""
    url = url.strip()
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


# --- HTTP client ---

def ms(seconds: float) -> str:
    return f"{seconds * 1000:.0f}"


@dataclass
class Reply:
    ok: bool                       # transport OK, 2xx, and (for RPC) no JSON-RPC error
    data: Any                      # RPC result, or parsed JSON body for GET
    error: str                     # short reason when not ok (never contains the URL)
    elapsed: float                 # seconds, request sent -> body read
    status: Optional[int] = None   # HTTP status code, when a response arrived

    @property
    def ms(self) -> str:
        return ms(self.elapsed)


def short_error(exc: requests.RequestException) -> str:
    """Classify a requests exception without echoing its message (which includes the URL path)."""
    if isinstance(exc, requests.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "TLS error"
    if isinstance(exc, requests.ConnectionError):
        return "connection failed"
    return type(exc).__name__


class Client:
    """One keep-alive session per run; every request reuses the same connection when possible."""

    def __init__(self, url: str, timeout: float):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()
        self.session.mount("http://", HTTPAdapter(pool_connections=1, pool_maxsize=1))
        self.session.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1))
        self.warmup: Optional[Reply] = None

    def rpc(self, method: str, params: Optional[list] = None) -> Reply:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or [], "id": 1}
        start = time.perf_counter()
        try:
            r = self.session.post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            return Reply(False, None, short_error(e), time.perf_counter() - start)
        elapsed = time.perf_counter() - start
        try:
            body = r.json()
        except ValueError:
            return Reply(False, None, f"HTTP {r.status_code}, non-JSON response", elapsed, r.status_code)
        if isinstance(body, dict) and "error" in body:
            err = body["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            return Reply(False, None, f"RPC error: {msg}"[:80], elapsed, r.status_code)
        if r.status_code != 200 or not isinstance(body, dict):
            return Reply(False, None, f"HTTP {r.status_code}", elapsed, r.status_code)
        return Reply(True, body.get("result"), "", elapsed, r.status_code)

    def get(self, path: str = "") -> Reply:
        url = self.url.rstrip("/") + path if path else self.url
        start = time.perf_counter()
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as e:
            return Reply(False, None, short_error(e), time.perf_counter() - start)
        elapsed = time.perf_counter() - start
        try:
            data = r.json()
        except ValueError:
            data = None
        if not 200 <= r.status_code < 300:
            return Reply(False, data, f"HTTP {r.status_code}", elapsed, r.status_code)
        return Reply(True, data, "", elapsed, r.status_code)

    def warm_up(self, target: str) -> None:
        """Open the connection with an untimed request. target is "rpc" or a GET path."""
        self.warmup = self.rpc("web3_clientVersion") if target == "rpc" else self.get(target)


# --- Formatting helpers ---

def to_int(value: Any) -> Optional[int]:
    """Parse hex ("0x1a") or decimal ("26", 26) values; None if missing or invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(s, 16) if s.lower().startswith("0x") else int(s)
        except ValueError:
            return None
    return None


def show(value: Any) -> str:
    """Render a JSON value the way jq -r would, with "-" for missing."""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def dig(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def utc(ts: Optional[int]) -> str:
    if not ts or ts <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return "-"


def fmt_duration(sec: int) -> str:
    if sec <= 0:
        return "0s"
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    if sec < 86400:
        return f"{sec // 3600}h {sec % 3600 // 60}m"
    return f"{sec // 86400}d {sec % 86400 // 3600}h"


def age(ts: Optional[int]) -> str:
    """Time since a Unix timestamp, or "-" when unknown or in the future."""
    if not ts:
        return "-"
    delta = int(time.time()) - ts
    return "-" if delta < 0 else fmt_duration(delta)


def rfc3339_to_epoch(raw: Any) -> Optional[int]:
    """Parse RFC3339 (Z or offset, up to nanosecond fractions) to Unix seconds."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    v = raw.strip().replace("Z", "+00:00")
    if "." in v:
        base, rest = v.split(".", 1)
        m = re.match(r"(\d+)(.*)", rest)
        if m:
            frac, tz = m.groups()
            v = f"{base}.{(frac + '000000')[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def table(widths: list, headers: list, rows: list) -> None:
    """Left-aligned columns; a width of None leaves the column unpadded."""
    def line(cols):
        return " ".join(f"{show(c):<{w}}" if w else show(c) for c, w in zip(cols, widths)).rstrip()
    print(line(headers))
    print(line(["-" * (w or len(h)) for w, h in zip(widths, headers)]))
    for row in rows:
        print(line(row))


# --- Rate / ETA tracking (kept in memory across monitor refreshes) ---

class Window:
    """(wall clock, value) samples from the last WINDOW_SEC seconds."""

    def __init__(self, seconds: int = WINDOW_SEC):
        self.seconds = seconds
        self.samples: deque = deque()

    def add(self, value: int) -> Optional[tuple]:
        """Record a sample; return (seconds, value delta) between oldest and newest, if any."""
        now = time.time()
        self.samples.append((now, value))
        while now - self.samples[0][0] > self.seconds:
            self.samples.popleft()
        (t0, v0), (t1, v1) = self.samples[0], self.samples[-1]
        if t1 <= t0:
            return None
        return t1 - t0, v1 - v0


@dataclass
class Ctx:
    client: Client
    rate: Window
    eta: Window
    errors: list = field(default_factory=list)  # printed in one section after the data

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def check(self, what: str, r: Reply) -> Reply:
        """Record a failed request under `what` (method or path); return the reply unchanged."""
        if not r.ok:
            self.error(f"{what}: {r.error}")
        return r


def print_blocks_per_sec(ctx: Ctx, height: int) -> None:
    delta = ctx.rate.add(height)
    bps = f"{delta[1] / delta[0]:.4f}" if delta and delta[1] >= 0 else "-"
    print(f"\nBlocks/sec: {bps}")


def print_time_to_sync(ctx: Ctx, block_ts: int) -> None:
    """ETA from block timestamps: chain-seconds gained per wall-second vs. the gap to now."""
    delta = ctx.eta.add(block_ts)
    gap = time.time() - block_ts
    eta = "-"
    if gap <= 0:
        eta = "synced"
    elif delta and delta[1] > 0:
        catch_up = delta[1] / delta[0]
        eta = fmt_duration(int(gap / (catch_up - 1))) if catch_up > 1 else "∞"
    print(f"\nTime to sync: {eta}")


# --- EVM (general_check / monitor) ---

@dataclass
class BlockRow:
    label: str
    ts: Optional[int]
    hex: str
    number: Optional[int]
    hash: str
    ms: str

    def cols(self) -> list:
        return [self.label, utc(self.ts), age(self.ts), self.hex, show(self.number), self.hash, self.ms]


def resolve_chain_identity(ctx: Ctx) -> tuple:
    """EVM: eth_chainId (hex, dec). Substrate nodes often lack it: fall back to system_chain name.
    Returns (hex_or_name, int_or_name, elapsed, mode)."""
    c = ctx.client
    r = c.rpc("eth_chainId")
    cid = to_int(r.data) if r.ok else None
    if cid is not None:
        return r.data, str(cid), r.elapsed, "evm"
    s = c.rpc("system_chain")
    if s.ok and isinstance(s.data, str) and s.data:
        return s.data, s.data, r.elapsed + s.elapsed, "substrate"
    ctx.error(f"eth_chainId: {r.error or 'null result'}")
    return None, None, r.elapsed, "evm"


def evm_block_row(ctx: Ctx, label: str, tag: str) -> BlockRow:
    # Header-only (false): only number, hash and timestamp are needed.
    r = ctx.check(f"eth_getBlockByNumber({tag})", ctx.client.rpc("eth_getBlockByNumber", [tag, False]))
    block = r.data if r.ok and isinstance(r.data, dict) else None
    number = to_int(block.get("number")) if block else None
    if number is None:
        if r.ok:
            ctx.error(f"eth_getBlockByNumber({tag}): null result (block not available)")
        return BlockRow(label, None, "-", None, "-", r.ms)
    return BlockRow(label, to_int(block.get("timestamp")), block["number"], number,
                    block.get("hash") or "-", r.ms)


def substrate_block_rows(ctx: Ctx) -> list:
    """Substrate (e.g. Bittensor): best header + its hash; safe/finalized/earliest not shown."""
    c = ctx.client
    h = ctx.check("chain_getHeader", c.rpc("chain_getHeader"))
    number_hex = dig(h.data, "number") if h.ok else None
    number = to_int(number_hex)
    if number is None:
        if h.ok:
            ctx.error("chain_getHeader: no block number in result")
        latest = BlockRow("Latest", None, "-", None, "-", h.ms)
    else:
        bh = ctx.check("chain_getBlockHash", c.rpc("chain_getBlockHash", [number]))
        block_hash = bh.data if bh.ok and isinstance(bh.data, str) and bh.data else "-"
        latest = BlockRow("Latest", None, number_hex, number, block_hash, ms(h.elapsed + bh.elapsed))
    return [latest] + [BlockRow(label, None, "-", None, "-", "-") for label in ("Safe", "Finalized", "Earliest")]


def stage_sort_key(name: str) -> int:
    return RETH_STAGE_ORDER.index(name) if name in RETH_STAGE_ORDER else 999


def print_sync_status(ctx: Ctx, r: Reply) -> None:
    """eth_syncing: Reth stages table, or Geth/Erigon-style summary."""
    if not r.ok or r.data is None:
        print("Sync status: unknown")
        ctx.error(f"eth_syncing: {r.error or 'null result'}")
        return
    req = f" (req {r.ms}ms)"
    res = r.data
    if isinstance(res, bool):
        print(f"Sync status: synced{req}")
        print("  (eth_syncing=false; Reth may report this before sync fully completes)")
        return
    if not isinstance(res, dict):
        print("Sync status: unknown")
        ctx.error("eth_syncing: unexpected result")
        return

    starting = res.get("startingBlock", res.get("starting_block"))
    current = res.get("currentBlock", res.get("current_block"))
    highest = res.get("highestBlock", res.get("highest_block"))
    starting_i, current_i, highest_i = to_int(starting), to_int(current), to_int(highest)
    print(f"Sync status: syncing{req}")

    stages = res.get("stages")
    if isinstance(stages, list) and stages:
        print(f"  starting: {show(starting_i)}  current: {show(current_i)}  highest: {show(highest_i)}")
        entries = sorted(
            (
                (stage_sort_key(s["name"]), s["name"], s.get("block"), to_int(s.get("block")))
                for s in stages
                if isinstance(s, dict) and s.get("name")
            ),
            key=lambda e: (e[0], e[1]),
        )
        stage_max = max((e[3] for e in entries if e[3] is not None), default=0)
        ref_height = max(stage_max, highest_i or 0)
        changesets = next((e[3] for e in entries if e[1] == "MerkleChangeSets"), None)

        # Active stage: earliest stage in pipeline order with real progress that is still behind
        # the furthest checkpoint. A stage at block 0 is unused rather than running.
        active = None
        if stage_max > 0:
            for _, name, _, block in entries:
                if name not in RETH_NEVER_ACTIVE and block is not None and 0 < block < stage_max:
                    active = (name, block)
                    break
        elif highest_i:
            active = ("Headers", 0)

        if active:
            print(f"  active stage: {active[0]} (block {active[1]})")
        if changesets is not None and changesets < stage_max:
            print("  (MerkleChangeSets is deprecated in Reth; a stale checkpoint here is not sync lag)")

        print()
        rows = []
        for _, name, raw, block in entries:
            label = f"{name} *" if active and name == active[0] else name
            pct = f"{100 * block / ref_height:.2f}%" if ref_height > 0 and block is not None else "-"
            rows.append([label, raw, show(block), pct])
        table([24, 14, 14, 12], ["Stage", "Block (hex)", "Block (dec)", "vs highest"], rows)
        return

    pct = f"{100 * current_i / highest_i:.2f}%" if highest_i and current_i is not None else "-"
    print(f"  starting: {show(starting)} ({show(starting_i)})  current: {show(current)} ({show(current_i)})"
          f"  highest: {show(highest)} ({show(highest_i)})  progress: {pct}")
    if "healedTrienodes" in res or "syncedAccounts" in res:
        print("  (snap-sync / state-healing fields present; see eth_syncing for full detail)")


def general_check(ctx: Ctx) -> int:
    c = ctx.client
    chain_hex, chain_int, chain_elapsed, mode = resolve_chain_identity(ctx)
    peers = ctx.check("net_peerCount", c.rpc("net_peerCount"))
    peers_int = to_int(peers.data) if peers.ok else None

    print()
    table([16, 14, 8, 18], ["Chain ID (hex)", "Chain ID (int)", "Peers", "ReqTime(ms)"],
          [[chain_hex, chain_int, peers_int, f"chain:{ms(chain_elapsed)} peers:{peers.ms}"]])

    print()
    print_sync_status(ctx, c.rpc("eth_syncing"))

    if mode == "substrate":
        rows = substrate_block_rows(ctx)
    else:
        rows = [evm_block_row(ctx, label, label.lower()) for label in ("Latest", "Safe", "Finalized", "Earliest")]

    print()
    table([10, 20, 12, 12, 10, HASH_COL, 10],
          ["Row", "BlockTime", "Block Age", "Block(hex)", "Block(dec)", "BlockHash", "ReqTime(ms)"],
          [row.cols() for row in rows])

    latest = rows[0]
    if latest.number is not None:
        print_blocks_per_sec(ctx, latest.number)
    if latest.ts:
        print_time_to_sync(ctx, latest.ts)
    return 0 if latest.number is not None else 1


# --- OP node ---

def op_check(ctx: Ctx) -> int:
    """Rollup chain IDs, version, peers (opp2p_peerStats, fallback opp2p_peers), optimism_syncStatus."""
    c = ctx.client
    rollup = ctx.check("optimism_rollupConfig", c.rpc("optimism_rollupConfig"))
    version = ctx.check("optimism_version", c.rpc("optimism_version"))
    peers = c.rpc("opp2p_peerStats")
    connected = dig(peers.data, "connected") if peers.ok else None
    peers_elapsed = peers.elapsed
    if to_int(connected) is None:
        # Older/newer variants expose totalConnected in opp2p_peers instead.
        fallback = c.rpc("opp2p_peers", [True])
        peers_elapsed += fallback.elapsed
        connected = dig(fallback.data, "totalConnected") if fallback.ok else None
        if to_int(connected) is None:
            ctx.error(f"opp2p_peerStats: {peers.error or 'no connected count'}; "
                      f"opp2p_peers: {fallback.error or 'no totalConnected'}")

    print()
    table([12, 12, 22, 8, 34], ["L1 Chain ID", "L2 Chain ID", "Version", "Peers", "ReqTime(ms)"],
          [[dig(rollup.data, "l1_chain_id"), dig(rollup.data, "l2_chain_id"),
            version.data if version.ok else None, connected,
            f"rollup:{rollup.ms} version:{version.ms} peers:{ms(peers_elapsed)}"]])

    sync = c.rpc("optimism_syncStatus")
    print(f"\nop-node sync status (optimism_syncStatus req {sync.ms}ms)")
    if not sync.ok or not isinstance(sync.data, dict):
        ctx.error(f"optimism_syncStatus: {sync.error or 'invalid response'}")
        return 1

    rows = []
    for label, key in OP_SYNC_ROWS:
        ts = to_int(dig(sync.data, key, "timestamp"))
        rows.append([label, utc(ts), age(ts), dig(sync.data, key, "number"), dig(sync.data, key, "hash")])
    print()
    table([12, 20, 12, 12, HASH_COL], ["Row", "BlockTime", "Block Age", "Block(dec)", "BlockHash"], rows)

    def gap(hi_key: str, lo_key: str) -> str:
        hi, lo = to_int(dig(sync.data, hi_key, "number")), to_int(dig(sync.data, lo_key, "number"))
        return str(hi - lo) if hi is not None and lo is not None and hi >= lo else "-"

    print(f"\nSync gaps: l1_head-current_l1={gap('head_l1', 'current_l1')} blocks, "
          f"l2_unsafe-safe={gap('unsafe_l2', 'safe_l2')} blocks")
    return 0


def op_peers(ctx: Ctx) -> int:
    """Connected OP node peers via opp2p_peers (params [true] = connected only)."""
    r = ctx.client.rpc("opp2p_peers", [True])
    print(f"\nOP node peers via opp2p_peers (req {r.ms}ms)")
    if not r.ok or not isinstance(r.data, dict):
        ctx.error(f"opp2p_peers: {r.error or 'invalid response'}")
        return 1

    peers = r.data.get("peers") or {}
    total = r.data.get("totalConnected", len(peers))
    print(f"OP Peers connected: {total}")
    if not peers:
        return 0

    rows = []
    for key, p in peers.items():
        direction = p.get("direction")
        if isinstance(direction, int) and not isinstance(direction, bool):
            direction = OP_PEER_DIRECTIONS[direction] if 0 <= direction < len(OP_PEER_DIRECTIONS) else direction
        rows.append([p.get("peerID") or key, direction, (p.get("addresses") or ["-"])[0], p.get("userAgent")])
    table([54, 9, 46, None], ["Peer ID", "Direction", "Address", "User Agent"], rows)

    print("\nOP Peer ENRs:")
    for p in peers.values():
        if p.get("ENR"):
            print(p["ENR"])
    return 0


# --- Tendermint / CometBFT ---

def tendermint_check(ctx: Ctx) -> int:
    """/status for sync_info and node metadata, /net_info for peers and listener state."""
    c = ctx.client
    status = c.get("/status")
    if not status.ok or not isinstance(status.data, dict):
        ctx.error(f"/status: {status.error or 'non-JSON response'}")
        return 1
    net = ctx.check("/net_info", c.get("/net_info"))

    res = status.data.get("result", status.data)
    net_res = net.data.get("result", net.data) if isinstance(net.data, dict) else None
    sync = dig(res, "sync_info") or {}

    print()
    table([24, 22, 10, 8, 10, 28], ["Network", "Moniker", "Version", "Peers", "Listening", "ReqTime(ms)"],
          [[dig(res, "node_info", "network"), dig(res, "node_info", "moniker"), dig(res, "node_info", "version"),
            dig(net_res, "n_peers"), dig(net_res, "listening"), f"status:{status.ms} net:{net.ms}"]])

    print(f"\nSync status: catching_up={show(sync.get('catching_up'))}")

    latest_height = to_int(sync.get("latest_block_height"))
    latest_ts = rfc3339_to_epoch(sync.get("latest_block_time"))
    earliest_ts = rfc3339_to_epoch(sync.get("earliest_block_time"))
    print()
    table([10, 20, 12, 12, HASH_COL], ["Row", "BlockTime", "Block Age", "Height", "BlockHash"], [
        ["Latest", utc(latest_ts), age(latest_ts), sync.get("latest_block_height"), sync.get("latest_block_hash")],
        ["Earliest", utc(earliest_ts), age(earliest_ts), sync.get("earliest_block_height"),
         sync.get("earliest_block_hash")],
    ])

    if latest_height is not None:
        print_blocks_per_sec(ctx, latest_height)
    if latest_ts:
        print_time_to_sync(ctx, latest_ts)
    return 0


# --- Aptos ---

def aptos_check(ctx: Ctx) -> int:
    """GET the ledger info at the URL as given (path included, e.g. .../v1).
    ledger_timestamp is microseconds since the Unix epoch."""
    r = ctx.client.get()
    if not r.ok or not isinstance(r.data, dict):
        ctx.error(f"ledger info: {r.error or 'non-JSON response'}")
        return 1
    d = r.data

    ledger_micro = to_int(d.get("ledger_timestamp"))
    ledger_ts = ledger_micro // 1_000_000 if ledger_micro else None
    ledger_ver, oldest_ledger = to_int(d.get("ledger_version")), to_int(d.get("oldest_ledger_version"))
    block_height, oldest_bh = to_int(d.get("block_height")), to_int(d.get("oldest_block_height"))

    def span(hi: Optional[int], lo: Optional[int]) -> str:
        return str(hi - lo) if hi is not None and lo is not None and hi >= lo else "-"

    print()
    table([12, 16, 24, 44, 14], ["Chain ID", "Epoch", "Node role", "Git hash", "ReqTime(ms)"],
          [[d.get("chain_id"), d.get("epoch"), d.get("node_role"), d.get("git_hash"), r.ms]])

    print("\nLedger head (ledger_timestamp drives chain time / age below):")
    table([14, 22, 16, 22, 28], ["Row", "Ledger time (UTC)", "Ledger age", "Ledger version", "(micro timestamp)"],
          [["Head", utc(ledger_ts), age(ledger_ts), d.get("ledger_version"), d.get("ledger_timestamp")]])

    print("\nBlock heights (pruning window on node):")
    table([14, 22, 22, 14], ["Row", "Block height", "Oldest block height", "Span"],
          [["Range", d.get("block_height"), d.get("oldest_block_height"), span(block_height, oldest_bh)]])

    print("\nLedger versions & extras:")
    print(f"  oldest_ledger_version: {show(d.get('oldest_ledger_version'))}")
    print(f"  ledger_version_span (head - oldest): {span(ledger_ver, oldest_ledger)}")
    enc_key = d.get("encryption_key")
    print(f"  encryption_key: {'null' if enc_key is None else show(enc_key)}")

    if block_height is not None:
        print_blocks_per_sec(ctx, block_height)
    if ledger_ts:
        print_time_to_sync(ctx, ledger_ts)
    return 0


# --- Beacon (consensus layer, standard Ethereum REST API) ---

def beacon_check(ctx: Ctx) -> int:
    """Node syncing/peer_count/health/version, head header slot and finality checkpoints."""
    c = ctx.client
    genesis = c.get("/eth/v1/beacon/genesis")
    genesis_time = to_int(dig(genesis.data, "data", "genesis_time"))
    genesis_note = ""
    if not genesis_time:
        genesis_time = BEACON_GENESIS_TIME_MAINNET
        genesis_note = " (fallback: Ethereum mainnet)"

    version = ctx.check("/eth/v1/node/version", c.get("/eth/v1/node/version"))
    sync = c.get("/eth/v1/node/syncing")
    if not sync.ok or not isinstance(sync.data, dict):
        ctx.error(f"/eth/v1/node/syncing: {sync.error or 'non-JSON response'}")
        return 1
    peers = ctx.check("/eth/v1/node/peer_count", c.get("/eth/v1/node/peer_count"))
    health = c.get("/eth/v1/node/health")  # non-2xx is a status here, shown in the Health column
    # Header only: /eth/v2/beacon/blocks/head would download the full block.
    head = ctx.check("/eth/v1/beacon/headers/head", c.get("/eth/v1/beacon/headers/head"))
    finality = ctx.check("/eth/v1/beacon/states/head/finality_checkpoints",
                         c.get("/eth/v1/beacon/states/head/finality_checkpoints"))

    health_label = {200: "OK (synced)", 206: "Syncing"}.get(health.status, f"HTTP {show(health.status)}")
    if health.status is None:
        ctx.error(f"/eth/v1/node/health: {health.error}")
    is_syncing = dig(sync.data, "data", "is_syncing")
    head_slot = dig(sync.data, "data", "head_slot")
    if head_slot in (None, ""):
        head_slot = dig(head.data, "data", "header", "message", "slot")
    head_slot_i = to_int(head_slot)
    justified = dig(finality.data, "data", "current_justified", "epoch") or dig(finality.data, "data", "justified", "epoch")
    finalized = dig(finality.data, "data", "finalized", "epoch")

    slot_ts = genesis_time + head_slot_i * BEACON_SLOT_SECONDS if head_slot_i is not None else None

    print()
    table([36, 14, 12, 14, 28], ["Version", "Peers", "Health", "Syncing", "ReqTime(ms)"],
          [[dig(version.data, "data", "version"), dig(peers.data, "data", "connected"), health_label,
            is_syncing if isinstance(is_syncing, bool) else None, f"ver:{version.ms} sync:{sync.ms}"]])

    print(f"\nSync status: is_syncing={show(is_syncing)}, head_slot={show(head_slot)}, "
          f"sync_distance={show(dig(sync.data, 'data', 'sync_distance'))}")
    print(f"Finality: justified_epoch={show(justified)}, finalized_epoch={show(finalized)}")
    print(f"Genesis time: {genesis_time}{genesis_note}")

    print()
    table([10, 22, 12, 14, 28], ["Row", "Slot time (UTC)", "Slot age", "Slot", "ReqTime(ms)"],
          [["Head", utc(slot_ts), age(slot_ts), head_slot,
            f"head:{head.ms} fin:{finality.ms} hc:{health.ms} peers:{peers.ms}"]])

    if head_slot_i is not None:
        print_blocks_per_sec(ctx, head_slot_i)
    if slot_ts:
        print_time_to_sync(ctx, slot_ts)
    return 0


def beacon_peers(ctx: Ctx) -> int:
    """ENRs of connected consensus layer peers (/eth/v1/node/peers, e.g. Prysm)."""
    r = ctx.client.get("/eth/v1/node/peers")
    print(f"\nConsensus layer peers via /eth/v1/node/peers (req {r.ms}ms)")
    peers = dig(r.data, "data") if r.ok else None
    enrs = [p["enr"] for p in peers or [] if isinstance(p, dict) and p.get("enr")]
    if not enrs:
        ctx.error(f"/eth/v1/node/peers: {r.error or 'no peers found'}")
        return 1
    print(f"Peers: {len(enrs)}")
    print("\n".join(enrs))
    return 0


# --- CLI ---

# command -> (function, monitor, warm-up target: "rpc" or a GET path)
COMMANDS = {
    "general_check": (general_check, False, "rpc"),
    "monitor": (general_check, True, "rpc"),
    "op": (op_check, False, "rpc"),
    "op_monitor": (op_check, True, "rpc"),
    "op_peers": (op_peers, False, "rpc"),
    "tendermint": (tendermint_check, False, "/status"),
    "tendermint_monitor": (tendermint_check, True, "/status"),
    "aptos": (aptos_check, False, ""),
    "aptos_monitor": (aptos_check, True, ""),
    "beacon": (beacon_check, False, "/eth/v1/node/version"),
    "beacon_monitor": (beacon_check, True, "/eth/v1/node/version"),
    "prysm_peers": (beacon_peers, False, "/eth/v1/node/version"),
}

# Per-block/tx lookups now live in eth-cli.py.
MOVED_TO_ETH_CLI = {
    "block_summary": "block <number>",
    "get_block": "block <number> --full",
    "tx": "tx <tx_hash>",
    "get_balance": "balance <address>",
}


def parse_args() -> argparse.Namespace:
    script = sys.argv[0].split("/")[-1]
    parser = argparse.ArgumentParser(
        prog=script,
        description="Quick health checks for EVM, OP node, Tendermint/CometBFT, Aptos and beacon nodes. "
        "All requests reuse one keep-alive connection, so ReqTime excludes TCP/TLS handshakes.",
        epilog="""Commands:
  general_check        EVM checks: chain ID, peers, sync status, latest/safe/finalized/earliest blocks (default)
  monitor              general_check, refreshed every --interval seconds (Ctrl+C to stop)
  op, op_monitor       OP node: rollup chain IDs, version, peers, optimism_syncStatus heads and gaps
  op_peers             Connected OP node peers (opp2p_peers): peer ID, direction, address, user agent, ENR
  tendermint, tendermint_monitor
                       Tendermint/CometBFT: network, peers, catching_up, latest/earliest blocks
  aptos, aptos_monitor Aptos REST ledger info; pass the full URL including the path (e.g. .../v1)
  beacon, beacon_monitor
                       Beacon node: version, peers, health, sync, head slot, finality
  prysm_peers          ENRs of connected consensus layer peers (/eth/v1/node/peers)

Block, transaction and balance lookups moved to eth-cli.py (block, tx, balance).

Examples:
  %(prog)s 8545
  %(prog)s 127.0.0.1:8545 monitor
  %(prog)s https://rpc.example.com general_check
  %(prog)s 9545 op
  %(prog)s 26657 tendermint_monitor
  %(prog)s http://127.0.0.1:8080/v1 aptos
  %(prog)s 3500 beacon""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="Endpoint (port only, host:port, or full http(s) URL)")
    parser.add_argument("command", nargs="?", default="general_check", metavar="command",
                        help="Check to run (default: %(default)s); see the list below")
    parser.add_argument("extra", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("-t", "--timeout", type=float, default=5, metavar="SECS",
                        help="Per-request timeout (default: %(default)s)")
    parser.add_argument("-i", "--interval", type=float, default=1, metavar="SECS",
                        help="Refresh interval for *_monitor commands (default: %(default)s)")
    args = parser.parse_args()

    if args.command in MOVED_TO_ETH_CLI:
        parser.exit(2, f"{script}: '{args.command}' moved to eth-cli.py: "
                       f"./eth-cli.py -u <url> {MOVED_TO_ETH_CLI[args.command]}\n")
    if args.command not in COMMANDS:
        parser.error(f"unknown command '{args.command}' (choose from: {', '.join(COMMANDS)})")
    if args.extra:
        parser.error(f"unexpected argument(s): {' '.join(args.extra)}")
    if args.timeout <= 0 or args.interval <= 0:
        parser.error("--timeout and --interval must be > 0")
    return args


def print_header(client: Client, command: str, monitor: bool, interval: float) -> None:
    print(f"Endpoint: {display_url(client.url)}  [{command}]")
    w = client.warmup
    if w is not None:
        if w.status is None:
            print("Connection: warm-up failed (see Errors); requests below may include connection setup")
        else:
            print(f"Connection: warm-up {w.ms} ms (TCP/TLS setup + first request); ReqTime below reuses it")
    if monitor:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"Refreshed: {now}  (every {interval:g}s, Ctrl+C to stop)")


def run_once(ctx: Ctx, func) -> int:
    """Run one check, then print every error it collected in a single section at the end."""
    ctx.errors = []
    w = ctx.client.warmup
    if w is not None and w.status is None:
        ctx.error(f"warm-up: {w.error}")
    rc = func(ctx)
    if ctx.errors:
        print("\nErrors:")
        for msg in ctx.errors:
            print(f"  {msg}")
    return rc


def main() -> None:
    args = parse_args()
    func, monitor, warm_target = COMMANDS[args.command]
    client = Client(normalize_url(args.url), args.timeout)
    ctx = Ctx(client, Window(), Window())
    client.warm_up(warm_target)

    if not monitor:
        print_header(client, args.command, False, args.interval)
        sys.exit(run_once(ctx, func))

    while True:
        start = time.monotonic()
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_header(client, args.command, True, args.interval)
            run_once(ctx, func)
        # Clear and redraw in one write to reduce flicker.
        sys.stdout.write("\033[H\033[2J" + buf.getvalue())
        sys.stdout.flush()
        time.sleep(max(0.0, args.interval - (time.monotonic() - start)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
