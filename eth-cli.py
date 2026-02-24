#!/usr/bin/env python3
"""Simple Ethereum CLI - check ETH and major ERC20 token balances."""

import sys
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# --- Configuration ---
DEFAULT_RPC = "https://ethereum-rpc.publicnode.com"
RPC_URL = DEFAULT_RPC

# ERC20 token contracts (Ethereum mainnet) - (symbol, address, decimals)
# Etherscan links: https://etherscan.io/token/<address>
TOKENS = [
    # Stablecoins
    ("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),   # https://etherscan.io/token/0xdac17f958d2ee523a2206206994597c13d831ec7
    ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),   # https://etherscan.io/token/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48
    ("DAI",  "0x6B175474E89094C44Da98b954Eedeac495271d0F", 18),  # https://etherscan.io/token/0x6b175474e89094c44da98b954eedeac495271d0f
    # Wrapped / bridged
    ("WETH", "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", 18),  # https://etherscan.io/token/0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2
    ("WBTC", "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", 8),    # https://etherscan.io/token/0x2260fac5e5542a773aa44fbcfedf7c193bc2c599
    # DeFi / governance
    ("LINK", "0x514910771AF9Ca656af840dff83E8264EcF986CA", 18),  # https://etherscan.io/token/0x514910771af9ca656af840dff83e8264ecf986ca
    ("UNI",  "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", 18),   # https://etherscan.io/token/0x1f9840a85d5af5bf1d1762f925bdaddc4201f984
    ("AAVE", "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", 18),   # https://etherscan.io/token/0x7fc66500c84a76ad7e9c93437bfc5ac33e2ddae9
    ("MKR",  "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2", 18),   # https://etherscan.io/token/0x9f8f72aa9304c8b593d555f12ef6589cc3a579a2
    ("CRV",  "0xD533a949740bb3306d119CC777fa900bA034cd52", 18),   # https://etherscan.io/token/0xd533a949740bb3306d119cc777fa900ba034cd52
    ("LDO",  "0x5A98FcBEA516Cf06857215779Fd812CA3beF1B32", 18),   # https://etherscan.io/token/0x5a98fcbea516cf06857215779fd812ca3bef1b32
    ("GRT",  "0xc944E90C64B2c07662A292be6244BDF05CdA44a7", 18),  # https://etherscan.io/token/0xc944e90c64b2c07662a292be6244bdf05cda44a7
    ("HOPR", "0xF5581dFeFD8Fb0e4aeC526bE659CFaB1f8c781dA", 18),  # https://etherscan.io/token/0xf5581dfefd8fb0e4aec526be659cfab1f8c781da
    # Meme / popular
    ("SHIB", "0x95aD61b0a150d79219dC64E6eEB7C517d7cC5A6c", 18),   # https://etherscan.io/token/0x95ad61b0a150d79219dc64e6eeb7c517d7cc5a6c
    ("PEPE", "0x6982508145454Ce325dDbE47a25d4ec3d2311933", 18),   # https://etherscan.io/token/0x6982508145454ce325ddbe47a25d4ec3d2311933
]


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
    print(f"Address: {addr}")
    print(f"RPC: {RPC_URL}")
    print("-" * 50)

    results: dict[str, tuple[str | None, int | None]] = {}  # label -> (raw_hex, decimals|None)

    with ThreadPoolExecutor(max_workers=min(32, 1 + len(TOKENS))) as executor:
        futures = [executor.submit(_fetch_eth, addr)]
        futures += [
            executor.submit(_fetch_token, addr, sym, tok, dec)
            for sym, tok, dec in TOKENS
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
    for symbol, _, decimals in TOKENS:
        raw_hex, _ = results.get(symbol, (None, None))
        if raw_hex is not None and decimals is not None:
            val = raw_to_token(raw_hex, decimals)
            if val > 0:
                print(f"{symbol}: {_format_balance(val, decimals)}")


def main():
    global RPC_URL

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: eth-cli.py <address> [--rpc URL]")
        print("  address  - Ethereum address to check balances for")
        print("  --rpc    - Optional RPC URL (default: https://ethereum-rpc.publicnode.com)")
        sys.exit(0 if len(sys.argv) < 2 else 1)

    args = sys.argv[1:]
    address = None
    i = 0
    while i < len(args):
        if args[i] == "--rpc" and i + 1 < len(args):
            RPC_URL = args[i + 1]
            i += 2
        else:
            address = args[i]
            i += 1

    if not address:
        print("Error: Address required.", file=sys.stderr)
        sys.exit(1)

    check_balance(address)


if __name__ == "__main__":
    main()
