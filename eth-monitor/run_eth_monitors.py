#!/usr/bin/env python3
"""
Wrapper to run multiple eth-monitor.sh instances from a YAML config.
Global defaults are merged with per-chain overrides; each chain runs as a separate process.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# Mapping from config keys to eth-monitor CLI args (or env var name if env_only)
CONFIG_TO_CLI = {
    "url": ("--url", None),
    "interval": ("--interval", None),
    "threshold": ("--threshold", None),
    "log_file": ("--log-file", None),
    "container": ("--container", None),
    "service": ("--service", None),
    "container_logs": ("--container-logs", None),
    "host_log_dest": ("--host-log-dest", None),
    "service_log_lines": ("--service-log-lines", None),
    "host_service_log_dest": ("--host-service-log-dest", None),
    "telegram_token": ("--telegram-token", None),
    "telegram_chat_id": ("--telegram-chat-id", None),
}
CONFIG_TO_ENV = {
    "timeout": "TIMEOUT",
    "verbose": "VERBOSE",
    "restart_cooldown": "RESTART_COOLDOWN",
    "dry_run": "DRY_RUN",
    "telegram_cooldown_rate_minutes": "TELEGRAM_COOLDOWN_RATE_MINUTES",
}


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        raise SystemExit("Config file is empty")
    return data


def merge_chain_config(global_cfg: dict, chain: dict, script_dir: Path) -> dict:
    """Merge global defaults with per-chain overrides. Resolve paths relative to script dir."""
    merged = dict(global_cfg)
    merged.update(chain)

    # Resolve log_file if not explicitly set
    log_dir = merged.get("log_dir", "./log")
    if not Path(log_dir).is_absolute():
        log_dir = str(script_dir / log_dir)
    name = merged.get("name", "eth-monitor")
    if "log_file" not in chain:
        merged["log_file"] = f"{log_dir}/{name}.log"

    # Resolve relative paths to host_log_dest, host_service_log_dest
    for key in ("host_log_dest", "host_service_log_dest"):
        val = merged.get(key)
        if val and not Path(val).is_absolute():
            merged[key] = str(script_dir / val)

    return merged


def build_args_and_env(cfg: dict, script_path: Path) -> tuple[list[str], dict[str, str]]:
    """Build eth-monitor.sh CLI args and env from merged config."""
    args = [str(script_path)]
    env = os.environ.copy()

    # CLI args
    for key, (arg_name, _) in CONFIG_TO_CLI.items():
        val = cfg.get(key)
        if val is None or val == "":
            continue
        args.extend([arg_name, str(val)])

    # Boolean flags
    if cfg.get("dry_run"):
        args.append("--dry-run")
    if cfg.get("verbose"):
        args.append("-v")

    # Env vars (no CLI in eth-monitor.sh for these)
    for key, env_name in CONFIG_TO_ENV.items():
        val = cfg.get(key)
        if val is not None and val != "":
            if isinstance(val, bool):
                env[env_name] = "true" if val else "false"
            else:
                env[env_name] = str(val)

    # Telegram from config (override env if set)
    if cfg.get("telegram_token"):
        env["TELEGRAM_BOT_TOKEN"] = str(cfg["telegram_token"])
    if cfg.get("telegram_chat_id"):
        env["TELEGRAM_CHAT_ID"] = str(cfg["telegram_chat_id"])

    return args, env


def validate_chain(chain: dict, index: int) -> None:
    """Ensure chain has required fields and valid combinations."""
    name = chain.get("name")
    if not name:
        raise SystemExit(f"Chain at index {index}: missing 'name'")
    url = chain.get("url")
    if not url:
        raise SystemExit(f"Chain '{name}': missing 'url'")
    container = chain.get("container")
    service = chain.get("service")
    if container and service:
        raise SystemExit(f"Chain '{name}': cannot have both 'container' and 'service'")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run multiple eth-monitor.sh instances from a YAML config"
    )
    parser.add_argument(
        "-c", "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "config.yaml",
        help="Path to YAML config file",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Print what would be run, do not start processes",
    )
    parser.add_argument(
        "--script",
        type=Path,
        default=Path(__file__).resolve().parent / "eth-monitor.sh",
        help="Path to eth-monitor.sh",
    )
    args = parser.parse_args()

    script_path = args.script.resolve()
    if not script_path.exists():
        print(f"ERROR: eth-monitor.sh not found at {script_path}", file=sys.stderr)
        sys.exit(1)

    script_dir = script_path.parent

    config_path = args.config
    if not config_path.exists():
        print(f"ERROR: Config not found at {config_path}", file=sys.stderr)
        sys.exit(1)

    data = load_config(config_path.resolve())

    global_cfg = data.get("global") or {}
    chains = data.get("chains") or []
    if not chains:
        print("ERROR: No chains defined in config", file=sys.stderr)
        sys.exit(1)

    for i, chain in enumerate(chains):
        validate_chain(chain, i)

    processes = []
    for chain in chains:
        merged = merge_chain_config(global_cfg, chain, script_dir)
        cmd_args, env = build_args_and_env(merged, script_path)

        name = merged.get("name", "?")
        if args.dry_run:
            print(f"[dry-run] {name}: {' '.join(cmd_args)}")
            continue

        print(f"Starting {name} (url={merged.get('url')})...")
        proc = subprocess.Popen(
            ["bash"] + cmd_args,
            env=env,
            cwd=str(script_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append((name, proc))
        print(f"  Started PID {proc.pid}")

    if args.dry_run:
        return

    if not processes:
        return

    print(f"\nRunning {len(processes)} monitor(s). Ctrl+C to stop all.\n")
    try:
        for name, proc in processes:
            proc.wait()
    except KeyboardInterrupt:
        print("\nStopping all monitors...")
        for name, proc in processes:
            proc.terminate()
        for name, proc in processes:
            proc.wait(timeout=5)
        print("Done.")


if __name__ == "__main__":
    main()
