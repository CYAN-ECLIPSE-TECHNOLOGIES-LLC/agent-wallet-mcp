import unittest

from policy import RefusalCode
from state_machine import (Dispatcher, NonceLedger, PaymentIntent, State,
                           TransitionError)
from tests.b0fixtures import *  # noqa: F401,F403


def intent(world, **over):
    kw = dict(wallet_id=WALLET, from_address=ALICE, asset_id=SANDBOX_ASSET,
              chain_id="sandbox:local", to=BOB, amount_base=1_000_000,
              desk_auth_ok=True)
    kw.update(over)
    return PaymentIntent(**kw)


def dispatcher(world):
    registry, adapters, oracle, policy, ledger, clock = world
    return Dispatcher(registry=registry, adapters=adapters, oracle=oracle,
                      policy=policy, clock=clock)


class HappyPathTests(unittest.TestCase):
    def test_full_dispatch_to_final(self):
        world = make_world()
        d = dispatcher(world)
        it = intent(world)
        res = d.dispatch(it)
        self.assertEqual(res.status, "executed")
        self.assertEqual(it.state, State.FINAL)
        self.assertEqual(it.desk_nonce, 0)
        self.assertTrue(d.nonces.is_consumed(WALLET, 0))
        ledger = world[4]
        self.assertEqual(ledger.read(BOB, SANDBOX_ASSET).amount_base, 1_000_000)
        self.assertEqual(ledger.read(ALICE, SANDBOX_ASSET).amount_base,
                         9_000_000)
        states = [h[0] for h in it.history]
        self.assertEqual(states, ["QUEUED", "AUTHORIZED", "BUILT", "SIGNED",
                                  "SUBMITTED", "CONFIRMED", "FINAL"])
        self.assertIsNotNone(res.receipt)
        self.assertEqual(res.receipt["finality"]["state"], "FINAL")
        self.assertFalse(res.receipt["finality"]["provisional"])

    def test_second_payment_uses_next_nonce(self):
        world = make_world()
        d = dispatcher(world)
        self.assertEqual(d.dispatch(intent(world)).status, "executed")
        it2 = intent(world)
        self.assertEqual(d.dispatch(it2).status, "executed")
        self.assertEqual(it2.desk_nonce, 1)


class RefusalSemanticsTests(unittest.TestCase):
    def test_refusal_consumes_no_nonce_and_returns_to_queued(self):
        world = make_world()
        d = dispatcher(world)
        it = intent(world, amount_base=9_000_000)  # over per-tx cap
        res = d.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.OVER_PER_TX_CAP)
        self.assertEqual(it.state, State.QUEUED)
        self.assertFalse(d.nonces.is_consumed(WALLET, 0))
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_desk_auth_failure_consumes_no_nonce(self):
        world = make_world()
        d = dispatcher(world)
        it = intent(world, desk_auth_ok=False)
        # authorize() does not check desk auth; dispatch does (F4 order)
        res = d.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.DESK_AUTH_INVALID)
        self.assertEqual(d.nonces.peek(WALLET), 0)
        self.assertEqual(it.state, State.QUEUED)

    def test_approval_flow(self):
        world = make_world()
        d = dispatcher(world)
        it = intent(world, amount_base=4_500_000)  # above threshold 4.0
        res = d.dispatch(it)
        self.assertEqual(res.status, "approval_required")
        self.assertEqual(it.state, State.QUEUED)
        self.assertEqual(d.nonces.peek(WALLET), 0)
        res = d.grant_approval(it)
        self.assertEqual(res.status, "authorized")
        res = d.dispatch(it)
        self.assertEqual(res.status, "executed")
        self.assertEqual(d.nonces.peek(WALLET), 1)


class DispatchRecheckRaceTests(unittest.TestCase):
    """Policy/freeze/price mutate between AUTHORIZED and dispatch:
    dispatch must refuse, return to QUEUED, and leave the nonce intact."""

    def _authorized(self, world):
        d = dispatcher(world)
        it = intent(world)
        self.assertEqual(d.authorize(it).status, "authorized")
        return d, it

    def test_freeze_between_authorize_and_dispatch(self):
        world = make_world()
        d, it = self._authorized(world)
        world[3].frozen_global = True
        res = d.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.POLICY_FROZEN)
        self.assertEqual(it.state, State.QUEUED)
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_cap_cut_between_authorize_and_dispatch(self):
        world = make_world()
        d, it = self._authorized(world)
        world[3].assets[SANDBOX_ASSET].per_tx_cap_base = 500_000
        res = d.dispatch(it)
        self.assertEqual(res.refusal_code, RefusalCode.OVER_PER_TX_CAP)
        self.assertEqual(it.state, State.QUEUED)
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_depeg_between_authorize_and_dispatch(self):
        world = make_world()
        d, it = self._authorized(world)
        world[2].set(SANDBOX_ASSET, "USD", "0.97", source="fixture")
        res = d.dispatch(it)
        self.assertEqual(res.refusal_code, RefusalCode.DEPEG_BAND_EXCEEDED)
        self.assertEqual(it.state, State.QUEUED)
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_balance_frozen_between_authorize_and_dispatch(self):
        world = make_world()
        d, it = self._authorized(world)
        world[4].set_frozen(ALICE, SANDBOX_ASSET, True)
        res = d.dispatch(it)
        self.assertEqual(res.refusal_code,
                         RefusalCode.BALANCE_ISSUER_FROZEN)
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_adapter_unhealthy_between_authorize_and_dispatch(self):
        world = make_world()
        d, it = self._authorized(world)
        world[1]["sandbox"].set_healthy(False)
        res = d.dispatch(it)
        self.assertEqual(res.refusal_code, RefusalCode.CHAIN_UNHEALTHY)
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_success_consumes_nonce_exactly_once(self):
        world = make_world()
        d, it = self._authorized(world)
        self.assertEqual(d.dispatch(it).status, "executed")
        with self.assertRaises(Exception):
            d.nonces.consume(WALLET, 0)  # replay is refused


class TransitionTests(unittest.TestCase):
    def test_illegal_transitions(self):
        it = intent(make_world())
        for target in (State.BUILT, State.SIGNED, State.FINAL,
                       State.REVERSED, State.SUBMITTED):
            with self.assertRaises(TransitionError, msg=target.value):
                it.transition(target)
        it.transition(State.AUTHORIZED)
        it.transition(State.BUILT)
        with self.assertRaises(TransitionError):
            it.transition(State.FINAL)

    def test_reorg_branch(self):
        it = intent(make_world())
        for s in (State.AUTHORIZED, State.BUILT, State.SIGNED,
                  State.SUBMITTED, State.CONFIRMED, State.FINAL,
                  State.REORGED, State.REVERSED):
            it.transition(s)
        self.assertEqual(it.state, State.REVERSED)
        with self.assertRaises(TransitionError):
            it.transition(State.QUEUED)

    def test_nonce_ledger_monotonic(self):
        n = NonceLedger()
        n.consume("w", 0)
        with self.assertRaises(Exception):
            n.consume("w", 0)
        with self.assertRaises(Exception):
            n.consume("w", 5)  # skip is refused
        n.consume("w", 1)
        self.assertEqual(n.peek("w"), 2)


if __name__ == "__main__":
    unittest.main()
