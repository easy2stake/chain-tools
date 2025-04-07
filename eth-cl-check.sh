#!/usr/bin/env python3

import requests
import time
import sys
import json

# Configuration
BASE_URL = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "http://127.0.0.1:3500"
DEBUG = "--debug" in sys.argv
GENESIS_TIME = 1606824023  # Ethereum mainnet genesis time

# Thresholds
MAX_BLOCK_DELAY = 60  # seconds
MIN_PEERS = 10

def log_debug(msg):
    if DEBUG:
        if isinstance(msg, (dict, list)):
            print(f"[DEBUG] {json.dumps(msg, indent=2)}")
        else:
            print(f"[DEBUG] {msg}")

def get_json(endpoint):
    try:
        full_url = f"{BASE_URL}{endpoint}"
        log_debug(f"GET {full_url}")
        r = requests.get(full_url, timeout=5)
        r.raise_for_status()
        json_data = r.json()
        log_debug(json_data)
        return json_data
    except Exception as e:
        print(f"Error fetching {endpoint}: {e}")
        return None

def check_sync_status():
    data = get_json("/eth/v1/node/syncing")
    if not data or data.get("data", {}).get("is_syncing") is True:
        print("Node is syncing or sync status unknown")
        return False
    return True

def check_peer_count():
    data = get_json("/eth/v1/node/peer_count")
    if not data:
        print("Could not fetch peer count")
        return False
    peers = int(data["data"]["connected"])
    log_debug({"peers": peers})
    if peers < MIN_PEERS:
        print(f"Low peer count: {peers}")
        return False
    return True

def check_finality():
    data = get_json("/eth/v1/beacon/states/head/finality_checkpoints")
    if not data:
        print("Could not fetch finality checkpoints")
        return False
    try:
        justified = int(data["data"]["justified"]["epoch"])
        finalized = int(data["data"]["finalized"]["epoch"])
        log_debug({
            "justified_epoch": justified,
            "finalized_epoch": finalized
        })
    except (KeyError, TypeError, ValueError):
        print("Missing or invalid finality checkpoints")
        return False

    if finalized == 0 or justified == 0:
        print("Finality checkpoints stuck or zero")
        return False
    return True

def check_health():
    try:
        r = requests.get(f"{BASE_URL}/eth/v1/node/health", timeout=5)
        log_debug({"status_code": r.status_code})
        if r.status_code == 200:
            return True
        elif r.status_code == 206:
            print("Node is syncing (206)")
            return False
        else:
            print(f"Health check failed: HTTP {r.status_code}")
            return False
    except Exception as e:
        print(f"Health check request failed: {e}")
        return False

def check_slot_timestamp():
    data = get_json("/eth/v2/beacon/blocks/head")
    if not data:
        print("Could not fetch head block")
        return False
    slot = int(data["data"]["message"]["slot"])
    timestamp = GENESIS_TIME + (slot * 12)
    now = int(time.time())
    delay = now - timestamp
    log_debug({
        "slot": slot,
        "calculated_block_timestamp": timestamp,
        "current_time": now,
        "delay_seconds": delay
    })
    if delay > MAX_BLOCK_DELAY:
        print(f"Block is stale: delay={delay}s (slot={slot})")
        return False
    return True

def main():
    checks = [
        ("Sync Status", check_sync_status),
        ("Peer Count", check_peer_count),
        ("Health Endpoint", check_health),
        ("Head Slot Timestamp", check_slot_timestamp),
    ]

    failed = False
    for name, check in checks:
        if not check():
            print(f"[FAIL] {name}")
            failed = True
        else:
            print(f"[OK]   {name}")

    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()

