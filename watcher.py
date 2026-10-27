"""Finality watcher for chain-backed adapters (B1 testnet cell).

Polls adapter.getStatus(tx_ref) and drives a PaymentIntent through
SUBMITTED -> CONFIRMED -> FINAL per the adapter's finalityPolicy:

- reaching CONFIRMED emits the PROVISIONAL receipt (exactly one);
- reaching FINAL emits the single FINAL receipt - and only that
  transition ever emits a FINAL receipt (receipt contract, B1 C-2/D-3);
- a payment already CONFIRMED when the watcher starts keeps its
  existing provisional receipt (no duplicate);
- on timeout the intent routes to UNKNOWN - never silently FINAL,
  never FAILED. UNKNOWN can be re-watched later (legal transitions
  UNKNOWN -> CONFIRMED / FINAL are handled).

The watcher is transport-agnostic: clock and sleep are injectable so
tests run it against fake adapters with zero real waiting.
"""
from __future__ import annotations

import time

from state_machine import State


class FinalityWatcher:
    def __init__(self, adapter, *, timeout_s: int = 180,
                 poll_interval_s: int = 2, clock=None, sleep=None):
        if timeout_s <= 0 or poll_interval_s <= 0:
            raise ValueError("timeout/poll must be positive")
        self.adapter = adapter
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self._clock = clock or (lambda: int(time.time()))
        self._sleep = sleep or time.sleep

    def watch(self, intent, receipt_factory) -> dict:
        """Drive `intent` toward FINAL. receipt_factory(intent,
        finality_state, provisional) -> receipt dict (already appended
        by the caller/dispatcher bookkeeping). Returns
        {status, receipts_emitted, polls}."""
        started = self._clock()
        emitted = []
        polls = 0
        while True:
            polls += 1
            status = self.adapter.getStatus(intent.tx_ref)
            if status == "CONFIRMED":
                if intent.state == State.SUBMITTED:
                    intent.transition(State.CONFIRMED,
                                      "watcher: confirmed (provisional)")
                    emitted.append(receipt_factory(intent, "CONFIRMED", True))
                elif intent.state == State.UNKNOWN:
                    intent.transition(State.CONFIRMED,
                                      "watcher: resolved from UNKNOWN")
                    emitted.append(receipt_factory(intent, "CONFIRMED", True))
            elif status == "FINAL":
                if intent.state == State.SUBMITTED:
                    intent.transition(State.CONFIRMED,
                                      "watcher: confirmed en route to FINAL")
                    emitted.append(receipt_factory(intent, "CONFIRMED", True))
                if intent.state in (State.CONFIRMED, State.UNKNOWN):
                    if intent.state == State.UNKNOWN:
                        # UNKNOWN -> FINAL is legal; no provisional was
                        # emitted for this resolution, FINAL receipt only.
                        pass
                    intent.transition(State.FINAL, "watcher: final")
                    emitted.append(receipt_factory(intent, "FINAL", False))
                return {"status": "FINAL", "receipts_emitted": emitted,
                        "polls": polls}
            elif status == "FAILED_TERMINAL":
                return {"status": "FAILED_TERMINAL",
                        "receipts_emitted": emitted, "polls": polls}
            # SUBMITTED / UNKNOWN from the chain: keep polling until
            # the timeout, then route the intent to UNKNOWN.
            if self._clock() - started >= self.timeout_s:
                if intent.state in (State.SUBMITTED, State.CONFIRMED):
                    intent.transition(State.UNKNOWN,
                                      "watcher: finality timeout - outcome "
                                      "unresolved, never assumed")
                return {"status": "UNKNOWN", "receipts_emitted": emitted,
                        "polls": polls}
            self._sleep(self.poll_interval_s)
