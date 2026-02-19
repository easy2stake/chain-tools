#!/usr/bin/env python3
"""
Unified eth-monitor: monitors EVM RPC block lag, optionally restarts Docker/systemd.
Reads config.yaml, runs each chain in a separate thread. Same behaviour as eth-monitor.sh.
"""

from __future__ import annotations

import argparse
import json
import platform
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
HOSTNAME = platform.node() or "unknown"

# Config defaults (match eth-monitor.sh / config-example.yaml)
DEFAULTS = {
    "interval": 10,
    "threshold": 120,
    "timeout": 2,
    "verbose": False,
    "restart_cooldown": 21600,
    "stuck_check_interval": 30,
    "dry_run": False,
    "log_dir": "./log",
    "container_log_lines": 5000,
    "container_logs": "",
    "host_log_dest": "./log/container-logs",
    "service_log_lines": 5000,
    "host_service_log_dest": "./log/service-logs",
    "telegram_token": "",
    "telegram_chat_id": "",
    "telegram_cooldown_rate_minutes": 5,
}


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        raise SystemExit("Config file is empty")
    return data


def merge_chain_config(global_cfg: dict, chain: dict, script_dir: Path) -> dict:
    merged = dict(DEFAULTS)
    merged.update(global_cfg or {})
    merged.update(chain)

    log_dir = merged.get("log_dir", "./log")
    if not Path(log_dir).is_absolute():
        log_dir = script_dir / log_dir
    else:
        log_dir = Path(log_dir)
    name = merged.get("name", "eth-monitor")
    if "log_file" not in chain:
        merged["log_file"] = str(log_dir / f"{name}.log")

    for key in ("host_log_dest", "host_service_log_dest"):
        val = merged.get(key)
        if val and not Path(val).is_absolute():
            merged[key] = str(script_dir / val)

    # Normalize lists
    for key in ("secondary_containers", "secondary_services"):
        val = merged.get(key)
        if isinstance(val, str) and val:
            merged[key] = [x.strip() for x in val.split(",") if x.strip()]
        elif not val:
            merged[key] = []

    return merged


def validate_chain(chain: dict, index: int) -> None:
    name = chain.get("name")
    if not name:
        raise SystemExit(f"Chain at index {index}: missing 'name'")
    if not chain.get("url"):
        raise SystemExit(f"Chain '{name}': missing 'url'")
    if chain.get("container") and chain.get("service"):
        raise SystemExit(f"Chain '{name}': cannot have both 'container' and 'service'")


def _send_startup_failure_telegram(merged_config: dict, message: str) -> None:
    """Send a one-off Telegram notification (e.g. startup failure). Uses merged config for token/chat_id."""
    token = merged_config.get("telegram_token") or ""
    chat_id = merged_config.get("telegram_chat_id") or ""
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": chat_id, "text": f"❌ <b>eth-monitor</b>: {message} | Host: {HOSTNAME}", "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def _hex_to_dec(hex_val: str) -> int:
    s = hex_val[2:] if hex_val.startswith("0x") else hex_val
    return int(s, 16)


