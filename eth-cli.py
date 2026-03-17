#!/usr/bin/env python3
"""Simple Ethereum CLI - balance check and transaction lookup."""

import os
import sys
from datetime import datetime, timezone, timedelta
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
DEFAULT_RPC_BY_CHAIN: dict[int, str] = {
    1: "https://ethereum-rpc.publicnode.com",
    56: "https://bsc-dataseed.binance.org",
    48900: "https://mainnet.zircuit.com",
    1284: "https://rpc.api.moonbeam.network",
}
DEFAULT_RPC = DEFAULT_RPC_BY_CHAIN[1]
RPC_URL = os.environ.get("ETH_RPC", DEFAULT_RPC)

# Chain IDs
CHAIN_ETH = 1
CHAIN_BSC = 56
CHAIN_ZIRCUIT = 48900
CHAIN_MOONBEAM = 1284

# Chain aliases for --chain option (name -> chain_id)
CHAIN_ALIASES: dict[str, int] = {
    "eth": CHAIN_ETH,
    "ethereum": CHAIN_ETH,
    "bsc": CHAIN_BSC,
    "bnb": CHAIN_BSC,
    "binance": CHAIN_BSC,
    "zircuit": CHAIN_ZIRCUIT,
    "moonbeam": CHAIN_MOONBEAM,
    "glmr": CHAIN_MOONBEAM,
}

# Env var names for per-chain RPC: ETH_RPC_1, ETH_RPC_ETH, ETH_RPC_48900, etc.
CHAIN_ENV_ALIASES: dict[int, list[str]] = {
    CHAIN_ETH: ["1", "eth", "ethereum"],
    CHAIN_BSC: ["56", "bsc", "bnb", "binance"],
    CHAIN_ZIRCUIT: ["48900", "zircuit"],
    CHAIN_MOONBEAM: ["1284", "moonbeam", "glmr"],
}


def get_rpc_for_chain(chain_id: int) -> str:
    """RPC URL for chain: env (ETH_RPC_1, ETH_RPC_ETH, etc.) else default."""
    for alias in CHAIN_ENV_ALIASES.get(chain_id, [str(chain_id)]):
        env_val = os.environ.get(f"ETH_RPC_{alias.upper()}")
        if env_val:
            return env_val
    return DEFAULT_RPC_BY_CHAIN.get(chain_id, DEFAULT_RPC)

# ERC20 token contracts by chain ID - (symbol, address, decimals)
# Etherscan: https://etherscan.io/token/<addr> | Zircuit: https://explorer.zircuit.com | Moonbeam: https://moonscan.io
TOKENS_BY_CHAIN: dict[int, list[tuple[str, str, int]]] = {
    CHAIN_ETH: [
        ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
        ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
        ("DAI", "0x6B175474E89094C44Da98b954Eedeac495271d0F", 18),
        ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),
        ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),
        ("LINK", "0x514910771AF9Ca656af840dff83E8264EcF986CA", 18),
        ("UNI", "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", 18),
        ("AAVE", "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", 18),
        ("MKR", "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2", 18),
        ("CRV", "0xD533a949740bb3306d119CC777fa900bA034cd52", 18),
        ("LDO", "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32", 18),
        ("GRT", "0xc944E90C64B2c07662A292be6244BDF05CdA44a7", 18),
        ("HOPR", "0xF5581dFeFD8Fb0e4aeC526bE659CFaB1f8c781dA", 18),
        ("SHIB", "0x95aD61b0a150d79219dC64E6eEB7C517d7cC5A6c", 18),
        ("PEPE", "0x6982508145454Ce325dDbE47a25d4ec3d2311933", 18),
    ],
    CHAIN_BSC: [
        ("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
        ("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18),
        ("BUSD", "0xe9e7CEA3DedcA5984780Bafc599bD69ADd087D56", 18),
        ("WBNB", "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", 18),
        ("DAI", "0x1AF3F329e8BE154074D8769D1FFa4eE058B1DBc3", 18),
        ("ETH", "0x2170Ed0880ac9A755fd29B2688956BD959F933F8", 18),
        ("BTCB", "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c", 18),
        ("CAKE", "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82", 18),
    ],
    CHAIN_ZIRCUIT: [
        ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6),
        ("USDT", "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", 6),
        ("WETH", "0x4200000000000000000000000000000000000006", 18),
        ("ZRC", "0xfd418E42783382E86Ae91e445406600Ba144D162", 18),
    ],
    CHAIN_MOONBEAM: [
        ("WGLMR", "0xAcc15dC74880C9944775448304B263D191c6077F", 18),
        ("USDC", "0x818ec0A7Fe18Ff94269904fCED6AE3DaE6d6dC0b", 6),
        ("LINK", "0x012414A392F9FA442a3109f1320c439C45518aC3", 18),
    ],
}

