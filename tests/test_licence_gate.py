# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""Licence gate battery (hermetic): the gate module's named states
under a fake License Server client + fake clock, and the EVM adapter
wiring — a gate refusal stops a chain-backed submit before any RPC
call, an allowing gate lets it through, and the sandbox family has
no gate at all. The live fixture-tenant proof against the deployed
License Server is a separate program artifact, not this battery."""
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from licence_gate import (LicenceGate, LicenceRefusal,
                          PAID_BOUNDARY_FEATURE)
from families.evm import keccak256
from families.evm import JsonRpcClient
from testnet_config import build_testnet_adapter, load_testnet_registry
from tests.fake_rpc import FakeRpc


def _artefact():
    body = json.dumps({"license_id": "lic-fixture"}).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + ".sig"


class FakeClient:
    def __init__(self, activate_state="LICENSE_VALID",
                 checkin_state="LICENSE_VALID"):
        self.activate_state = activate_state
        self.checkin_state = checkin_state
        self.activations = 0
        self.checkins = 0

    def activate(self, artefact, install_id, label):
        self.activations += 1
        if self.activate_state == "LICENSE_VALID":
            return {"state": "LICENSE_VALID",
                    "activation_token": "tok-1", "license_id": "lic-1"}
        return {"state": self.activate_state}

    def checkin(self, token, artefact_hash):
        self.checkins += 1
        return {"state": self.checkin_state}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class GateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.tmp.name) / "licence.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _gate(self, client, clock=None, with_artefact=True):
        if with_artefact:
            Path(self.state_path).write_text(json.dumps(
                {"artefact": _artefact(), "install_id": "inst-1"}))
        return LicenceGate(state_path=self.state_path, client=client,
                           now=clock or Clock())

    def test_absent_config_is_not_configured(self):
        gate = LicenceGate(state_path=None, client=FakeClient())
        r = gate.check()
        self.assertEqual(r["state"], "LICENSE_NOT_CONFIGURED")
        self.assertFalse(r["allowed"])
        with self.assertRaises(LicenceRefusal):
            gate.require()

    def test_activation_then_checkin(self):
        client = FakeClient()
        gate = self._gate(client)
        r = gate.check()
        self.assertTrue(r["allowed"])
        self.assertEqual(r["state"], "LICENSE_VALID")
        self.assertEqual(client.activations, 1)
        persisted = json.loads(Path(self.state_path).read_text())
        self.assertEqual(persisted["activation_token"], "tok-1")
        self.assertTrue(persisted["artefact_hash"])
        # heartbeat reuse: no second server call inside the window
        r2 = gate.check()
        self.assertTrue(r2["allowed"])
        self.assertEqual(client.activations + client.checkins, 1)
        # forced re-check goes to check-in, not re-activation
        r3 = gate.check(force=True)
        self.assertTrue(r3["allowed"])
        self.assertEqual(client.checkins, 1)

    def test_revoked_checkin_refuses(self):
        client = FakeClient(checkin_state="LICENSE_REVOKED")
        gate = self._gate(client)
        Path(self.state_path).write_text(json.dumps({
            "artefact": _artefact(), "install_id": "inst-1",
            "activation_token": "tok-1", "artefact_hash": "h",
            "last_success_at": Clock()()}))
        r = gate.check()
        self.assertFalse(r["allowed"])
        self.assertEqual(r["state"], "LICENSE_REVOKED")

    def test_unreachable_inside_grace_allows(self):
        clock = Clock()
        client = FakeClient(checkin_state="LICENSE_SERVER_UNREACHABLE")
        gate = self._gate(client, clock)
        Path(self.state_path).write_text(json.dumps({
            "artefact": _artefact(), "install_id": "inst-1",
            "activation_token": "tok-1", "artefact_hash": "h",
            "last_success_at": clock() - 3600}))
        r = gate.check()
        self.assertTrue(r["allowed"])
        self.assertEqual(r["state"], "LICENSE_GRACE")

    def test_unreachable_past_grace_suspends(self):
        clock = Clock()
        client = FakeClient(checkin_state="LICENSE_SERVER_UNREACHABLE")
        gate = self._gate(client, clock)
        Path(self.state_path).write_text(json.dumps({
            "artefact": _artefact(), "install_id": "inst-1",
            "activation_token": "tok-1", "artefact_hash": "h",
            "last_success_at": clock() - 73 * 3600}))
        r = gate.check()
        self.assertFalse(r["allowed"])
        self.assertEqual(r["state"], "LICENSE_SUSPENDED_OFFLINE")

    def test_never_activated_unreachable_has_no_offline_path(self):
        client = FakeClient(activate_state="LICENSE_SERVER_UNREACHABLE")
        gate = self._gate(client)
        r = gate.check()
        self.assertFalse(r["allowed"])
        self.assertEqual(r["state"], "LICENSE_SUSPENDED_OFFLINE")

    def test_non_boundary_feature_never_gated(self):
        gate = LicenceGate(state_path=None, client=FakeClient())
        r = gate.require("sandbox_dispatch")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["state"], "NOT_A_PAID_BOUNDARY")
        self.assertEqual(PAID_BOUNDARY_FEATURE, "chain_backed_dispatch")


class AdapterWiringTests(unittest.TestCase):
    def setUp(self):
        self.reg = load_testnet_registry()
        self.rpc = FakeRpc()
        tx_hash = "0x" + keccak256(bytes.fromhex("deadbeef")).hex()
        self.tx_hash = tx_hash
        self.artifact = {"signed": True, "raw_tx": "0xdeadbeef",
                         "tx_hash": tx_hash, "chain_id": "eip155:84532"}

    def _adapter(self, gate):
        return build_testnet_adapter(
            self.reg,
            rpc_client=JsonRpcClient("http://fake", transport=self.rpc),
            licence_gate=gate)

    def test_refusing_gate_blocks_submit_before_any_rpc(self):
        gate = LicenceGate(state_path=None, client=FakeClient())
        adapter = self._adapter(gate)
        with self.assertRaises(LicenceRefusal):
            adapter.submit(self.artifact)
        self.assertEqual(self.rpc.sent_raw, [])
        self.assertNotIn(self.tx_hash, self.rpc.txs)

    def test_allowing_gate_lets_submit_through(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state_path = str(Path(tmp.name) / "licence.json")
        Path(state_path).write_text(json.dumps(
            {"artefact": _artefact(), "install_id": "inst-1"}))
        gate = LicenceGate(state_path=state_path, client=FakeClient())
        adapter = self._adapter(gate)
        self.assertEqual(adapter.submit(self.artifact), self.tx_hash)

    def test_no_gate_is_pre_product_posture(self):
        adapter = build_testnet_adapter(
            self.reg,
            rpc_client=JsonRpcClient("http://fake", transport=self.rpc),
            licence_gate=None)
        self.assertIsNone(adapter.licence_gate)
        self.assertEqual(adapter.submit(self.artifact), self.tx_hash)

    def test_env_without_state_var_attaches_no_gate(self):
        env = dict(os.environ)
        env.pop("AGENT_WALLET_LICENCE_STATE", None)
        old = os.environ.get("AGENT_WALLET_LICENCE_STATE")
        os.environ.pop("AGENT_WALLET_LICENCE_STATE", None)
        try:
            adapter = build_testnet_adapter(
                self.reg,
                rpc_client=JsonRpcClient("http://fake",
                                         transport=self.rpc))
        finally:
            if old is not None:
                os.environ["AGENT_WALLET_LICENCE_STATE"] = old
        self.assertIsNone(adapter.licence_gate)


if __name__ == "__main__":
    unittest.main()
