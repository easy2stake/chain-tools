# chain-tools

Small CLI utilities for inspecting and debugging EVM nodes and chains.

## Block history

Probe how far back an RPC endpoint retains blocks, tx index, archival state, logs, and receipts.

```bash
./check-block-history.py 8545
./check-block-history.py http://localhost:8545
```

## Basic checks

Run quick health checks on a local or remote node: EVM (chain ID, enode, peers, sync status, latest/safe/finalized/earliest blocks), OP node, Tendermint/CometBFT, Aptos and beacon. Each check has a `*_monitor` variant that refreshes every `--interval` seconds. All requests reuse one keep-alive connection, so `ReqTime` excludes TCP/TLS handshakes. Block, transaction and balance lookups are in `eth-cli`.

JSON-RPC checks (`general_check`, `monitor`, `op*`) also run over one WebSocket: pass a `ws://` / `wss://` URL, or `--ws` to turn a port, `host:port` or `http(s)://` URL into `ws(s)://`. `heads` (WebSocket only) subscribes to `newHeads` and prints each head as it arrives: delay after the block timestamp, interval, gaps and reorgs, and a summary on Ctrl+C. WebSocket mode needs `websocket-client` (`sudo apt install python3-websocket`, or `pip install websocket-client`).

```bash
./basic-checks.py 8545
./basic-checks.py 127.0.0.1:8545 monitor
./basic-checks.py https://rpc.example.com general_check
./basic-checks.py 9545 op
./basic-checks.py 26657 tendermint_monitor
./basic-checks.py http://127.0.0.1:8080/v1 aptos
./basic-checks.py 3500 beacon
./basic-checks.py ws://127.0.0.1:8546 monitor
./basic-checks.py --ws 8546 heads
./basic-checks.py wss://rpc.example.com heads
```

## eth-cli

Query balances, transactions, mempool status, and blocks across common EVM chains.

```bash
./eth-cli.py balance 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb
./eth-cli.py -u 8545 tx 0xabc...
```

## Block fetch benchmark

Benchmark an RPC endpoint with `eth_getBlockByNumber` on random blocks. Reports req/s, latency percentiles, and errors.

With `--logs SPAN` it benchmarks `eth_getLogs` instead, over random `SPAN`-block windows inside the selected range, and also reports logs per request. Unfiltered by default; `--address` (repeatable, matched as OR) limits it to logs from those contracts.

```bash
./bench-get-block.py 8545
./bench-get-block.py -n 5000 -c 32 --full http://localhost:8545
./bench-get-block.py -d 60 --recent 100000 localhost:8545
./bench-get-block.py --logs 100 --recent 100000 -c 8 localhost:8545
./bench-get-block.py --logs 1000 --from 1000000 --to 2000000 -n 200 8545
./bench-get-block.py --logs 1000 --address 0xdAC17F958D2ee523a2206206994597C13D831ec7 8545
```

## Promote freezer files to symlinks

Replace local ancient/freezer files with symlinks to copies in another directory. Dry-run by default; pass `--apply` to act. Stop the node before `--apply --force`.

```bash
./promote-to-symlink.sh --src /data/archive/chain \
  --dst /var/lib/node/ancient/chain --force 'bodies.000*.cdat'

./promote-to-symlink.sh --src /data/archive/chain \
  --dst /var/lib/node/ancient/chain --apply --force \
  --backup-dir /data/ancient-displaced 'bodies.000*.cdat'
```