# ERC20 Transfer(address,address,uint256) topic0
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

CHAIN_NAMES: dict[int, str] = {
    CHAIN_ETH: "Ethereum",
    CHAIN_BSC: "BSC",
    CHAIN_ZIRCUIT: "Zircuit",
    CHAIN_MOONBEAM: "Moonbeam",
}

NATIVE_SYMBOL: dict[int, str] = {
    CHAIN_ETH: "ETH",
    CHAIN_BSC: "BNB",
    CHAIN_ZIRCUIT: "ETH",
    CHAIN_MOONBEAM: "GLMR",
}


def get_chain_id() -> int | None:
    """Get chain ID from RPC via eth_chainId."""
    result = rpc_call("eth_chainId", [])
    if result is None:
        return None
    return int(result, 16)


def rpc_call(method: str, params: list, return_error: bool = False) -> dict | None:
    """Send JSON-RPC request. If return_error=True, return full response including error."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            if not return_error:
                print(f"RPC error: {data['error']}", file=sys.stderr)
            return data if return_error else None
        return data.get("result") if not return_error else data
    except requests.RequestException as e:
        if not return_error:
            print(f"RPC request failed: {e}", file=sys.stderr)
        return None


def _decode_revert_reason(data_hex: str) -> str:
    """Decode Error(string) revert data. Returns empty string if not decodable."""
    if not data_hex or data_hex == "0x" or len(data_hex) < 138:  # 0x08c379a0 + 32 + 32 + min 1 char
        return ""
    data = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if data[:8] != "08c379a0":  # Error(string) selector
        return ""
    try:
        offset = int(data[8:72], 16)
        length = int(data[72:136], 16)
        if length > 1000:
            return ""
        raw = bytes.fromhex(data[136 : 136 + length * 2]).decode("utf-8", errors="replace")
        return raw.strip() or ""
    except (ValueError, IndexError):
        return ""


def get_revert_reason(tx: dict, receipt: dict, block_num: str) -> str:
    """Replay failed tx via eth_call to get revert reason."""
    call = {
        "from": tx.get("from", "0x0000000000000000000000000000000000000000"),
        "to": tx.get("to"),
        "data": tx.get("input") or tx.get("data", "0x"),
        "value": tx.get("value", "0x0"),
        "gas": tx.get("gas", "0x5208"),
    }
    if tx.get("gasPrice"):
        call["gasPrice"] = tx["gasPrice"]
    elif tx.get("maxFeePerGas"):
        call["maxFeePerGas"] = tx["maxFeePerGas"]
        call["maxPriorityFeePerGas"] = tx.get("maxPriorityFeePerGas", tx["maxFeePerGas"])
    if not call.get("to"):
        return ""
    resp = rpc_call("eth_call", [call, block_num], return_error=True)
    if resp is None or "error" not in resp:
        return ""
    err = resp["error"]
    data = err.get("data", "")
    if isinstance(data, str):
        return _decode_revert_reason(data)
    return ""


def to_checksum(address: str) -> str:
    """Normalize address to checksum format (0x...)."""
    addr = address.strip()
    if not addr.startswith("0x"):
        addr = "0x" + addr
    return addr


def get_eth_balance(address: str) -> str | None:
    """Get native ETH balance in wei (returns hex string)."""
    addr = to_checksum(address)
    result = rpc_call("eth_getBalance", [addr, "latest"])
    return result


def get_erc20_balance(token_address: str, owner_address: str) -> str | None:
    """Get ERC20 token balance. Returns raw hex value (uint256)."""
    addr = to_checksum(owner_address)
    token = to_checksum(token_address)
    # balanceOf(address) selector = 0x70a08231
    # Pad address to 32 bytes (64 hex chars)
    padded = addr[2:].zfill(64)
    data = "0x70a08231" + padded
    result = rpc_call("eth_call", [{"to": token, "data": data}, "latest"])
    return result


def wei_to_eth(wei_hex: str) -> float:
    """Convert wei (hex) to ETH."""
    if not wei_hex or wei_hex == "0x":
        return 0.0
    wei = int(wei_hex, 16)
    return wei / 1e18


def raw_to_token(raw_hex: str, decimals: int) -> float:
    """Convert raw ERC20 balance to human-readable."""
    if not raw_hex or raw_hex == "0x":
        return 0.0
    raw = int(raw_hex, 16)
    return raw / (10**decimals)


def get_tx(tx_hash: str) -> dict | None:
    """Get transaction by hash. Returns tx dict or None."""
    h = tx_hash.strip()
    if not h.startswith("0x"):
        h = "0x" + h
    return rpc_call("eth_getTransactionByHash", [h])


def get_tx_receipt(tx_hash: str) -> dict | None:
    """Get transaction receipt. Returns receipt dict or None."""
    h = tx_hash.strip()
    if not h.startswith("0x"):
        h = "0x" + h
    return rpc_call("eth_getTransactionReceipt", [h])


def parse_block_arg(arg: str) -> str:
    """Parse block argument to eth_getBlockByNumber format. Returns hex block tag or number."""
    s = arg.strip().lower()
    if s in ("latest", "safe", "finalized", "finalised", "pending", "earliest"):
        return "finalized" if s == "finalised" else s
    if s.startswith("0x"):
        return s if len(s) > 2 else "0x0"
    try:
        n = int(s)
        return hex(n)
    except ValueError:
        return arg


def get_block(block_param: str, full_tx: bool = False) -> dict | None:
    """Get block by number or tag. block_param: latest, safe, finalized, pending, or hex block number."""
    return rpc_call("eth_getBlockByNumber", [block_param, full_tx])


def _time_ago(ts: int) -> str:
    """Return human-readable 'X ago' string for a Unix timestamp."""
    now = datetime.now(timezone.utc)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    delta = now - dt
    if delta < timedelta(0):
        return "in the future"
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    if secs < 2592000:
        return f"{secs // 86400}d ago"
    if secs < 31536000:
        return f"{secs // 2592000}mo ago"
    return f"{secs // 31536000}y ago"


# Well-known selectors that have collisions in 4byte.directory - prefer canonical names
COMMON_SELECTORS: dict[str, str] = {
    "0xa9059cbb": "transfer(address,uint256)",
    "0x095ea7b3": "approve(address,uint256)",
    "0x23b872dd": "transferFrom(address,address,uint256)",
    "0x70a08231": "balanceOf(address)",
    "0x18160ddd": "totalSupply()",
    "0x06fdde03": "name()",
    "0x95d89b41": "symbol()",
    "0x313ce567": "decimals()",
    "0x42842e0e": "safeTransferFrom(address,address,uint256)",
    "0xb88d4fde": "safeTransferFrom(address,address,uint256,bytes)",
    "0xa22cb465": "setApprovalForAll(address,bool)",
    "0xe985e9c5": "isApprovedForAll(address,address)",
}


def _get_function_signature(selector: str) -> str:
    """Look up function name. Prefer COMMON_SELECTORS, else 4byte.directory."""
    sel = selector.lower()
    if not sel.startswith("0x"):
        sel = "0x" + sel
    if sel in COMMON_SELECTORS:
        return COMMON_SELECTORS[sel]
    try:
        r = requests.get(
            f"https://www.4byte.directory/api/v1/signatures/?hex_signature={sel}",
            timeout=3,
        )
        if r.status_code != 200:
            return ""
        data = r.json()
        results = data.get("results", [])
        if results:
            return results[0].get("text_signature", "")
    except Exception:
        pass
    return ""


def _print_input_decoded(input_data: str) -> None:
    """Pretty-print tx input: Function, MethodID, and params [0], [1], [2]..."""
    hex_part = input_data[2:] if input_data.startswith("0x") else input_data
    if len(hex_part) < 8:
        return
    selector = "0x" + hex_part[:8].lower()
    params_hex = hex_part[8:]
    func_name = _get_function_signature(selector)
    print("")
    print("Input:")
    if func_name:
        print(f"Function: {func_name}")
    print(f"MethodID: {selector}")
    for i in range(0, len(params_hex), 64):
        chunk = params_hex[i : i + 64]
        idx = i // 64
        print(f"[{idx}]:  {chunk}")


def _format_balance(value: float, decimals: int) -> str:
    """Format token balance for display (comma-separated, sensible precision)."""
    if decimals <= 6:
        return f"{value:,.2f}"
    return f"{value:,.4f}"


def _fetch_native(addr: str, symbol: str) -> tuple[str, str | None, type(None)]:
    """Fetch native balance. Returns (symbol, hex_value, None)."""
    return (symbol, get_eth_balance(addr), None)


def _fetch_token(addr: str, symbol: str, token_addr: str, decimals: int) -> tuple[str, str | None, int]:
    """Fetch ERC20 balance. Returns (symbol, hex_value, decimals)."""
    return (symbol, get_erc20_balance(token_addr, addr), decimals)


def check_balance(address: str) -> None:
    """Check and print native + all configured ERC20 token balances (parallel requests)."""
    addr = to_checksum(address)
    chain_id = get_chain_id()
    chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}" if chain_id else "Unknown")
    tokens = TOKENS_BY_CHAIN.get(chain_id, []) if chain_id else []
    native_sym = NATIVE_SYMBOL.get(chain_id, "ETH")

    print(f"Address: {addr}")
    print(f"Chain:   {chain_name} ({chain_id})")
    print(f"RPC:     {RPC_URL}")
    print("-" * 50)

    results: dict[str, tuple[str | None, int | None]] = {}

    with ThreadPoolExecutor(max_workers=min(32, 1 + len(tokens))) as executor:
        futures = [executor.submit(_fetch_native, addr, native_sym)]
        futures += [
            executor.submit(_fetch_token, addr, sym, tok, dec)
            for sym, tok, dec in tokens
        ]
        for future in as_completed(futures):
            label, raw_hex, decimals = future.result()
            results[label] = (raw_hex, decimals)

    # Print native balance first (skip if failed or zero)
    native_hex, _ = results.get(native_sym, (None, None))
    if native_hex is not None:
        native_val = wei_to_eth(native_hex)
        if native_val > 0:
            print(f"{native_sym}:  {native_val:.6f}")

    # Print tokens in original order (skip if failed or zero)
    for symbol, _, decimals in tokens:
        raw_hex, _ = results.get(symbol, (None, None))
        if raw_hex is not None and decimals is not None:
            val = raw_to_token(raw_hex, decimals)
            if val > 0:
                print(f"{symbol}: {_format_balance(val, decimals)}")


def check_tx(tx_hash: str) -> None:
    """Query and print transaction details."""
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_tx = ex.submit(get_tx, tx_hash)
        f_rcpt = ex.submit(get_tx_receipt, tx_hash)
        f_chain = ex.submit(get_chain_id)
        tx, receipt, chain_id = f_tx.result(), f_rcpt.result(), f_chain.result()

    if not tx:
        print("Transaction not found.", file=sys.stderr)
        sys.exit(1)

    h = tx_hash.strip()
    if not h.startswith("0x"):
        h = "0x" + h

    chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}") if chain_id else "Unknown"
    native_sym = NATIVE_SYMBOL.get(chain_id, "ETH")
    token_by_addr = {addr.lower(): (sym, dec) for sym, addr, dec in TOKENS_BY_CHAIN.get(chain_id or 0, [])}

    print(f"{'Chain:':8} {chain_name} ({chain_id})")
    print(f"{'RPC:':8} {RPC_URL}")
    print("-" * 80)
    print(f"TX Hash:     {h}")
    print(f"From:        {tx.get('from', 'N/A')}")
    print(f"To:          {tx.get('to') or '(contract creation)'}")
    print(f"Value:       {wei_to_eth(tx.get('value', '0x0')):.6f} {native_sym}")
    print(f"Gas Limit:   {int(tx.get('gas', '0x0'), 16):,}")
    gas_price = tx.get("gasPrice") or tx.get("maxFeePerGas") or "0x0"
    print(f"Gas Price:   {int(gas_price, 16) / 1e9:.2f} Gwei")

    if receipt:
        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        eff_gas = receipt.get("effectiveGasPrice") or tx.get("gasPrice") or tx.get("maxFeePerGas") or "0x0"
        fee_wei = gas_used * int(eff_gas, 16)
        fee_eth = fee_wei / 1e18
        block_num = receipt.get("blockNumber")
        block = get_block(block_num) if block_num else None
        ts = int(block.get("timestamp", "0x0"), 16) if block else 0
        ts_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "N/A"
        ago = _time_ago(ts) if ts else ""
        ts_display = f"{ts_str} ({ago})" if ago else ts_str
        print(f"Gas Used:    {gas_used:,}")
        print(f"Tx Fee:      {fee_eth:.6f} {native_sym}")
        print(f"Timestamp:   {ts_display}")
        print(f"Block:       {int(receipt.get('blockNumber', '0x0'), 16):,}")
        status_ok = receipt.get("status") == "0x1"
        status_str = "Success" if status_ok else "Failed"
        if not status_ok:
            reason = get_revert_reason(tx, receipt, block_num or "latest")
            if reason:
                status_str = f"Failed ({reason})"
        print(f"Status:      {status_str}")

    # ERC20 token transfers from receipt logs (show all, not just those involving tx sender)
    tx_from = (tx.get("from") or "").lower()
    token_transfers: list[tuple[str, str, str, float, str, str]] = []  # (direction, symbol, addr, amount, from_addr, to_addr)

    for log in receipt.get("logs", []) if receipt else []:
        topics = log.get("topics", [])
        if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC:
            continue
        token_addr = (log.get("address") or "").lower()
        from_addr = ("0x" + topics[1][-40:]).lower() if len(topics[1]) >= 40 else ""
        to_addr = ("0x" + topics[2][-40:]).lower() if len(topics[2]) >= 40 else ""
        raw_amount = log.get("data", "0x0")
        if raw_amount == "0x":
            raw_amount = "0x0"
        amount_raw = int(raw_amount, 16)
        sym, dec = token_by_addr.get(token_addr, ("?", 18))
        amount = amount_raw / (10**dec)
        if amount == 0:
            continue
        if from_addr == tx_from:
            direction = "sent"
        elif to_addr == tx_from:
            direction = "received"
        else:
            direction = "transfer"
        token_transfers.append((direction, sym, token_addr, amount, from_addr, to_addr))

    if token_transfers:
        print("")
        print("Token transfers:")
        for direction, sym, addr, amt, fr, to in token_transfers:
            label = sym if sym != "?" else f"{addr[:8]}...{addr[-6:]}"
            dec = token_by_addr.get(addr, ("", 18))[1]
            amt_fmt = _format_balance(amt, dec)
            if direction == "sent":
                dest = f"to {to}"
            elif direction == "received":
                dest = f"from {fr}"
            else:
                dest = f"{fr} -> {to}"
            print(f"  {direction.capitalize():8} {amt_fmt:>12} {label}  ({dest})")

    input_data = tx.get("input") or tx.get("data") or "0x"
    if input_data and input_data != "0x":
        _print_input_decoded(input_data)


def check_block(block_arg: str) -> None:
    """Query and print block details."""
    block_param = parse_block_arg(block_arg)
    block = get_block(block_param)
    if not block:
        print("Block not found.", file=sys.stderr)
        sys.exit(1)

    chain_id = get_chain_id()
    chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}") if chain_id else "Unknown"

    print(f"Block: {block_param}")
    print(f"Chain: {chain_name} ({chain_id})")
    print(f"RPC:   {RPC_URL}")
    print("-" * 50)
    num_hex = block.get("number", "0x0")
    num_int = int(num_hex, 16)
    print(f"Number:       {num_int:,} (0x{num_int:x})")
    print(f"Hash:         {block.get('hash', 'N/A')}")
    print(f"Parent:       {block.get('parentHash', 'N/A')}")
    ts = int(block.get('timestamp', '0x0'), 16)
    ts_human = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if ts else "N/A"
    print(f"Timestamp:    {ts} ({ts_human})")
    print(f"Miner:        {block.get('miner', 'N/A')}")
    print(f"Gas Limit:    {int(block.get('gasLimit', '0x0'), 16):,}")
    print(f"Gas Used:     {int(block.get('gasUsed', '0x0'), 16):,}")
    print(f"Tx Count:     {len(block.get('transactions', []))}")


def print_help() -> None:
    print("Usage: eth-cli.py balance <address> [--chain NAME] [--rpc URL]")
    print("       eth-cli.py tx <tx_hash> [--chain NAME] [--rpc URL]")
    print("       eth-cli.py block [latest|safe|finalized|NUMBER] [--chain NAME] [--rpc URL]")
    print("")
    print("  balance   - Check native + token balances for address")
    print("  tx        - Query transaction by hash")
    print("  block     - Query block (default: latest)")
    print("             Args: latest, safe, finalized, pending, earliest, or block number (int/hex)")
    print("  --chain   - Chain: eth, bsc, zircuit, moonbeam (uses default RPC for that chain)")
    print("  --rpc     - RPC URL (overrides --chain default; else $ETH_RPC or Ethereum)")
    print("")
    print("  Env: ETH_CHAIN (chain when no --chain); ETH_RPC (fallback);")
    print("       ETH_RPC_ETH, ETH_RPC_ZIRCUIT, ETH_RPC_MOONBEAM (per-chain RPC overrides)")


def main():
    global RPC_URL

    if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv:
        print_help()
        sys.exit(0)

    args = sys.argv[1:]
    target = None
    mode = None
    chain_arg = None
    rpc_arg = None
    i = 0
    while i < len(args):
        if args[i] == "--chain" and i + 1 < len(args):
            chain_arg = args[i + 1].lower()
            i += 2
        elif args[i] == "--rpc" and i + 1 < len(args):
            rpc_arg = args[i + 1]
            i += 2
        elif args[i] == "balance":
            if i + 1 >= len(args) or args[i + 1].startswith("--"):
                print("Error: balance requires an address.", file=sys.stderr)
                sys.exit(1)
            mode = "balance"
            target = args[i + 1]
            i += 2
        elif args[i] == "tx":
            if i + 1 >= len(args):
                print("Error: tx requires a transaction hash.", file=sys.stderr)
                sys.exit(1)
            mode = "tx"
            target = args[i + 1]
            i += 2
        elif args[i] == "block":
            mode = "block"
            target = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else "latest"
            if target == "latest":
                i += 1
            else:
                i += 2
        else:
            i += 1

    # Apply chain/RPC: --rpc overrides; --chain uses chain RPC (env or default); else ETH_CHAIN or ETH_RPC
    if rpc_arg:
        RPC_URL = rpc_arg
    else:
        chain_arg = chain_arg or os.environ.get("ETH_CHAIN", "").strip().lower()
        if chain_arg:
            chain_id = CHAIN_ALIASES.get(chain_arg)
            if chain_id:
                RPC_URL = get_rpc_for_chain(chain_id)
            elif chain_arg.isdigit():
                cid = int(chain_arg)
                RPC_URL = get_rpc_for_chain(cid) if cid in DEFAULT_RPC_BY_CHAIN else os.environ.get("ETH_RPC", DEFAULT_RPC)
            else:
                print(f"Error: Unknown chain '{chain_arg}'. Use: {', '.join(CHAIN_ALIASES)}", file=sys.stderr)
                sys.exit(1)

    if mode == "block":
        check_block(target or "latest")
    elif mode == "tx":
        if not target:
            print("Error: Transaction hash required.", file=sys.stderr)
            sys.exit(1)
        check_tx(target)
    elif mode == "balance":
        if not target:
            print("Error: Address required.", file=sys.stderr)
            sys.exit(1)
        check_balance(target)
    else:
        print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
