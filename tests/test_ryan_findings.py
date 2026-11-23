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


if __name__ == "__main__":
    unittest.main()
