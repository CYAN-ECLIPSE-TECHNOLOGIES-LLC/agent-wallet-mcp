# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""Regression battery for the Ryan Owens external test report of
2026-11-07 (Agent Wallet sandbox, tested at v0.2.0).

Each test class maps to one numbered finding in that report. The
scenarios are Ryan's own: a payment is queued for human approval, the
world then changes (spend accumulates, the owner tightens or expires
the policy, the kill switch lands), and the approval is resolved. On
v0.2.0 every F1 scenario EXECUTED; the fixed behaviour is refusal.

F1 (CRITICAL, approval skipped policy checks) was cured by the v0.3.x
engine rebuild — dispatch now runs the full policy evaluation against
the CURRENT policy before executing. These tests lock that behaviour
in so the bypass can never silently return.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402

BOB, ALICE = "sandbox:bob", "sandbox:alice"
BASE = {"per_tx_cap_usdc": "50.00", "daily_cap_usdc": "60.00",
        "approval_above_usdc": "20.00", "allowlist": [BOB],
        "purpose": "ryan-findings regression"}


def make_wallet(policy_kw):
    st = server.fresh_state()
    wid = server.t_create_wallet(
        st, {"owner_id": "owner@example.com"})["wallet_id"]
    server.t_set_policy(st, {"wallet_id": wid, **policy_kw})
    server.t_fund_wallet(st, {"wallet_id": wid, "amount_usdc": "500.00"})
    return st, wid


def queue(st, wid, amount, to=BOB):
    r = server.t_pay(st, {"wallet_id": wid, "to": to, "amount_usdc": amount})
    assert r["status"] == "approval_required", r
    return r["request_id"]


def approve(st, rid):
    return server.t_resolve_approval(st, {"request_id": rid}, True)


class TestF1ApprovalRechecksPolicy(unittest.TestCase):
    """F1: approve_payment must evaluate the CURRENT policy, exactly as
    check_payment does, before anything executes."""

    def test_daily_cap_filled_after_queueing(self):
        st, wid = make_wallet(BASE)
        rid = queue(st, wid, "30.00")
        for _ in range(3):  # spend the full 60.00 daily cap directly
            r = server.t_pay(st, {"wallet_id": wid, "to": BOB,
                                  "amount_usdc": "20.00"})
            self.assertEqual(r["status"], "executed")
        r = approve(st, rid)
        self.assertEqual(r["status"], "rejected")

    def test_per_tx_cap_lowered_after_queueing(self):
        st, wid = make_wallet({**BASE, "daily_cap_usdc": "500.00"})
        rid = queue(st, wid, "40.00")
        server.t_set_policy(st, {"wallet_id": wid,
                                 **{**BASE, "daily_cap_usdc": "500.00",
                                    "per_tx_cap_usdc": "30.00"}})
        r = approve(st, rid)
        self.assertEqual(r["status"], "rejected")

    def test_recipient_removed_from_allowlist_after_queueing(self):
        st, wid = make_wallet({**BASE, "daily_cap_usdc": "500.00"})
        rid = queue(st, wid, "40.00")
        server.t_set_policy(st, {"wallet_id": wid,
                                 **{**BASE, "daily_cap_usdc": "500.00",
                                    "allowlist": [ALICE]}})
        r = approve(st, rid)
        self.assertEqual(r["status"], "rejected")

    def test_policy_expired_after_queueing(self):
        st, wid = make_wallet({**BASE, "daily_cap_usdc": "500.00"})
        rid = queue(st, wid, "40.00")
        st["wallets"][wid]["policy"]["expires_at"] = server.now() - 5
        r = approve(st, rid)
        self.assertEqual(r["status"], "rejected")

    def test_approval_still_executes_when_policy_unchanged(self):
        # The re-check must not break the honest path: a queued request
        # that still fits the policy at approval time executes.
        st, wid = make_wallet(BASE)
        rid = queue(st, wid, "30.00")
        r = approve(st, rid)
        self.assertEqual(r["status"], "executed")


def entries_for(st, event, rid):
    return [e for e in st["audit"] if e["event"] == event
            and e["detail"].get("request_id") == rid]


