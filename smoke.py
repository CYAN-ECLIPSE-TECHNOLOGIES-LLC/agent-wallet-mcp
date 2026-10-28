#!/usr/bin/env python3
# Eclipse Agent Wallet MCP
# Copyright © 2026 Cyan Eclipse Technologies LLC · cetech.ai
# Smoke: drive server.py over real stdio — initialize + tools/list only.

"""Smoke: initialize handshake + tools/list against server.py over
real stdio. No tool calls, no state mutation beyond boot."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def main():
    proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True)
    try:
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "smoke",
                                        "version": "0"}}}) + "\n")
        proc.stdin.flush()
        init = json.loads(proc.stdout.readline())
        print("server:", init["result"]["serverInfo"])
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
             "params": {}}) + "\n")
        proc.stdin.flush()
        tools = json.loads(proc.stdout.readline())["result"]["tools"]
        print("tools:", len(tools), [t["name"] for t in tools])
        assert len(tools) == 10, len(tools)
        print("SMOKE OK")
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


if __name__ == "__main__":
    main()
