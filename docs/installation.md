# Installation

Requirements: Python 3 (standard library only — no pip installs, no build step).

```bash
git clone https://github.com/cetech-ai/agent-wallet-mcp.git
cd agent-wallet-mcp
python3 server.py
```

Register it with an MCP client (Muse, Muse, Cursor, any stdio client)
by pointing the client's MCP configuration at:

```
command: python3
args: ["/path/to/agent-wallet-mcp/server.py"]
```

State persists in `ledger.json` beside the server (see Configuration).
