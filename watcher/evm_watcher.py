#!/usr/bin/env python3

import requests
import time
import json
import sys
import subprocess
import os
from typing import Optional, Dict, Any, Union

class EVMWatcher:
    def __init__(self, config_path: str, state_path: str = "watcher_state.json"):
        self.config = self._load_config(config_path)
        self.state_path = state_path
        self.state = self._load_state()
        
        self.global_settings = self.config.get("global_settings", {})
        self.timeout = self.global_settings.get("request_timeout", 2)
        self.max_retries = self.global_settings.get("max_retries", 3)
        self.retry_backoff = self.global_settings.get("retry_backoff", 1)
        self.default_lag_threshold = self.global_settings.get("default_lag_threshold", 60)
        self.stuck_check_interval = self.global_settings.get("stuck_check_interval", 30)
        self.min_restart_interval = self.global_settings.get("min_restart_interval", 600)

    def _load_config(self, path: str) -> Dict[str, Any]:
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config file {path}: {e}")
            sys.exit(1)

    def _load_state(self) -> Dict[str, Any]:
        if not os.path.exists(self.state_path):
            return {}
        try:
            with open(self.state_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading state file {self.state_path}: {e}")
            return {}

    def _save_state(self):
        try:
            with open(self.state_path, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f"Error saving state file {self.state_path}: {e}")

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
                    print(f"RPC Error from {url}: {data['error']}")
                    return None
                
                return data.get("result")

            except requests.exceptions.RequestException as e:
                print(f"Attempt {attempt}/{self.max_retries} failed for {url}: {e}")
                if attempt < self.max_retries:
                    time.sleep(self.retry_backoff)
                else:
                    print(f"All {self.max_retries} attempts failed for {url}.")
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
        # eth_syncing returns False (boolean) when synced, or an object when syncing.
        return result

    def trigger_restart(self, chain_id: int):
        """
        Executes the restart command for the specific chain ID.
        """
        chain_config = self.config.get("chains", {}).get(str(chain_id))
        
        if not chain_config:
            print(f"No configuration found for Chain ID {chain_id}. Skipping restart.")
            return

        command = chain_config.get("restart_command")
        if not command:
            print(f"No restart command defined for Chain ID {chain_id}.")
            return

        print(f"Executing restart command for Chain ID {chain_id}: {command}")
        try:
            # Using shell=True to allow complex commands like 'systemctl restart ...'
            subprocess.run(command, shell=True, check=True)
            print(f"Restart command executed successfully for Chain ID {chain_id}.")
            
            # Update state
            self.state[str(chain_id)] = {"last_restart": int(time.time())}
            self._save_state()
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to execute restart command for Chain ID {chain_id}: {e}")

    def check_endpoint(self, url: str):
        """
        Checks the endpoint with 3 conditions:
        1. Not syncing (or explicitly False) AND Lagging
        2. Stuck (block number not increasing after interval)
        3. Restart cooldown passed
        """
        print(f"\nChecking endpoint: {url}")
        
        # 1. Get Chain ID
        chain_id = self.get_chain_id(url)
        if chain_id is None:
            print(f"Failed to fetch Chain ID from {url}. Skipping.")
            return

        print(f"Chain ID: {chain_id}")

        # --- CONDITION 1: Sync Status & Lag ---
        sync_status = self.get_sync_status(url)
                
        if sync_status is not False: 
            print(f"Chain {chain_id} is currently syncing. Skipping checks.")
            return

        # Get Last Block
        block = self.get_last_block(url)
        if not block:
            print(f"Failed to fetch latest block from {url}. Skipping.")
            return

        timestamp_hex = block.get("timestamp")
        if not timestamp_hex:
            print(f"Block data missing timestamp from {url}.")
            return

        block_timestamp = int(timestamp_hex, 16)
        current_time = int(time.time())
        lag = current_time - block_timestamp
        
        print(f"Block Timestamp: {block_timestamp} (Lag: {lag}s)")

        chain_config = self.config.get("chains", {}).get(str(chain_id), {})
        threshold = chain_config.get("lag_threshold", self.default_lag_threshold)

        if lag <= threshold:
            print(f"[OK] Chain {chain_id} is healthy.")
            return
            
        print(f"[WARN] Condition 1 Met: Chain {chain_id} is lagging by {lag}s (Threshold: {threshold}s) and not syncing.")

        # --- CONDITION 2: Stuck Check ---
        print(f"Checking Condition 2: Waiting {self.stuck_check_interval}s to confirm block is stuck...")
        
        initial_block_number_hex = block.get("number")
        if not initial_block_number_hex:
             print("Could not get block number. Aborting.")
             return
             
        time.sleep(self.stuck_check_interval)
        
        # Fetch block again
        new_block = self.get_last_block(url)
        if not new_block:
             print("Failed to fetch second block check. Aborting.")
             return
             
        new_block_number_hex = new_block.get("number")
        
        if initial_block_number_hex != new_block_number_hex:
            print(f"[INFO] Block moved from {int(initial_block_number_hex, 16)} to {int(new_block_number_hex, 16)}. Chain is active. No restart needed.")
            return
            
        print(f"[WARN] Condition 2 Met: Block number stuck at {int(initial_block_number_hex, 16)} for {self.stuck_check_interval}s.")

        # --- CONDITION 3: Restart Cooldown ---
        last_restart = self.state.get(str(chain_id), {}).get("last_restart", 0)
        time_since_restart = current_time - last_restart
        
        if time_since_restart < self.min_restart_interval:
            print(f"[INFO] Restart cooldown active. Last restart was {time_since_restart}s ago (Min interval: {self.min_restart_interval}s). Skipping.")
            return
            
        print(f"[WARN] Condition 3 Met: Last restart was {time_since_restart}s ago.")
        
        # All conditions met
        print(f"[ALERT] All conditions met for Chain {chain_id}. Initiating restart...")
        self.trigger_restart(chain_id)

    def run(self):
        """
        Main execution loop.
        """
        urls = self.config.get("urls", [])
        if not urls:
            print("No URLs configured to watch.")
            return

        for url in urls:
            try:
                self.check_endpoint(url)
            except Exception as e:
                print(f"Unexpected error checking {url}: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        config_file = "watcher_config.json"
    else:
        config_file = sys.argv[1]
        
    watcher = EVMWatcher(config_file)
    watcher.run()
