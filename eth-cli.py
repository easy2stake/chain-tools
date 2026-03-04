#!/usr/bin/env python3
"""Simple Ethereum CLI - balance check and transaction lookup."""

import os
import sys
from datetime import datetime, timezone
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
RPC_URL = os.environ.get("ETH_RPC", DEFAULT_RPC)

# Chain IDs
CHAIN_ETH = 1
CHAIN_ZIRCUIT = 48900

# ERC20 token contracts by chain ID - (symbol, address, decimals)
# Etherscan: https://etherscan.io/token/<addr> | Zircuit: https://explorer.zircuit.com
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
    CHAIN_ZIRCUIT: [
        ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6),
        ("USDT", "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2", 6),
        ("WETH", "0x4200000000000000000000000000000000000006", 18),
        ("ZRC", "0xfd418E42783382E86Ae91e445406600Ba144D162", 18),
    ],
}

# ERC20 Transfer(address,address,uint256) topic0
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

CHAIN_NAMES: dict[int, str] = {
    CHAIN_ETH: "Ethereum",
    CHAIN_ZIRCUIT: "Zircuit",
}


def get_chain_id() -> int | None:
    """Get chain ID from RPC via eth_chainId."""
    result = rpc_call("eth_chainId", [])
    if result is None:
        return None
    return int(result, 16)


def rpc_call(method: str, params: list) -> dict | None:
    """Send JSON-RPC request to the configured RPC endpoint."""
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    try:
        r = requests.post(RPC_URL, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            print(f"RPC error: {data['error']}", file=sys.stderr)
            return None
        return data.get("result")
    except requests.RequestException as e:
        print(f"RPC request failed: {e}", file=sys.stderr)
        return None


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


def _format_balance(value: float, decimals: int) -> str:
    """Format token balance for display (comma-separated, sensible precision)."""
    if decimals <= 6:
        return f"{value:,.2f}"
    return f"{value:,.4f}"


def _fetch_eth(addr: str) -> tuple[str, str | None, type(None)]:
    """Fetch ETH balance. Returns ('ETH', hex_value, None)."""
    return ("ETH", get_eth_balance(addr), None)


def _fetch_token(addr: str, symbol: str, token_addr: str, decimals: int) -> tuple[str, str | None, int]:
    """Fetch ERC20 balance. Returns (symbol, hex_value, decimals)."""
    return (symbol, get_erc20_balance(token_addr, addr), decimals)


def check_balance(address: str) -> None:
    """Check and print ETH + all configured ERC20 token balances (parallel requests)."""
    addr = to_checksum(address)
    chain_id = get_chain_id()
    chain_name = CHAIN_NAMES.get(chain_id, f"Chain {chain_id}" if chain_id else "Unknown")
    tokens = TOKENS_BY_CHAIN.get(chain_id, []) if chain_id else []

    print(f"Address: {addr}")
    print(f"Chain:   {chain_name} ({chain_id})")
    print(f"RPC:     {RPC_URL}")
    print("-" * 50)

    results: dict[str, tuple[str | None, int | None]] = {}

    with ThreadPoolExecutor(max_workers=min(32, 1 + len(tokens))) as executor:
        futures = [executor.submit(_fetch_eth, addr)]
        futures += [
            executor.submit(_fetch_token, addr, sym, tok, dec)
            for sym, tok, dec in tokens
        ]
        for future in as_completed(futures):
            label, raw_hex, decimals = future.result()
            results[label] = (raw_hex, decimals)

    # Print ETH first (skip if failed or zero)
    eth_hex, _ = results.get("ETH", (None, None))
    if eth_hex is not None:
        eth_val = wei_to_eth(eth_hex)
        if eth_val > 0:
            print(f"ETH:  {eth_val:.6f}")

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
    token_by_addr = {addr.lower(): (sym, dec) for sym, addr, dec in TOKENS_BY_CHAIN.get(chain_id or 0, [])}


    print(f"{'Chain:':8} {chain_name} ({chain_id})")
    print(f"{'RPC:':8} {RPC_URL}")
    print("-" * 80)
    print(f"TX Hash:     {h}")
    print(f"From:        {tx.get('from', 'N/A')}")
    print(f"To:          {tx.get('to') or '(contract creation)'}")
    print(f"Value:       {wei_to_eth(tx.get('value', '0x0')):.6f} ETH")
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
        print(f"Gas Used:    {gas_used:,}")
        print(f"Tx Fee:      {fee_eth:.6f} ETH")
        print(f"Timestamp:   {ts_str}")
        print(f"Block:       {int(receipt.get('blockNumber', '0x0'), 16):,}")
        print(f"Status:      {'Success' if receipt.get('status') == '0x1' else 'Failed'}")

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
    print(f"Timestamp:    {int(block.get('timestamp', '0x0'), 16)}")
    print(f"Miner:        {block.get('miner', 'N/A')}")
    print(f"Gas Limit:    {int(block.get('gasLimit', '0x0'), 16):,}")
    print(f"Gas Used:     {int(block.get('gasUsed', '0x0'), 16):,}")
    print(f"Tx Count:     {len(block.get('transactions', []))}")


def print_help() -> None:
    print("Usage: eth-cli.py balance <address> [--rpc URL]")
    print("       eth-cli.py tx <tx_hash> [--rpc URL]")
    print("       eth-cli.py block [latest|safe|finalized|NUMBER] [--rpc URL]")
    print("")
    print("  balance   - Check ETH + token balances for address")
    print("  tx        - Query transaction by hash")
    print("  block     - Query block (default: latest)")
    print("             Args: latest, safe, finalized, pending, earliest, or block number (int/hex)")
    print("  --rpc     - RPC URL (default: $ETH_RPC or public Ethereum)")


def main():
    global RPC_URL

    if len(sys.argv) < 2 or "-h" in sys.argv or "--help" in sys.argv:
        print_help()
        sys.exit(0)

    args = sys.argv[1:]
    target = None
    mode = None
    i = 0
    while i < len(args):
        if args[i] == "--rpc" and i + 1 < len(args):
            RPC_URL = args[i + 1]
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
