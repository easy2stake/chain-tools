#!/usr/bin/env python3

import requests
import time
import json
import sys
import subprocess
import os
import argparse
from datetime import datetime
from typing import Optional, Dict, Any, Union

class EVMWatcher:
    def __init__(self, config_path: str, dry_run: bool = False):
        self.config = self._load_config(config_path)
        self.dry_run = dry_run
        
        self.global_settings = self.config.get("global_settings", {})
        self.timeout = self.global_settings.get("request_timeout", 2)
        self.max_retries = self.global_settings.get("max_retries", 3)
        self.retry_backoff = self.global_settings.get("retry_backoff", 1)
        self.default_lag_threshold = self.global_settings.get("default_lag_threshold", 60)
        self.stuck_check_interval = self.global_settings.get("stuck_check_interval", 30)
        self.min_restart_interval = self.global_settings.get("min_restart_interval", 600)

    def log(self, message: str):
        """
        Prints a message with a human-readable timestamp.
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {message}")

    def _load_config(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config file {path}: {e}")
            sys.exit(1)

    def make_rpc_call(self, url: str, method: str, params: list = []) -> Optional[Any]:
        """
        Makes a JSON-RPC call with configurable retry mechanism.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1
        }
        headers = {"Content-Type": "application/json"}

        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(
                    url, 
                    json=payload, 
                    headers=headers, 
                    timeout=self.timeout
                )
                response.raise_for_status()
                data = response.json()
                
                if "error" in data:
                    self.log(f"RPC Error from {url}: {data['error']}")
                    return None
                
                return data.get("result")

            except requests.exceptions.RequestException as e:
                self.log(f"Attempt {attempt}/{self.max_retries} failed for {url}: {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff)
                else:
                    self.log(f"All {self.max_retries} attempts failed for {url}.")
                    return None

    def get_chain_id(self, url: str) -> Optional[int]:
        """
        Fetches the Chain ID.
        """
        result = self.make_rpc_call(url, "eth_chainId")
        if result:
            return int(result, 16)
        return None

    def get_last_block(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Fetches the latest block details.
        """
        return self.make_rpc_call(url, "eth_getBlockByNumber", ["latest", False])

    def get_sync_status(self, url: str) -> Union[bool, Dict[str, Any]]:
        """
        Fetches the sync status. Returns False if not syncing, or the sync object if syncing.
        """
        result = self.make_rpc_call(url, "eth_syncing")
        return result

    def get_block_timestamp(self, block: Dict[str, Any]) -> Optional[int]:
        """
        Extracts and converts the timestamp from a block dictionary.
        """
        timestamp_hex = block.get("timestamp")
        if not timestamp_hex:
            return None
        return int(timestamp_hex, 16)

    def get_last_restart_time(self, installation_type: str, unit_name: str) -> int:
        """
        Gets the last restart timestamp (Unix epoch) for the given unit/container.
        Returns 0 if fails.
        """
        try:
            if installation_type == "docker":
                # Format: 2025-11-20T19:36:11.097279179Z
                cmd = ["docker", "inspect", unit_name, "--format", "{{.State.StartedAt}}"]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                raw_time = result.stdout.strip().replace('"', '')
                
                # Python's fromisoformat doesn't handle 'Z' or nanoseconds perfectly in older versions.
                # Truncate nanoseconds to microseconds for compatibility if needed, or handle manually.
                # Simpler approach: replace Z with +00:00 and truncate fractional seconds if too long
                if "." in raw_time:
                     # Drop everything after the dot to ignore sub-seconds
                     # 2025-11-20T19:36:11.097279179Z -> 2025-11-20T19:36:11
                     raw_time = raw_time.split(".")[0]
                     # Add UTC offset manually since Z was stripped if it was at the end
                     raw_time = raw_time + "+00:00"
                else:
                     raw_time = raw_time.rstrip("Z") + "+00:00"
                
                dt = datetime.fromisoformat(raw_time)
                return int(dt.timestamp())

            elif installation_type == "systemd":
                # Format depends on locale (e.g., "Fri 2025-11-14 14:01:03 CET").
                # We use `date -d` to parse it safely into an epoch timestamp.
                
                cmd = ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", unit_name]
                result = subprocess.run(cmd, capture_output=True, text=True, check=True)
                raw_time = result.stdout.strip()
                
                if not raw_time or raw_time == "n/a":
                    return 0
                
                # Parsing systemd time format can be tricky across locales.
                # Trying standard approach with `date` command to convert to epoch
                date_cmd = ["date", "-d", raw_time, "+%s"]
                date_res = subprocess.run(date_cmd, capture_output=True, text=True)
                
                if date_res.returncode == 0:
                    return int(date_res.stdout.strip())
                else:
                    self.log(f"Failed to parse systemd date: {raw_time}")
                    return 0
                    
        except Exception as e:
            self.log(f"Error fetching last restart time for {unit_name} ({installation_type}): {e}")
            return 0
        
        return 0

    def get_chain_config(self, chain_id: int) -> Dict[str, Any]:
        """
        Retrieves the configuration for a specific chain ID.
        """
        return self.config.get("chains", {}).get(str(chain_id), {})

    def trigger_restart(self, chain_id: int):
        """
        Executes the restart command dynamically based on config.
        """
        chain_config = self.get_chain_config(chain_id)
        
        if not chain_config:
            self.log(f"No configuration found for Chain ID {chain_id}. Skipping restart.")
            return

        install_type = chain_config.get("installation_type")
        unit_name = chain_config.get("unit_name")

        if not install_type or not unit_name:
            self.log(f"Missing installation_type or unit_name for Chain ID {chain_id}. Skipping.")
            return

        if install_type == "docker":
            command = f"docker restart {unit_name}"
        elif install_type == "systemd":
            command = f"systemctl restart {unit_name}"
        else:
            self.log(f"Unknown installation type: {install_type} for Chain ID {chain_id}")
            return

        if self.dry_run:
            self.log(f"[DRY RUN] Would execute restart command for Chain ID {chain_id}: {command}")
            return

        self.log(f"Executing restart command for Chain ID {chain_id}: {command}")
        try:
            subprocess.run(command, shell=True, check=True)
            self.log(f"Restart command executed successfully for Chain ID {chain_id}.")
        except subprocess.CalledProcessError as e:
            self.log(f"Failed to execute restart command for Chain ID {chain_id}: {e}")

    def check_endpoint(self, url: str):
        """
        Checks the endpoint with 3 conditions:
        1. Not syncing (or explicitly False) AND Lagging
        2. Stuck (block number not increasing after interval)
        3. Restart cooldown passed (checked against live process status)
        """
        self.log(f"Checking endpoint: {url}")
        
        # 1. Get Chain ID
        chain_id = self.get_chain_id(url)
        if chain_id is None:
            self.log(f"Failed to fetch Chain ID from {url}. Skipping.")
            return

        # Lookup name from config
        chain_config = self.get_chain_config(chain_id)
        chain_name = chain_config.get("name", "Unknown")
        
        self.log(f"Chain ID: {chain_id} ({chain_name})")

        # --- CONDITION 1: Sync Status & Lag ---
        sync_status = self.get_sync_status(url)
        
        if sync_status is not False: 
            self.log(f"Chain {chain_id} is currently syncing. Skipping checks.")
            return

        # Get Last Block
        block = self.get_last_block(url)
        if not block:
            self.log(f"Failed to fetch latest block from {url}. Skipping.")
            return

        block_timestamp = self.get_block_timestamp(block)
        if block_timestamp is None:
            self.log(f"Block data missing timestamp from {url}.")
            return

        current_time = int(time.time())
        lag = current_time - block_timestamp
        
        self.log(f"Block Timestamp: {block_timestamp} (Lag: {lag}s)")

        chain_config = self.get_chain_config(chain_id)
        threshold = chain_config.get("lag_threshold", self.default_lag_threshold)

        if lag <= threshold:
            self.log(f"[OK] Chain {chain_id} is healthy.")
            return
            
        self.log(f"[WARN] Condition 1 Met: Chain {chain_id} is lagging by {lag}s (Threshold: {threshold}s) and not syncing.")

        # --- CONDITION 2: Stuck Check ---
        self.log(f"Checking Condition 2: Waiting {self.stuck_check_interval}s to confirm block is stuck...")
        
        initial_block_number_hex = block.get("number")
        if not initial_block_number_hex:
             self.log("Could not get block number. Aborting.")
             return
             
        time.sleep(self.stuck_check_interval)
        
        # Fetch block again
        new_block = self.get_last_block(url)
        if not new_block:
             self.log("Failed to fetch second block check. Aborting.")
             return
             
        new_block_number_hex = new_block.get("number")
        
        if initial_block_number_hex != new_block_number_hex:
            self.log(f"[INFO] Block moved from {int(initial_block_number_hex, 16)} to {int(new_block_number_hex, 16)}. Chain is active. No restart needed.")
            return
            
        self.log(f"[WARN] Condition 2 Met: Block number stuck at {int(initial_block_number_hex, 16)} for {self.stuck_check_interval}s.")

        # --- CONDITION 3: Restart Cooldown (Live Check) ---
        install_type = chain_config.get("installation_type")
        unit_name = chain_config.get("unit_name")
        
        if not install_type or not unit_name:
            self.log(f"Missing configuration (installation_type/unit_name) for Chain {chain_id}. Cannot check process time.")
            return

        last_restart = self.get_last_restart_time(install_type, unit_name)
        if last_restart == 0:
             self.log(f"[WARN] Could not determine last restart time for {unit_name}. Assuming safe to restart (careful!).")
             # Option: return here if you want to be conservative.
             # Proceeding for now.
        
        time_since_restart = current_time - last_restart
        # Check configured min interval for this chain or global default
        min_restart_interval = chain_config.get("min_restart_interval", self.min_restart_interval)
        
        if time_since_restart < min_restart_interval:
            self.log(f"[INFO] Restart cooldown active. Last process start was {time_since_restart}s ago (Min interval: {min_restart_interval}s). Skipping.")
            return
            
        self.log(f"[WARN] Condition 3 Met: Last process start was {time_since_restart}s ago.")
        
        # All conditions met
        self.log(f"[ALERT] All conditions met for Chain {chain_id}. Initiating restart...")
        self.trigger_restart(chain_id)

    def run(self):
        """
        Main execution loop.
        """
        urls = self.config.get("urls", [])
        if not urls:
            self.log("No URLs configured to watch.")
            return

        for url in urls:
            try:
                self.check_endpoint(url)
            except Exception as e:
                self.log(f"Unexpected error checking {url}: {e}")

def parse_arguments():
    # Get the directory where the script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_config = os.path.join(script_dir, "watcher_config.json")

    parser = argparse.ArgumentParser(description="EVM Chain Watcher: Monitors EVM endpoints and restarts stuck/lagging chains.")
    parser.add_argument("config", nargs="?", default=default_config, help=f"Path to the configuration file (default: {default_config})")
    parser.add_argument("--dry-run", action="store_true", help="Perform checks without executing restart commands")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_arguments()
    watcher = EVMWatcher(args.config, dry_run=args.dry_run)
    watcher.run()