class TestF2RefusedApprovalLogging(unittest.TestCase):
    """F2: a refused approval must be logged as a refusal, never as
    "approval_granted". The audit log is the evidence of what was
    authorized; a grant entry for an approval that did not execute
    corrupts that evidence (Ryan's entry 14, post-revoke)."""

    def test_post_revoke_approval_is_refused_in_log(self):
        # Ryan's exact scenario. With the F3 cure the queued request is
        # cancelled by the revoke itself, so the later approval attempt
        # finds nothing pending — and the log shows the cancellation,
        # never a grant.
        st, wid = make_wallet(BASE)
        rid = queue(st, wid, "30.00")
        server.t_revoke(st, {"wallet_id": wid, "reason": "test"})
        with self.assertRaises(server.ToolError):
            approve(st, rid)
        self.assertEqual(entries_for(st, "approval_granted", rid), [])
        self.assertEqual(len(entries_for(st, "approval_cancelled", rid)), 1)

    def test_policy_refusal_at_approval_is_refused_in_log(self):
        st, wid = make_wallet(BASE)
        rid = queue(st, wid, "30.00")
        for _ in range(3):  # fill the daily cap after queueing
            server.t_pay(st, {"wallet_id": wid, "to": BOB,
                              "amount_usdc": "20.00"})
        r = approve(st, rid)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(entries_for(st, "approval_granted", rid), [])
        self.assertEqual(len(entries_for(st, "approval_refused", rid)), 1)

    def test_executed_approval_logs_one_grant_before_execution(self):
        st, wid = make_wallet(BASE)
        rid = queue(st, wid, "30.00")
        r = approve(st, rid)
        self.assertEqual(r["status"], "executed")
        granted = entries_for(st, "approval_granted", rid)
        self.assertEqual(len(granted), 1)
        executed = [e for e in st["audit"]
                    if e["event"] == "payment_executed"
                    and e["detail"].get("via_approval") == rid]
        self.assertEqual(len(executed), 1)
        self.assertLess(granted[0]["seq"], executed[0]["seq"])


class TestF3RevokeCancelsPending(unittest.TestCase):
    """F3: revoke cancels the wallet's pending approval queue and logs
    each cancellation. Queued requests must not outlive the kill
    switch as silent "pending" entries."""

    def test_revoke_cancels_and_logs_pending(self):
        st, wid = make_wallet({**BASE, "daily_cap_usdc": "500.00"})
        rid1 = queue(st, wid, "30.00")
        rid2 = queue(st, wid, "25.00")
        out = server.t_revoke(st, {"wallet_id": wid, "reason": "test"})
        self.assertEqual(st["pending"][rid1]["status"], "cancelled")
        self.assertEqual(st["pending"][rid2]["status"], "cancelled")
        self.assertEqual(sorted(out["cancelled_request_ids"]),
                         sorted([rid1, rid2]))
        canc = [e for e in st["audit"] if e["event"] == "approval_cancelled"]
        self.assertEqual(len(canc), 2)
        kill = [e for e in st["audit"]
                if e["event"] == "kill_switch_engaged"]
        self.assertEqual(sorted(kill[0]["detail"]["cancelled_request_ids"]),
                         sorted([rid1, rid2]))

    def test_cancelled_request_cannot_be_approved(self):
        st, wid = make_wallet({**BASE, "daily_cap_usdc": "500.00"})
        rid = queue(st, wid, "30.00")
        server.t_revoke(st, {"wallet_id": wid, "reason": "test"})
        with self.assertRaises(server.ToolError):
            approve(st, rid)

    def test_revoke_with_empty_queue_cancels_nothing(self):
        st, wid = make_wallet(BASE)
        out = server.t_revoke(st, {"wallet_id": wid, "reason": "test"})
        self.assertEqual(out["cancelled_request_ids"], [])
        self.assertEqual([e for e in st["audit"]
                          if e["event"] == "approval_cancelled"], [])


class TestF4ExpiryInput(unittest.TestCase):
    """F4: expiry input is validated and the liability receipt states
    the expiry it carries. expires_in_days=0 must not silently mean
    "never expires"; negatives must not mint an already-expired
    policy; no-expiry is expressed by omitting the field and the
    receipt says which of the two the grant carries."""

    def grant(self, **kw):
        st = server.fresh_state()
        wid = server.t_create_wallet(
            st, {"owner_id": "owner@example.com"})["wallet_id"]
        return st, wid, server.t_set_policy(
            st, {"wallet_id": wid, **BASE, **kw})

    def test_zero_days_rejected(self):
        st = server.fresh_state()
        wid = server.t_create_wallet(
            st, {"owner_id": "owner@example.com"})["wallet_id"]
        with self.assertRaises(server.ToolError):
            server.t_set_policy(st, {"wallet_id": wid, **BASE,
                                     "expires_in_days": 0})

    def test_negative_days_rejected(self):
        st = server.fresh_state()
        wid = server.t_create_wallet(
            st, {"owner_id": "owner@example.com"})["wallet_id"]
        with self.assertRaises(server.ToolError):
            server.t_set_policy(st, {"wallet_id": wid, **BASE,
                                     "expires_in_days": -1})

    def test_receipt_states_expiry_date(self):
        _, _, out = self.grant(expires_in_days=7)
        receipt = out["liability_receipt"]
        self.assertIn("expires ", receipt)
        self.assertIn("7 days from the grant", receipt)

    def test_receipt_states_no_expiry(self):
        _, _, out = self.grant()
        self.assertIn("no expiry", out["liability_receipt"])

    def test_one_day_expiry_accepted_and_in_future(self):
        _, wid, out = self.grant(expires_in_days=1)
        self.assertGreater(out["policy"]["expires_at"], server.now())


if __name__ == "__main__":
    unittest.main()
