# chain-tools

Small CLI utilities for inspecting and debugging EVM nodes and chains.

## Block history

Probe how far back an RPC endpoint retains blocks, tx index, archival state, logs, and receipts.

```bash
./check-block-history.py 8545
./check-block-history.py http://localhost:8545
```

## Basic checks

Run quick health checks on a local or remote node (sync status, peers, blocks, and more).

```bash
./basic_geth_checks.sh 8545 general_check
./basic_geth_checks.sh 127.0.0.1:8545 monitor
```

## eth-cli

Query balances, transactions, mempool status, and blocks across common EVM chains.

```bash
./eth-cli.py balance 0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb
./eth-cli.py -u 8545 tx 0xabc...
```
