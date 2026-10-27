"""C-3 evidence: (D-3) exactly one receipt per state transition, and
(D-4) PaymentIntent TTL - an intent past its TTL is refused
APPROVAL_EXPIRED at authorize and at dispatch, moved to EXPIRED, and
never consumes the desk nonce."""
import unittest

from policy import RefusalCode
from state_machine import Dispatcher, PaymentIntent, State
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


class SingleReceiptTests(unittest.TestCase):
    def test_dispatch_emits_exactly_one_receipt(self):
        world = make_world()
        d = dispatcher(world)
        it = intent(world)
        res = d.dispatch(it)
        self.assertEqual(res.status, "executed")
        self.assertEqual(len(it.receipts), 1)  # D-3: was a duplicate pair
        self.assertIs(res.receipt, it.receipts[0])
        self.assertEqual(res.receipt["finality"]["state"], "FINAL")
        self.assertFalse(res.receipt["finality"]["provisional"])

    def test_two_payments_two_receipts(self):
        world = make_world()
        d = dispatcher(world)
        r1 = d.dispatch(intent(world)).receipt
        r2 = d.dispatch(intent(world)).receipt
        self.assertNotEqual(r1["receipt_hash"], r2["receipt_hash"])
        finals = [r for r in (r1, r2)
                  if r["finality"]["state"] == "FINAL"]
        self.assertEqual(len(finals), 2)  # one per payment, not 2x


class IntentTtlTests(unittest.TestCase):
    def test_ttl_shape_validation(self):
        for bad in (0, -5, 1.5, True, "60"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                intent(make_world(), ttl_s=bad)
        it = intent(make_world(), ttl_s=60)
        self.assertEqual(it.expires_at, it.created_at + 60)
        self.assertIsNone(intent(make_world()).expires_at)

    def test_expired_intent_refused_at_authorize(self):
        world = make_world()
        clock = world[5]
        d = dispatcher(world)
        it = intent(world, ttl_s=60, created_at=clock())
        clock.advance(61)
        res = d.authorize(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.APPROVAL_EXPIRED)
        self.assertEqual(it.state, State.EXPIRED)
        self.assertEqual(d.nonces.peek(WALLET), 0)

    def test_expiry_between_authorize_and_dispatch(self):
        world = make_world()
        clock = world[5]
        d = dispatcher(world)
        it = intent(world, ttl_s=60, created_at=clock())
        self.assertEqual(d.authorize(it).status, "authorized")
        clock.advance(61)
        res = d.dispatch(it)
        self.assertEqual(res.status, "refused")
        self.assertEqual(res.refusal_code, RefusalCode.APPROVAL_EXPIRED)
        self.assertEqual(it.state, State.EXPIRED)
        self.assertEqual(d.nonces.peek(WALLET), 0)  # nonce never burned
        ledger = world[4]
        self.assertEqual(ledger.read(BOB, SANDBOX_ASSET).amount_base, 0)

    def test_unexpired_intent_dispatches(self):
        world = make_world()
        clock = world[5]
        d = dispatcher(world)
        it = intent(world, ttl_s=3600, created_at=clock())
        clock.advance(60)
        self.assertEqual(d.dispatch(it).status, "executed")

    def test_no_ttl_means_no_expiry(self):
        world = make_world()
        clock = world[5]
        d = dispatcher(world)
        it = intent(world, created_at=clock())
        clock.advance(10_000_000)
        # The price is long stale by now, but with no TTL set the
        # refusal can never be APPROVAL_EXPIRED.
        res = d.authorize(it)
        self.assertNotEqual(res.refusal_code, RefusalCode.APPROVAL_EXPIRED)
        self.assertNotEqual(it.state, State.EXPIRED)


if __name__ == "__main__":
    unittest.main()
