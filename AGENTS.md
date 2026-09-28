# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Copilot, etc.) working in this repository.

## This is a PUBLIC repository

Everything committed here — code, comments, docs, commit messages, PR text, test fixtures,
example output — is visible to anyone. Treat every change as if it will be published.

## Never commit or write into tracked files

- **Secrets:** API keys, RPC provider keys/tokens (Infura, Alchemy, QuickNode, Ankr, etc.),
  JWT secrets (`jwt.hex`), private keys, mnemonics, keystore files, passwords, auth headers,
  webhook URLs (Slack/Discord/Telegram bot tokens), `.env` contents.
- **Infrastructure details:** real hostnames, internal/public IP addresses, domain names,
  ports specific to our deployment, VPN/bastion info, SSH users, cloud account/project IDs,
  bucket names, datacenter or provider names tied to our setup.
- **Node identity:** enode URLs, peer IDs, node keys, validator/operator addresses or
  pubkeys that identify our infrastructure.
- **Operational data:** real log excerpts, monitoring output, alert contents, customer or
  partner names, internal ticket links, internal chat/wiki links.
- **Local paths:** absolute paths that reveal usernames or machine layout
  (e.g. `/home/<user>/...`, `/Users/<user>/...`, mount points of real data disks).
- **Personal info:** personal emails, names, phone numbers. Do not add author emails to
  files; git metadata is enough.

## Use placeholders instead

| Instead of              | Use                                                     |
|-------------------------|---------------------------------------------------------|
| Real RPC endpoint       | `http://localhost:8545`, `https://rpc.example.com`      |
| Real IP / host          | `127.0.0.1`, `192.0.2.10` (RFC 5737), `node.example.com`|
| API key in URL          | `https://eth-mainnet.example.com/v2/$RPC_API_KEY`       |
| Real address / tx hash  | Well-known public addresses, or `0x000...`, `0xabc...`  |
| Real data paths         | `/data/...`, `/var/lib/node/...`                        |
| Real enode              | `enode://<pubkey>@<ip>:30303`                           |

## Configuration and secrets handling

- Read secrets from environment variables or from gitignored config files; never hardcode them,
  not even as defaults.
- Local config lives in gitignored files (e.g. `eth-monitor/config.yaml`). If a tool needs a
  config, commit a sanitized `*.example.yaml` / `.env.example` with placeholder values only.
- When adding a new config/secret file, add it to `.gitignore` in the same change.
- Do not print secrets in logs or error messages; mask them (e.g. show only the host of an
  RPC URL, never the path/query that may carry a key).

## Before committing

1. Review the full diff (`git diff --staged`) specifically for anything in the lists above.
2. Scan for common leaks, e.g.:
   ```bash
   git diff --staged | grep -nEi 'api[_-]?key|secret|token|password|passwd|private[_-]?key|mnemonic|bearer|authorization|enode://|jwt|([0-9]{1,3}\.){3}[0-9]{1,3}|/home/|/Users/'
   ```
   Any hit must be a placeholder or removed.
3. Never stage untracked files blindly (`git add -A` / `git add .`); add files by name.
4. Do not commit generated artifacts: logs (`log/`), `*.tmp`, `__pycache__`, dumps,
   database/freezer files, or output captured from real nodes.
5. Commit messages and PR descriptions must also be free of the items above.

If something sensitive was already committed, do **not** just delete it in a new commit —
stop and tell the maintainer so the secret can be rotated and history cleaned.

## Working conventions

- Small, self-contained CLI utilities for inspecting/debugging EVM nodes (Python and Bash).
- Keep tools read-only toward nodes by default; anything destructive must be opt-in
  (dry-run by default, explicit `--apply`/`--force`), like `promote-to-symlink.sh`.
- Accept endpoints as arguments (port, `host:port`, or full URL) rather than embedding them.
- Document new tools in `README.md` with placeholder-only examples.
- Do not add telemetry, remote calls, or dependencies that phone home.
