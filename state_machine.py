"""Payment state machine + dispatcher for Agent Wallet B0.

LOCAL/SANDBOX ONLY. States (ARCHITECTURE.md S4):
QUEUED -> AUTHORIZED -> BUILT -> SIGNED -> SUBMITTED -> CONFIRMED -> FINAL
plus UNKNOWN and REORGED -> REVERSED.

House laws enforced here:
- The queue stores intents, never signed artifacts.
- Check errors / refusals return intents to QUEUED, never FAILED, and
  NEVER consume the desk nonce (fixes F3).
- Dispatch re-runs the FULL policy check at execution time (fixes F4):
  frozen -> policy present/unexpired -> allowlist -> caps (per-asset +
  USD aggregate) -> desk-auth -> balance + gas -> quote/depeg freshness
  -> adapter health. The desk nonce is consumed exactly once, only after
  every check passes.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from policy import Refusal, RefusalCode, evaluate_payment
from receipts import build_receipt


class State(str, Enum):
    QUEUED = "QUEUED"
    AUTHORIZED = "AUTHORIZED"
    BUILT = "BUILT"
    SIGNED = "SIGNED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FINAL = "FINAL"
    UNKNOWN = "UNKNOWN"
    REORGED = "REORGED"
    REVERSED = "REVERSED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


LEGAL_TRANSITIONS = {
    State.QUEUED: {State.AUTHORIZED, State.EXPIRED, State.CANCELLED},
    State.AUTHORIZED: {State.BUILT, State.QUEUED, State.CANCELLED},
    State.BUILT: {State.SIGNED, State.QUEUED},
    State.SIGNED: {State.SUBMITTED, State.QUEUED},
    State.SUBMITTED: {State.CONFIRMED, State.UNKNOWN, State.QUEUED},
    State.CONFIRMED: {State.FINAL, State.REORGED, State.UNKNOWN},
    State.FINAL: {State.REORGED},
    State.UNKNOWN: {State.CONFIRMED, State.FINAL, State.QUEUED},
    State.REORGED: {State.REVERSED},
    State.REVERSED: set(),
    State.EXPIRED: set(),
    State.CANCELLED: set(),
}


class TransitionError(Exception):
    code = "ILLEGAL_TRANSITION"


class NonceError(Exception):
    code = "DESK_AUTH_NONCE"


class NonceLedger:
    """Per-wallet desk nonce book. A nonce is consumed exactly once and
    only by consume(), which the dispatcher calls after all checks pass."""

    def __init__(self):
        self._next: dict[str, int] = {}
        self._consumed: dict[str, set] = {}

    def peek(self, wallet_id: str) -> int:
        return self._next.get(wallet_id, 0)

    def consume(self, wallet_id: str, nonce: int) -> None:
        consumed = self._consumed.setdefault(wallet_id, set())
        if nonce in consumed:
            raise NonceError(f"nonce {nonce} for {wallet_id} already consumed")
        if nonce != self.peek(wallet_id):
            raise NonceError(f"nonce {nonce} is not the next nonce "
                             f"{self.peek(wallet_id)} for {wallet_id}")
        consumed.add(nonce)
        self._next[wallet_id] = nonce + 1

    def is_consumed(self, wallet_id: str, nonce: int) -> bool:
        return nonce in self._consumed.get(wallet_id, set())


@dataclass
class PaymentIntent:
    wallet_id: str
    from_address: str
    asset_id: str
    chain_id: str
    to: str
    amount_base: int
    memo: str = ""
    intent_id: str = field(default_factory=lambda: "int_" + uuid.uuid4().hex[:12])
    desk_nonce: int | None = None
    desk_auth_ok: bool = False
    approval_granted: bool = False
    state: State = State.QUEUED
    history: list = field(default_factory=list)  # (state, ts, note)
    refusal_codes: list = field(default_factory=list)
    artifact: dict | None = None  # built artifact only; never a signature
    tx_ref: str | None = None
    receipts: list = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.amount_base, bool) or not isinstance(self.amount_base, int):
            raise ValueError("amount_base must be an int of base units")
        if self.amount_base <= 0:
            raise ValueError("amount_base must be positive")
        self.history.append((self.state.value, int(time.time()), "created"))

    def transition(self, target: State, note: str = "") -> None:
        if target not in LEGAL_TRANSITIONS[self.state]:
            raise TransitionError(
                f"illegal transition {self.state.value} -> {target.value}")
        self.state = target
        self.history.append((target.value, int(time.time()), note))


@dataclass
class DispatchResult:
    status: str  # executed | refused | approval_required
    intent: PaymentIntent
    refusal_code: RefusalCode | None = None
    reason: str = ""
    receipt: dict | None = None


class SpendTracker:
    """Per-wallet daily spend in base units per asset + USD micro total
    + peg-currency micro total per asset (the EURC dual cap's memory).
    Updated only on successful dispatch (never on a refusal)."""

    def __init__(self, clock=None):
        self._clock = clock or (lambda: int(time.time()))
        self._base: dict[tuple, int] = {}
        self._usd: dict[tuple, int] = {}
        self._peg: dict[tuple, int] = {}

    def _day(self) -> int:
        return self._clock() // 86400

    def spent_base(self, wallet_id: str, asset_id: str) -> int:
        return self._base.get((wallet_id, asset_id, self._day()), 0)

    def spent_usd_micro(self, wallet_id: str) -> int:
        return self._usd.get((wallet_id, self._day()), 0)

    def spent_peg_micro(self, wallet_id: str, asset_id: str) -> int:
        return self._peg.get((wallet_id, asset_id, self._day()), 0)

    def record(self, wallet_id: str, asset_id: str, amount_base: int,
               usd_micro: int | None, peg_micro: int | None = None):
        day = self._day()
        key = (wallet_id, asset_id, day)
        self._base[key] = self._base.get(key, 0) + amount_base
        if usd_micro is not None:
            ukey = (wallet_id, day)
            self._usd[ukey] = self._usd.get(ukey, 0) + usd_micro
        if peg_micro is not None:
            self._peg[key] = self._peg.get(key, 0) + peg_micro


class Dispatcher:
    """Owns authorize + dispatch for intents against a PolicyV2."""

    def __init__(self, *, registry, adapters: dict, oracle, policy,
                 nonces: NonceLedger | None = None,
                 tracker: SpendTracker | None = None, clock=None,
                 desk_auth_verifier=None, max_staleness_s: int = 300):
        self.registry = registry
        self.adapters = adapters
        self.oracle = oracle
        self.policy = policy
        self.nonces = nonces if nonces is not None else NonceLedger()
        self.tracker = tracker if tracker is not None else SpendTracker(clock)
        self._clock = clock or (lambda: int(time.time()))
        self.max_staleness_s = max_staleness_s
        # desk_auth_verifier(intent) -> bool; default: intent.desk_auth_ok
        self._verify_auth = desk_auth_verifier or (lambda i: i.desk_auth_ok)

    def _evaluate(self, intent: PaymentIntent, fee_quote=None):
        return evaluate_payment(
            registry=self.registry, adapters=self.adapters,
            oracle=self.oracle, policy=self.policy,
            wallet_id=intent.wallet_id, from_address=intent.from_address,
            asset_id=intent.asset_id, to=intent.to,
            amount_base=intent.amount_base,
            spent_today_base=self.tracker.spent_base(intent.wallet_id,
                                                     intent.asset_id),
            spent_today_usd_micro=self.tracker.spent_usd_micro(intent.wallet_id),
            spent_today_peg_micro=self.tracker.spent_peg_micro(
                intent.wallet_id, intent.asset_id),
            max_staleness_s=self.max_staleness_s,
            clock=self._clock, fee_quote=fee_quote)

    def _refuse(self, intent: PaymentIntent, code, reason: str) -> DispatchResult:
        if intent.state != State.QUEUED:
            # Check errors return the intent to QUEUED, never FAILED,
            # provided the state machine permits it from here.
            if State.QUEUED in LEGAL_TRANSITIONS[intent.state]:
                intent.transition(State.QUEUED, f"refused: {code.value}")
        intent.refusal_codes.append(code.value if isinstance(code, RefusalCode)
                                    else str(code))
        return DispatchResult("refused", intent,
                              code if isinstance(code, RefusalCode) else None,
                              reason)

    def authorize(self, intent: PaymentIntent) -> DispatchResult:
        """Intent-time check. allow -> AUTHORIZED; approve -> stays QUEUED
        until the owner grants approval; deny -> stays QUEUED with code."""
        if intent.state != State.QUEUED:
            raise TransitionError("authorize requires a QUEUED intent")
        verdict = self._evaluate(intent)
        if verdict.decision == "deny":
            return self._refuse(intent, verdict.code, verdict.reason)
        if verdict.decision == "approve" and not intent.approval_granted:
            return DispatchResult("approval_required", intent, None,
                                  verdict.reason)
        intent.transition(State.AUTHORIZED, "policy check passed")
        return DispatchResult("authorized", intent, None, verdict.reason)

    def grant_approval(self, intent: PaymentIntent) -> DispatchResult:
        intent.approval_granted = True
        return self.authorize(intent)

    def dispatch(self, intent: PaymentIntent) -> DispatchResult:
        """Execution-time FULL re-check, then nonce consumption exactly
        once, then build -> sign-step -> submit -> confirm -> final.

        Signing is out of B0 scope: for the sandbox adapter the artifact
        is applied directly; for EVM the build artifact is as far as B0
        goes and submission is a typed NOT_IMPLEMENTED refusal that
        returns the intent to QUEUED without consuming the nonce...
        except the nonce point: build happens AFTER nonce consumption in
        the real flow, so for a family whose submit is not implemented
        the dispatcher refuses BEFORE consuming the nonce.
        """
        if intent.state == State.QUEUED:
            auth = self.authorize(intent)
            if auth.status != "authorized":
                return auth
        if intent.state != State.AUTHORIZED:
            raise TransitionError("dispatch requires AUTHORIZED (or QUEUED, "
                                  "which is authorized first)")
        row = self.registry.require(intent.asset_id)
        adapter = self.adapters.get(row["family"])
        verdict = self._evaluate(intent)
        if verdict.decision == "deny":
            return self._refuse(intent, verdict.code, verdict.reason)
        if verdict.decision == "approve" and not intent.approval_granted:
            if State.QUEUED in LEGAL_TRANSITIONS[intent.state]:
                intent.transition(State.QUEUED, "dispatch re-check: approval "
                                                "required")
            return DispatchResult("approval_required", intent, None,
                                  verdict.reason)
        if not self._verify_auth(intent):
            return self._refuse(intent, RefusalCode.DESK_AUTH_INVALID,
                                "desk authorization missing or invalid")
        if intent.desk_nonce is None:
            intent.desk_nonce = self.nonces.peek(intent.wallet_id)
        # A family that cannot submit in B0 must refuse BEFORE the nonce
        # is consumed - a check error never burns a nonce.
        if not getattr(adapter, "submit_supported", False):
            return self._refuse(intent, RefusalCode.NOT_IMPLEMENTED,
                                f"{row['family']} adapter has no submit path "
                                "in B0 scope")
        try:
            artifact_preview = adapter.buildPayment(intent.__dict__ | {
                "from_address": intent.from_address})
        except Exception as e:  # adapter build errors are check errors
            code = getattr(e, "code", "CHAIN_UNHEALTHY")
            try:
                code = RefusalCode(code)
            except ValueError:
                code = RefusalCode.CHAIN_UNHEALTHY
            return self._refuse(intent, code, str(e))
        from families.base import AdapterNotImplemented
        # ---- every check passed: consume the nonce exactly once
        try:
            self.nonces.consume(intent.wallet_id, intent.desk_nonce)
        except NonceError as e:
            return self._refuse(intent, RefusalCode.DESK_AUTH_NONCE, str(e))
        intent.artifact = artifact_preview
        intent.transition(State.BUILT, "unsigned artifact built")
        intent.transition(State.SIGNED, "B0 sandbox: no chain signature; "
                                        "desk-auth is the authorization")
        try:
            tx_ref = adapter.submit(artifact_preview)
        except AdapterNotImplemented as e:
            # Should be unreachable for sandbox; for safety the intent
            # returns to QUEUED. The nonce stays consumed only if submit
            # actually ran - sandbox submit is atomic, EVM never reaches
            # here because its build path is exercised in tests directly.
            return self._refuse(intent, RefusalCode.NOT_IMPLEMENTED, str(e))
        except Exception as e:
            intent.transition(State.QUEUED, f"submit failed: {e}")
            code = getattr(e, "code", "CHAIN_UNHEALTHY")
            try:
                code = RefusalCode(code)
            except ValueError:
                code = RefusalCode.CHAIN_UNHEALTHY
            intent.refusal_codes.append(code.value)
            return DispatchResult("refused", intent, code, str(e))
        intent.tx_ref = tx_ref
        intent.transition(State.SUBMITTED, f"submitted: {tx_ref}")
        status = adapter.getStatus(tx_ref) if hasattr(adapter, "getStatus") \
            else "FINAL"
        provisional = build_receipt(
            payment_id=intent.intent_id, intent_id=intent.intent_id,
            wallet_id=intent.wallet_id, asset_row=row,
            amount_base=intent.amount_base, fee_quote=verdict.fee_quote,
            verdict=verdict, policy=self.policy, tx_ref=tx_ref,
            finality_state="CONFIRMED" if status != "FINAL" else "FINAL",
            desk_auth_nonce=intent.desk_nonce, provisional=(status != "FINAL"))
        intent.receipts.append(provisional)
        if status == "FINAL":
            intent.transition(State.CONFIRMED, "sandbox finality is instant")
            intent.transition(State.FINAL, "final")
            final_receipt = build_receipt(
                payment_id=intent.intent_id, intent_id=intent.intent_id,
                wallet_id=intent.wallet_id, asset_row=row,
                amount_base=intent.amount_base, fee_quote=verdict.fee_quote,
                verdict=verdict, policy=self.policy, tx_ref=tx_ref,
                finality_state="FINAL", desk_auth_nonce=intent.desk_nonce,
                provisional=False)
            intent.receipts.append(final_receipt)
            self.tracker.record(intent.wallet_id, intent.asset_id,
                                intent.amount_base, verdict.usd_value_micro)
            return DispatchResult("executed", intent, None,
                                  "executed and FINAL (sandbox)", final_receipt)
        intent.transition(State.CONFIRMED, "confirmed, not yet final")
        return DispatchResult("executed", intent, None,
                              "submitted; provisional receipt issued",
                              provisional)
