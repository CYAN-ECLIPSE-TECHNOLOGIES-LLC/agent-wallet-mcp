"""Existing-surface smoke: the untouched v1 server.py (10 tools) must
still handshakes + run its sandbox flow against a TEMP ledger. This is
the 'existing suite green' check for B0: HEAD ships no test files, its CI
is compileall + JSON validation + this handshake."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rpc(proc_in, rid, method, params=None):
    req = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params:
        req["params"] = params
    proc_in.write(json.dumps(req) + "\n")
    proc_in.flush()


class ServerSmokeTests(unittest.TestCase):
    def test_handshake_and_sandbox_flow(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, AGENT_WALLET_LEDGER=str(Path(td) / "l.json"))
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "server.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, env=env)
            try:
                rpc(proc.stdin, 1, "initialize",
                    {"protocolVersion": "2025-03-26"})
                resp = json.loads(proc.stdout.readline())
                self.assertIn("result", resp)
                rpc(proc.stdin, 2, "tools/list")
                resp = json.loads(proc.stdout.readline())
                tools = [t["name"] for t in resp["result"]["tools"]]
                self.assertEqual(len(tools), 10)  # surface unchanged

                def call(rid, name, args):
                    rpc(proc.stdin, rid, "tools/call",
                        {"name": name, "arguments": args})
                    r = json.loads(proc.stdout.readline())
                    return json.loads(r["result"]["content"][0]["text"])

                w = call(3, "create_wallet", {"owner_id": "owner"})
                wid = w["wallet_id"]
                call(4, "fund_wallet", {"wallet_id": wid,
                                        "amount_usdc": "100"})
                call(5, "set_policy", {"wallet_id": wid,
                                       "per_tx_cap_usdc": "50",
                                       "daily_cap_usdc": "80"})
                pay = call(6, "pay", {"wallet_id": wid,
                                      "to": "0x" + "cd" * 20,
                                      "amount_usdc": "10"})
                self.assertEqual(pay["status"], "executed")
            finally:
                proc.stdin.close()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