def _format_timestamp(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


class ChainMonitor:
    def __init__(self, config: dict, script_dir: Path):
        self.cfg = config
        self.script_dir = script_dir
        self.last_restart_time = 0.0
        self.telegram_rate: dict[str, float] = {}
        self.chain_id: int | None = None
        self.chain_short_name = ""
        self._log_file = self.cfg.get("log_file") or str(script_dir / "log" / "eth-monitor.log")

    def _chain_prefix(self) -> str:
        if self.chain_id is not None:
            sn = f" ({self.chain_short_name})" if self.chain_short_name else ""
            return f"Chain ID: {self.chain_id}{sn}"
        return f"Chain: {self.cfg.get('name', '?')}"

    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = self._chain_prefix()
        entry = f"{prefix} | [{ts}] {msg}"
        print(entry)
        Path(self._log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(entry + "\n")

    def _rpc(self, method: str, params: list) -> dict | None:
        url = self.cfg["url"]
        timeout = self.cfg.get("timeout", 2)
        body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            self._log(f"RPC error: {e}")
            return None

    def _get_latest_block(self) -> dict | None:
        data = self._rpc("eth_getBlockByNumber", ["latest", False])
        if not data or "error" in data:
            return None
        result = data.get("result")
        if not result or result is None:
            return None
        return data

    def _is_syncing(self) -> bool:
        data = self._rpc("eth_syncing", [])
        if not data or "error" in data:
            return False
        result = data.get("result")
        if result is None or result == "null" or result == "":
            return False
        if result is False:
            return False
        return True

    def _send_telegram(self, msg: str, rate_limit_sec: int | None = None, rate_limit_id: str | None = None) -> bool:
        token = self.cfg.get("telegram_token") or ""
        chat_id = self.cfg.get("telegram_chat_id") or ""
        if not token or not chat_id:
            self._log("WARN: Telegram not configured. TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
            return False

        if rate_limit_sec and rate_limit_id:
            now = time.time()
            last = self.telegram_rate.get(rate_limit_id, 0)
            if now - last < rate_limit_sec:
                return True

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                out = json.loads(resp.read().decode())
                if out.get("ok"):
                    if rate_limit_sec and rate_limit_id:
                        self.telegram_rate[rate_limit_id] = time.time()
                    return True
        except Exception as e:
            self._log(f"WARN: Failed to send Telegram: {e}")
        return False

    def _check_block_lag(self, block_data: dict) -> bool:
        """Return True if lag exceeded (ERROR), False if OK."""
        result = block_data.get("result") or {}
        num_hex = result.get("number")
        ts_hex = result.get("timestamp")
        if not num_hex or not ts_hex or num_hex == "null" or ts_hex == "null":
            self._log(f"ERROR [{HOSTNAME}]: Failed to extract block data")
            self._send_telegram(
                f"⚠️ <b>eth-monitor</b>: Failed to extract block data from RPC ({self.cfg['url']}) | Host: {HOSTNAME}"
            )
            return True

        block_number = _hex_to_dec(num_hex)
        block_ts = _hex_to_dec(ts_hex)
        now = int(time.time())
        lag = now - block_ts
        threshold = self.cfg.get("threshold", 120)
        status = "OK" if lag <= threshold else "ERROR"
        block_time_fmt = _format_timestamp(block_ts)

        holdoff = 0
        target_label = ""
        if self.cfg.get("container"):
            tsr = now - int(self.last_restart_time)
            holdoff = max(0, self.cfg.get("restart_cooldown", 21600) - tsr)
            target_label = f" | Container: {self.cfg['container']} | Holdoff remaining: {holdoff}s"
        elif self.cfg.get("service"):
            tsr = now - int(self.last_restart_time)
            holdoff = max(0, self.cfg.get("restart_cooldown", 21600) - tsr)
            target_label = f" | Service: {self.cfg['service']} | Holdoff remaining: {holdoff}s"

        self._log(
            f"Block: {block_number} | Block Time: {block_time_fmt} | Lag: {lag}s | Status: {status}{target_label}"
        )

        if self.cfg.get("verbose"):
            self._log(json.dumps(block_data, indent=2))

        return status == "ERROR"

    def _sync_restart_time_from_target(self) -> None:
        container = self.cfg.get("container")
        service = self.cfg.get("service")
        target_type = ""
        target_name = ""
        current_started_at = 0

        if container:
            target_type, target_name = "container", container
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.StartedAt}}", container],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip() and "0001-01-01" not in r.stdout:
                try:
                    s = r.stdout.strip()
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt = datetime.fromisoformat(s)
                    current_started_at = int(dt.timestamp())
                except ValueError:
                    pass
        elif service:
            target_type, target_name = "service", service
            r = subprocess.run(
                ["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", service],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() != "n/a":
                try:
                    r2 = subprocess.run(
                        ["date", "-d", r.stdout.strip(), "+%s"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if r2.returncode == 0:
                        current_started_at = int(r2.stdout.strip())
                except (ValueError, subprocess.TimeoutExpired):
                    pass

        if not current_started_at:
            return

        cooldown = self.cfg.get("restart_cooldown", 21600)
        if current_started_at > self.last_restart_time + 10 and self.last_restart_time > 0:
            self.last_restart_time = current_started_at
            self._log(f"Detected external restart of {target_type} '{target_name}'. Cooldown applied ({cooldown}s).")
            self._send_telegram(
                f"🔄 <b>eth-monitor</b>: Detected external restart of {target_type} <b>{target_name}</b>. "
                f"Cooldown applied ({cooldown}s). | Host: {HOSTNAME}"
            )
        elif current_started_at > self.last_restart_time:
            self.last_restart_time = current_started_at

    def _copy_container_docker_logs(self, container: str, host_dest: str) -> None:
        lines = self.cfg.get("container_log_lines", 5000)
        Path(host_dest).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_file = Path(host_dest) / f"{container}_{ts}.container.log"
        r = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if container not in (r.stdout or "").splitlines():
            self._log(f"WARN: Docker container '{container}' not found, skipping docker logs copy")
            return
        self._log(f"Copying docker logs for {container} (last {lines} lines) to {dest_file}")
        r = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container],
            stdout=dest_file.open("w"),
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        if r.returncode == 0:
            prefix = "[DRY RUN] " if self.cfg.get("dry_run") else ""
            self._log(f"{prefix}Successfully copied docker logs to {dest_file}")

    def _copy_container_logs_folder(self, container: str, container_path: str, host_dest: str) -> None:
        if not container_path:
            return
        Path(host_dest).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = Path(host_dest) / f"{container}_{ts}"
        r = subprocess.run(
            ["docker", "cp", f"{container}:{container_path}", str(dest)],
            capture_output=True,
            timeout=60,
        )
        if r.returncode != 0:
            self._log(f"WARN: Failed to copy logs from container '{container}'")
        else:
            prefix = "[DRY RUN] " if self.cfg.get("dry_run") else ""
            self._log(f"{prefix}Successfully copied logs to {dest}")

    def _copy_service_logs(self, service: str, host_dest: str) -> None:
        lines = self.cfg.get("service_log_lines", 5000)
        Path(host_dest).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest_file = Path(host_dest) / f"{service}_{ts}.journal.log"
        r = subprocess.run(["systemctl", "show", service], capture_output=True, timeout=5)
        if r.returncode != 0:
            self._log(f"WARN: Systemd service '{service}' not found, skipping journal copy")
            return
        self._log(f"Copying journal for {service} (last {lines} lines) to {dest_file}")
        r = subprocess.run(
            ["journalctl", "-u", service, "-n", str(lines), "--no-pager"],
            stdout=dest_file.open("w"),
            timeout=30,
        )
        if r.returncode == 0:
            prefix = "[DRY RUN] " if self.cfg.get("dry_run") else ""
            self._log(f"{prefix}Successfully copied service journal to {dest_file}")

    def _restart_secondary_targets(self) -> None:
        dry = self.cfg.get("dry_run", False)
        for c in self.cfg.get("secondary_containers") or []:
            if dry:
                self._log(f"[DRY RUN] Would restart secondary container: {c}")
            else:
                r = subprocess.run(["docker", "restart", c], capture_output=True, timeout=30)
                self._log("Restarted secondary container: " + c if r.returncode == 0 else f"WARN: Failed to restart {c}")
        for s in self.cfg.get("secondary_services") or []:
            if dry:
                self._log(f"[DRY RUN] Would restart secondary service: {s}")
            else:
                r = subprocess.run(["systemctl", "restart", s], capture_output=True, timeout=30)
                self._log("Restarted secondary service: " + s if r.returncode == 0 else f"WARN: Failed to restart {s}")

    def _restart_container(self, container: str) -> bool:
        now = time.time()
        cooldown = self.cfg.get("restart_cooldown", 21600)
        tsr = now - self.last_restart_time
        if tsr < cooldown:
            self._log(f"WARN: Restart cooldown active. Last restart was {int(tsr)}s ago (cooldown: {cooldown}s). Skipping.")
            rate_sec = (self.cfg.get("telegram_cooldown_rate_minutes") or 5) * 60
            self._send_telegram(
                f"⏳ <b>eth-monitor</b>: Restart skipped (cooldown). Container: {container}. "
                f"Last restart {int(tsr)}s ago (cooldown: {cooldown}s). | Host: {HOSTNAME}",
                rate_limit_sec=rate_sec,
                rate_limit_id="cooldown_skip",
            )
            return False

        host_dest = self.cfg.get("host_log_dest") or str(self.script_dir / "log" / "container-logs")
        self._copy_container_docker_logs(container, host_dest)
        if self.cfg.get("container_logs"):
            self._copy_container_logs_folder(container, self.cfg["container_logs"], host_dest)

        dry = self.cfg.get("dry_run", False)
        url = self.cfg["url"]
        if dry:
            self._log(f"[DRY RUN] Would restart Docker container: {container}")
            self._restart_secondary_targets()
            self._send_telegram(
                f"🔄 <b>eth-monitor</b> [DRY RUN]: Would restart container: {container} "
                f"(block lag exceeded on {url}) | Host: {HOSTNAME}"
            )
            self.last_restart_time = now
            return True

        self._log(f"Restarting Docker container: {container}")
        self._send_telegram(
            f"🔄 <b>eth-monitor</b>: Restarting container: {container} (block lag exceeded on {url}) | Host: {HOSTNAME}"
        )
        r = subprocess.run(["docker", "restart", container], capture_output=True, timeout=60)
        if r.returncode == 0:
            self.last_restart_time = now
            self._log(f"Successfully restarted Docker container: {container}")
            self._restart_secondary_targets()
            self._send_telegram(f"✅ <b>eth-monitor</b>: Successfully restarted container: {container} | Host: {HOSTNAME}")
            return True
        else:
            self._log(f"ERROR [{HOSTNAME}]: Failed to restart Docker container: {container}")
            self._send_telegram(f"❌ <b>eth-monitor</b>: Failed to restart container: {container} | Host: {HOSTNAME}")
            return False

    def _restart_service(self, service: str) -> bool:
        now = time.time()
        cooldown = self.cfg.get("restart_cooldown", 21600)
        tsr = now - self.last_restart_time
        if tsr < cooldown:
            self._log(f"WARN: Restart cooldown active. Last restart was {int(tsr)}s ago (cooldown: {cooldown}s). Skipping.")
            rate_sec = (self.cfg.get("telegram_cooldown_rate_minutes") or 5) * 60
            self._send_telegram(
                f"⏳ <b>eth-monitor</b>: Restart skipped (cooldown). Service: {service}. "
                f"Last restart {int(tsr)}s ago (cooldown: {cooldown}s). | Host: {HOSTNAME}",
                rate_limit_sec=rate_sec,
                rate_limit_id="cooldown_skip",
            )
            return False

        host_dest = self.cfg.get("host_service_log_dest") or str(self.script_dir / "log" / "service-logs")
        self._copy_service_logs(service, host_dest)

        dry = self.cfg.get("dry_run", False)
        url = self.cfg["url"]
        if dry:
            self._log(f"[DRY RUN] Would restart systemd service: {service}")
            self._restart_secondary_targets()
            self._send_telegram(
                f"🔄 <b>eth-monitor</b> [DRY RUN]: Would restart service: {service} "
                f"(block lag exceeded on {url}) | Host: {HOSTNAME}"
            )
            self.last_restart_time = now
            return True

        self._log(f"Restarting systemd service: {service}")
        self._send_telegram(
            f"🔄 <b>eth-monitor</b>: Restarting service: {service} (block lag exceeded on {url}) | Host: {HOSTNAME}"
        )
        r = subprocess.run(["systemctl", "restart", service], capture_output=True, timeout=60)
        if r.returncode == 0:
            self.last_restart_time = now
            self._log(f"Successfully restarted systemd service: {service}")
            self._restart_secondary_targets()
            self._send_telegram(f"✅ <b>eth-monitor</b>: Successfully restarted service: {service} | Host: {HOSTNAME}")
            return True
        else:
            self._log(f"ERROR [{HOSTNAME}]: Failed to restart systemd service: {service}")
            self._send_telegram(f"❌ <b>eth-monitor</b>: Failed to restart service: {service} | Host: {HOSTNAME}")
            return False

    def _validate_endpoint(self) -> bool:
        url = self.cfg["url"]
        self._log(f"Validating RPC endpoint: {url}")
        data = self._rpc("eth_blockNumber", [])
        if not data:
            self._log(f"ERROR [{HOSTNAME}]: Failed to connect to RPC endpoint")
            self._send_telegram(f"❌ <b>eth-monitor</b>: Startup validation failed — cannot connect to RPC {url} | Host: {HOSTNAME}")
            return False
        if "error" in data:
            err = data["error"].get("message", "Unknown error")
            self._log(f"ERROR [{HOSTNAME}]: RPC error: {err}")
            self._send_telegram(f"❌ <b>eth-monitor</b>: Startup validation failed — RPC error: {err} ({url}) | Host: {HOSTNAME}")
            return False
        if not data.get("result"):
            self._log(f"ERROR [{HOSTNAME}]: Invalid response from RPC endpoint")
            self._send_telegram(f"❌ <b>eth-monitor</b>: Startup validation failed — invalid RPC response from {url} | Host: {HOSTNAME}")
            return False

        data = self._rpc("eth_chainId", [])
        if data and "result" in data and data["result"]:
            self.chain_id = _hex_to_dec(data["result"])
            rpcs_path = self.script_dir / "rpcs.json"
            if rpcs_path.exists():
                try:
                    with open(rpcs_path, encoding="utf-8") as f:
                        chains = json.load(f)
                    for c in chains:
                        if c.get("chainId") == self.chain_id:
                            self.chain_short_name = c.get("shortName", "") or ""
                            break
                except (json.JSONDecodeError, TypeError):
                    pass
            self._log(f"Chain ID: {self.chain_id}" + (f" ({self.chain_short_name})" if self.chain_short_name else ""))
        self._log("RPC endpoint validated successfully")
        return True

    def _send_telegram_startup(self) -> None:
        holdoff = "—"
        if self.cfg.get("container") or self.cfg.get("service"):
            now = time.time()
            tsr = now - self.last_restart_time
            cooldown = self.cfg.get("restart_cooldown", 21600)
            if self.last_restart_time > 0 and tsr < cooldown:
                holdoff = f"active ({int(tsr)}s since restart)"
            else:
                holdoff = "inactive"

        target = "—"
        if self.cfg.get("container"):
            target = f"Container: {self.cfg['container']}"
        elif self.cfg.get("service"):
            target = f"Service: {self.cfg['service']}"

        dry = "\n<b>DRY RUN</b>" if self.cfg.get("dry_run") else ""
        chain_line = ""
        if self.chain_id:
            chain_line = f"\n<b>Chain ID:</b> {self.chain_id}" + (f" ({self.chain_short_name})" if self.chain_short_name else "")
        msg = (
            f"🟢 <b>eth-monitor started</b>{dry}\n"
            f"<b>Host:</b> {HOSTNAME}\n"
            f"<b>RPC:</b> {self.cfg['url']}{chain_line}\n"
            f"<b>Interval:</b> {self.cfg.get('interval', 10)}s · <b>Threshold:</b> {self.cfg.get('threshold', 120)}s\n"
            f"<b>Target:</b> {target}\n"
            f"<b>Cooldown:</b> {self.cfg.get('restart_cooldown', 21600)}s · <b>Holdoff:</b> {holdoff}\n"
            f"<b>Log:</b> {self._log_file}"
        )
        self._send_telegram(msg)

    def run(self, shutdown: threading.Event) -> None:
        try:
            self._sync_restart_time_from_target()

            if not self._validate_endpoint():
                return

            # Log last restart time and holdoff at startup (match eth-monitor.sh)
            container = self.cfg.get("container")
            service = self.cfg.get("service")
            cooldown = self.cfg.get("restart_cooldown", 21600)
            if container or service:
                if self.last_restart_time > 0:
                    self._log(f"Last restart time: {_format_timestamp(int(self.last_restart_time))}")
                    now = time.time()
                    tsr = now - self.last_restart_time
                    if tsr < cooldown:
                        self._log(f"Holdoff: active ({int(tsr)}s since restart, cooldown {cooldown}s)")
                    else:
                        self._log(f"Holdoff: inactive ({int(tsr)}s since restart, cooldown {cooldown}s)")
                else:
                    self._log("Last restart time: unknown (no previous start time from target)")
                    self._log("Holdoff: inactive")

            self._send_telegram_startup()

            interval = self.cfg.get("interval", 10)
            threshold = self.cfg.get("threshold", 120)
            stuck = self.cfg.get("stuck_check_interval", 30)

            self._log(f"Starting continuous monitoring (interval: {interval}s, threshold: {threshold}s)")
            self._log(f"Log file: {self._log_file}")
            if self.cfg.get("dry_run"):
                self._log("DRY RUN MODE: Container/service restarts will be simulated only")

            # Detailed startup logs when container or service is set (match eth-monitor.sh)
            if container:
                if self.cfg.get("dry_run"):
                    self._log(f"Docker container restart enabled (DRY RUN): {container} (cooldown: {cooldown}s)")
                else:
                    self._log(f"Docker container restart enabled: {container} (cooldown: {cooldown}s)")
                self._log(
                    f"Container log capture: last {self.cfg.get('container_log_lines', 5000)} docker log lines -> "
                    f"{self.cfg.get('host_log_dest', '')}"
                )
                if self.cfg.get("container_logs"):
                    self._log(
                        f"Container folder copy enabled: {self.cfg['container_logs']} -> "
                        f"{self.cfg.get('host_log_dest', '')}"
                    )
                sec_c = self.cfg.get("secondary_containers") or []
                sec_s = self.cfg.get("secondary_services") or []
                if sec_c or sec_s:
                    self._log(f"Secondary targets: containers={sec_c} services={sec_s}")
                self._log(
                    f"Restart only if block stuck: eth_syncing=false and block unchanged for {stuck}s"
                )
            if service:
                if self.cfg.get("dry_run"):
                    self._log(f"Systemd service restart enabled (DRY RUN): {service} (cooldown: {cooldown}s)")
                else:
                    self._log(f"Systemd service restart enabled: {service} (cooldown: {cooldown}s)")
                self._log(
                    f"Service journal capture: last {self.cfg.get('service_log_lines', 5000)} lines -> "
                    f"{self.cfg.get('host_service_log_dest', '')}"
                )
                sec_c = self.cfg.get("secondary_containers") or []
                sec_s = self.cfg.get("secondary_services") or []
                if sec_c or sec_s:
                    self._log(f"Secondary targets: containers={sec_c} services={sec_s}")
                self._log(
                    f"Restart only if block stuck: eth_syncing=false and block unchanged for {stuck}s"
                )

            while not shutdown.is_set():
                self._sync_restart_time_from_target()
                block_data = self._get_latest_block()

                if block_data:
                    lag_exceeded = self._check_block_lag(block_data)

                    if lag_exceeded:
                        if container or service:
                            if self._is_syncing():
                                self._log("Node is syncing. Skipping restart (lag during sync is expected).")
                            else:
                                self._log(f"Node not syncing. Checking if block is stuck (waiting {stuck}s)...")
                                initial_hex = (block_data.get("result") or {}).get("number", "")
                                shutdown.wait(timeout=stuck)
                                if shutdown.is_set():
                                    break
                                new_data = self._get_latest_block()
                                if not new_data:
                                    self._log("WARN: Failed to fetch block after stuck-check wait. Skipping restart this cycle.")
                                else:
                                    new_hex = (new_data.get("result") or {}).get("number", "")
                                    if initial_hex != new_hex:
                                        self._log(f"Block moved from {initial_hex} to {new_hex}. Chain is active. No restart needed.")
                                    else:
                                        self._log(f"Block stuck at {initial_hex} for {stuck}s. Proceeding with restart.")
                                        if container:
                                            self._restart_container(container)
                                        else:
                                            self._restart_service(service)
                        else:
                            self._send_telegram(
                                f"⚠️ <b>eth-monitor</b>: Block lag exceeded threshold "
                                f"({self.cfg.get('threshold', 120)}s) on {self.cfg['url']} | Host: {HOSTNAME}"
                            )
                else:
                    self._log(f"ERROR [{HOSTNAME}]: Failed to fetch latest block")
                    # Build detailed message with chain info
                    chain_info_parts = []
                    if self.chain_id is not None:
                        chain_info_parts.append(f"Chain ID: {self.chain_id}")
                        if self.chain_short_name:
                            chain_info_parts[-1] += f" ({self.chain_short_name})"
                    chain_name = self.cfg.get('name', 'unknown')
                    chain_info_parts.append(f"Chain: {chain_name}")
                    container = self.cfg.get('container')
                    service = self.cfg.get('service')
                    if container:
                        chain_info_parts.append(f"Container: {container}")
                    elif service:
                        chain_info_parts.append(f"Service: {service}")
                    chain_info = " | ".join(chain_info_parts)
                    self._send_telegram(
                        f"⚠️ <b>eth-monitor</b>: Failed to fetch latest block from {self.cfg['url']} | {chain_info} | Host: {HOSTNAME}"
                    )

                shutdown.wait(timeout=interval)
        except Exception as e:
            self._log(f"Monitor {self.cfg.get('name', '?')} error: {e}")
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified eth-monitor from config.yaml")
    parser.add_argument("-c", "--config", type=Path, default=SCRIPT_DIR / "config.yaml", help="Path to YAML config")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry run (pass to all chains)")
    parser.add_argument("--chain", help="Run only this chain by name")
    args = parser.parse_args()

    config_path = args.config.resolve()
    if not config_path.exists():
        print(f"ERROR: Config not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    data = load_config(config_path)
    global_cfg = data.get("global") or {}
    chains = data.get("chains") or []
    if not chains:
        print("ERROR: No chains defined in config", file=sys.stderr)
        sys.exit(1)

    for i, ch in enumerate(chains):
        validate_chain(ch, i)

    if args.chain:
        chains = [c for c in chains if c.get("name") == args.chain]
        if not chains:
            print(f"ERROR: Chain '{args.chain}' not found", file=sys.stderr)
            sys.exit(1)

    if args.dry_run:
        for ch in chains:
            merged = merge_chain_config(global_cfg, ch, SCRIPT_DIR)
            merged["dry_run"] = True
            print(f"[dry-run] {merged.get('name')}: would run monitor for {merged.get('url')}")
        return

    monitors = []
    for ch in chains:
        merged = merge_chain_config(global_cfg, ch, SCRIPT_DIR)
        name = merged.get("name", "?")
        container = merged.get("container")
        service = merged.get("service")

        if container:
            r = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=5)
            if container not in (r.stdout or "").splitlines():
                print(f"ERROR: Docker container '{container}' not found - skipping chain '{name}'", file=sys.stderr)
                _send_startup_failure_telegram(merged, f"Startup failed — Docker container <b>{container}</b> not found (skipping chain <b>{name}</b>)")
                continue
        if service:
            r = subprocess.run(["systemctl", "show", service], capture_output=True, timeout=5)
            if r.returncode != 0:
                print(f"ERROR: Systemd service '{service}' not found - skipping chain '{name}'", file=sys.stderr)
                _send_startup_failure_telegram(merged, f"Startup failed — Systemd service <b>{service}</b> not found (skipping chain <b>{name}</b>)")
                continue

        monitors.append(ChainMonitor(merged, SCRIPT_DIR))

    if not monitors:
        print("ERROR: No chains started (all skipped due to missing container/service)", file=sys.stderr)
        sys.exit(1)

    shutdown = threading.Event()

    def on_signal(signum: int, frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    threads = [threading.Thread(target=m.run, args=(shutdown,)) for m in monitors]
    for t in threads:
        t.start()
    print(f"Running {len(threads)} monitor(s). Ctrl+C to stop all.\n")
    for t in threads:
        t.join()
    print("Monitoring stopped")


if __name__ == "__main__":
    main()
