# Configuration

No configuration file. One environment variable (names only):

| Variable | Meaning |
|---|---|
| `AGENT_WALLET_LEDGER` | Path to the JSON ledger file. Default: `ledger.json` beside `server.py`. |

The ledger holds wallet state and the append-only audit log. It is runtime
state, excluded from the repository via `.gitignore`.
