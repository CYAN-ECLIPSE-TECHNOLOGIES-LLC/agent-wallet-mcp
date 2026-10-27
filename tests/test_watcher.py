import unittest

from receipts import build_receipt
from state_machine import PaymentIntent, State
from watcher import FinalityWatcher
from tests.b0fixtures import *  # noqa: F401,F403


class ScriptedAdapter:
    def __init__(self, statuses):
        self.statuses = list(statuses)

    def getStatus(self, tx_ref):
        if len(self.statuses) > 1:
            return self.statuses.pop(0)
        return self.statuses[0]


def make_intent(state_path=("AUTHORIZED", "BUILT", "SIGNED", "SUBMITTED")):
    it = PaymentIntent(wallet_id=WALLET, from_address=ALICE,
                       asset_id=SANDBOX_ASSET, chain_id="sandbox:local",
                       to=BOB, amount_base=100, desk_nonce=0)
    for name in state_path:
        it.transition(State[name], "test")
    it.tx_ref = "tx-1"
    return it


def factory(it, finality_state, provisional):
    r = {"finality": {"state": finality_state, "provisional": provisional}}
    it.receipts.append(r)
    return r


class WatcherTests(unittest.TestCase):
    def test_submitted_confirmed_final_receipts(self):
        clock = Clock(1000)
        it = make_intent()
        w = FinalityWatcher(ScriptedAdapter(
            ["SUBMITTED", "CONFIRMED", "FINAL"]), timeout_s=100,
            poll_interval_s=1, clock=clock, sleep=lambda s: None)
        out = w.watch(it, factory)
        self.assertEqual(out["status"], "FINAL")
        self.assertEqual(it.state, State.FINAL)
        finals = [r for r in it.receipts
                  if r["finality"]["state"] == "FINAL"]
        prov = [r for r in it.receipts if r["finality"]["provisional"]]
        self.assertEqual(len(finals), 1)
        self.assertEqual(len(prov), 1)

    def test_final_direct_from_submitted(self):
        it = make_intent()
        w = FinalityWatcher(ScriptedAdapter(["FINAL"]), timeout_s=100,
                            poll_interval_s=1, clock=Clock(0),
                            sleep=lambda s: None)
        out = w.watch(it, factory)
        self.assertEqual(out["status"], "FINAL")
        finals = [r for r in it.receipts
                  if r["finality"]["state"] == "FINAL"]
        self.assertEqual(len(finals), 1)

    def test_timeout_routes_unknown_never_final(self):
        clock = Clock(0)

        def sleep(_s):
            clock.advance(10)
        it = make_intent()
        w = FinalityWatcher(ScriptedAdapter(["SUBMITTED"]), timeout_s=25,
                            poll_interval_s=10, clock=clock, sleep=sleep)
        out = w.watch(it, factory)
        self.assertEqual(out["status"], "UNKNOWN")
        self.assertEqual(it.state, State.UNKNOWN)
        self.assertEqual(it.receipts, [])  # no provisional, no FINAL

    def test_unknown_resolves_to_final(self):
        it = make_intent()
        it.transition(State.UNKNOWN, "test")
        w = FinalityWatcher(ScriptedAdapter(["FINAL"]), timeout_s=100,
                            poll_interval_s=1, clock=Clock(0),
                            sleep=lambda s: None)
        out = w.watch(it, factory)
        self.assertEqual(out["status"], "FINAL")
        self.assertEqual(it.state, State.FINAL)
        self.assertEqual(len([r for r in it.receipts
                              if r["finality"]["state"] == "FINAL"]), 1)


if __name__ == "__main__":
    unittest.main()
