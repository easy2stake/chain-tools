#!/usr/bin/python3

import requests
import time
import sys
import json

# Help menu if running empty or --help flag is provided
if len(sys.argv) == 1 or "--help" in sys.argv:
    print(f"""Usage: {sys.argv[0]} [BASE_URL] [--debug] [--help]

BASE_URL: The base URL for the Ethereum node API.
--debug:   Enable debug output.
--help:    Display this help menu.
""")
    sys.exit(0)

# Configuration
if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
    print("Error: BASE_URL must be provided.")
    sys.exit(1)

# Variables passed by ldirectord/haproxy.
# sys.argv[1] = VIP Address
# sys.argv[2] = VIP Port
# sys.argv[3] = Real Server IP
# sys.argv[4] = Real Server Port

# Reassigning to named variables
server = sys.argv[3]
port = sys.argv[4]
BASE_URL=f"http://{server}:{port}"
DEBUG = "--debug" in sys.argv
GENESIS_TIME = 1606824023  # Ethereum mainnet genesis time
script_name = sys.argv[0].split('/')[-1]

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
    full_url = f"{BASE_URL}{endpoint}"
    try:
        log_debug(f"get_json: GET {full_url}")
        r = requests.get(full_url, timeout=2)
        r.raise_for_status()
        json_data = r.json()
        # log_debug(f"get_json: {json_data}")
        return json_data
    except requests.exceptions.Timeout:
        print(f"{script_name}: Error fetching {full_url}: connection timed out")
        return None
    except Exception as e:
        print(f"E{script_name}: rror fetching {full_url}: {e}")
        return None

def check_sync_status():
    data = get_json("/eth/v1/node/syncing")
    if data is None:
        print(f"{script_name}: Sync check timed out. Exiting script.")
        sys.exit(1)
    log_debug(f"check_sync_status: {data}")
    if data.get("data", {}).get("is_syncing") is True:
        print("{script_name}: Node is syncing or sync status unknown")
        return False, "syncing or unknown"
    return True, "synced"

def check_peer_count():
    data = get_json("/eth/v1/node/peer_count")
    if not data:
        print("{script_name}: Could not fetch peer count")
        return False, "N/A"
    peers = int(data["data"]["connected"])
    log_debug({"check_peer_count": peers})
    if peers < MIN_PEERS:
        print(f"{script_name}: Low peer count: {peers}")
        return False, f"{peers} peers"
    return True, f"{peers} peers"

def check_finality():
    data = get_json("/eth/v1/beacon/states/head/finality_checkpoints")
    if not data:
        print("{script_name}: Could not fetch finality checkpoints")
        return False, "N/A"
    try:
        justified = int(data["data"]["justified"]["epoch"])
        finalized = int(data["data"]["finalized"]["epoch"])
        log_debug({"check_finality": {"justified_epoch": justified, "finalized_epoch": finalized}})
    except (KeyError, TypeError, ValueError):
        print("Missing or invalid finality checkpoints")
        return False, "invalid checkpoints"

    if finalized == 0 or justified == 0:
        print("{script_name}: Finality checkpoints stuck or zero")
        return False, "zero checkpoints"
    return True, f"justified: {justified}, finalized: {finalized}"

def check_health():
    health_url = f"{BASE_URL}/eth/v1/node/health"
    try:
        r = requests.get(health_url, timeout=5)
        log_debug({"check_health": {"status_code": r.status_code}})
        if r.status_code == 200:
            return True, "HTTP 200"
        elif r.status_code == 206:
            print("{script_name}: Node is syncing (206)")
            return False, "HTTP 206"
        else:
            print(f"{script_name}: Health check failed: HTTP {r.status_code} for {health_url}")
            return False, f"{script_name}: HTTP {r.status_code}"
    except requests.exceptions.Timeout:
        print(f"{script_name}: Health check request failed: connection timed out for {health_url}")
        return False, "connection timed out"
    except Exception as e:
        print(f"{script_name}: Health check request failed: {e} for {health_url}")
        return False, "request failed"

def check_slot_timestamp():
    data = get_json("/eth/v2/beacon/blocks/head")
    if not data:
        print("{script_name}: Could not fetch head block")
        return False, "N/A"
    slot = int(data["data"]["message"]["slot"])
    timestamp = GENESIS_TIME + (slot * 12)
    now = int(time.time())
    delay = now - timestamp
    log_debug({"check_slot_timestamp": {"slot": slot, "calculated_block_timestamp": timestamp, "current_time": now, "delay_seconds": delay}})
    if delay > MAX_BLOCK_DELAY:
        print(f"{script_name}: Block is stale: delay={delay}s (slot={slot})")
        return False, f"stale delay {delay}s"
    return True, f"delay {delay}s"

def main():
    # print(f"Script called: {sys.argv}")

    checks = [
        ("Sync status", check_sync_status),
        ("PeerCount", check_peer_count),
        ("HealthEndpoint", check_health),
        ("HeadSlotTimestamp", check_slot_timestamp),
    ]

    messages = []
    failed = False
    for name, check in checks:
        result, detail = check()
        if not result:
            messages.append(f"[FAIL] {name}: {detail}")
            failed = True
        else:
            messages.append(f"[OK] {name}: {detail}")
    print(f"{script_name}: {BASE_URL} -> " + " | ".join(messages))
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    main()

