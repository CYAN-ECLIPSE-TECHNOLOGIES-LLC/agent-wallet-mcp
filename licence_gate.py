# Copyright (c) 2026 Cyan Eclipse Technologies LLC
# SPDX-License-Identifier: MIT
"""Cyan Eclipse Agent Wallet - License Server licence gate.

Wraps the Cyan Eclipse License Server client library (ActivationClient,
the Folio L-3 / Pact / ACT / Indigo pattern) and turns its named states
into a gate decision for this product's paid boundary: chain-backed
dispatch (feature below). Everything else in this product is never
licence-gated, in any licence state: the sandbox family and its
dispatch, all reads, wallet creation, policy administration, the audit
log, and every tool's sandbox behaviour.

Boundary (named constant, OWNER-REVISABLE - change the constant and
this docstring together, and record the change in the build record):
    PAID_BOUNDARY_FEATURE = "chain_backed_dispatch"
The gate is consulted only at the EVM adapter's broadcast choke point
(families/evm.py submit) when a gate is attached - by
testnet_config.build_testnet_adapter when the AGENT_WALLET_LICENCE_STATE
env var names a licence state file, or by an explicit gate argument.
It is not a tool and changes no tool shape. Chain-backed mode in this
build is testnet-only (eip155:84532) and counsel-gated beyond that;
the licence gate is a commercial boundary, never a live-money gate.

Semantics (License Server model, SPEC S12):
- activation-once: the first gated call presents the licence artefact
  + install id and persists the returned activation token + artefact
  hash into the licence state file. Check-in presents the token and
  artefact hash only - no usage telemetry, no content.
- heartbeat: a successful check is reused for HEARTBEAT_SECONDS
  (server default 4h) before the next check-in.
- revocation: a definitive server answer (revoked, expired, refused)
  takes effect at that check - the offline grace never overrides a
  server answer, only a server silence.
- offline grace: if the License Server is unreachable, work proceeds
  while the last successful check-in is younger than
  OFFLINE_GRACE_SECONDS (72h) and suspends past it. A licence that
  has never activated has no offline path.
- refusal is a typed LicenceRefusal carrying the named state - a
  receipt-grade error, never a crash, never a traceback.

Gate modes:
- enforced: a licence state file exists (artefact issued by the
  License Server for tenant cet, product agent-wallet, fixture tier
  until a pricing decision supersedes it).
- absent: no state file configured; gated work is refused with
  LICENSE_NOT_CONFIGURED. The gate never invents a licence.
"""
import base64
import hashlib
import json
import os
import sys
import time
import uuid

PRODUCT_ID = "agent-wallet"
PAID_BOUNDARY_FEATURE = "chain_backed_dispatch"
DEFAULT_BASE_URL = "http://127.0.0.1:8799"
PAID_STATES = ("LICENSE_VALID", "LICENSE_GRACE")
HEARTBEAT_SECONDS = 4 * 3600          # server default check-in cadence
OFFLINE_GRACE_SECONDS = 72 * 3600     # server default offline window
UNREACHABLE = "LICENSE_SERVER_UNREACHABLE"


def artefact_hash(artefact):
    head = artefact.split(".", 1)[0]
    body = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
    return hashlib.sha256(body).hexdigest()


def _load_activation_client(base_url):
    candidates = []
    env_dir = os.environ.get("CETLIC_CLIENT_DIR")
    if env_dir:
        candidates.append(env_dir)
    candidates.append("/home/witness/app-family/cyan-eclipse-license-server")
    for cand in candidates:
        if os.path.isfile(os.path.join(cand, "client", "activation.py")):
            if cand not in sys.path:
                sys.path.insert(0, cand)
            try:
                from client.activation import ActivationClient
                return ActivationClient(base_url)
            except Exception:
                continue
    return None


class LicenceRefusal(Exception):
    """Typed gate refusal: str() is '<STATE>: <detail>' - the same
    named-state surface the tools already return for policy errors."""

    def __init__(self, state, detail=""):
        self.state = state
        self.detail = detail
        super().__init__(f"{state}: {detail}" if detail else state)


class LicenceGate:
    def __init__(self, state_path=None, base_url=None, client=None,
                 now=None):
        self.state_path = state_path or os.environ.get(
            "AGENT_WALLET_LICENCE_STATE")
        self.base_url = base_url or os.environ.get(
            "CETLIC_BASE_URL", DEFAULT_BASE_URL)
        self._client = client
        self._now = now or time.time
        self._last_result = None

    @classmethod
    def from_env(cls):
        """A gate armed from the environment, or None when no licence
        state file is named (the pre-product operator posture: the
        boundary exists in code, enforcement arms when a deployment
        configures a licence)."""
        if os.environ.get("AGENT_WALLET_LICENCE_STATE"):
            return cls()
        return None

    @property
    def configured(self):
        return bool(self.state_path and os.path.isfile(self.state_path))

    def _client_or_none(self):
        if self._client is None:
            self._client = _load_activation_client(self.base_url)
        return self._client

    def _read_state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)

    def _save_state(self, state):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self.state_path)

    def _grace_result(self, state):
        """Server unreachable: the 72h offline window, measured from
        the last successful check-in persisted in the state file."""
        last = state.get("last_success_at")
        if last and (self._now() - float(last)) <= OFFLINE_GRACE_SECONDS:
            return {"state": "LICENSE_GRACE", "allowed": True,
                    "detail": "License Server unreachable; inside the "
                              "72h offline window from the last "
                              "successful check-in"}
        return {"state": "LICENSE_SUSPENDED_OFFLINE", "allowed": False,
                "detail": "License Server unreachable and the 72h "
                          "offline window from the last successful "
                          "check-in has closed (or no activation has "
                          "ever succeeded)"}

    def check(self, force=False):
        """Returns {state, allowed, detail}. Performs activation on
        first use, check-in after; reuses a successful check for the
        heartbeat interval unless force=True."""
        if (not force and self._last_result is not None
                and self._last_result["allowed"]
                and self._now() - self._last_result["at"]
                < HEARTBEAT_SECONDS):
            r = dict(self._last_result)
            r["detail"] = "heartbeat window: reusing the last " \
                          "successful check-in"
            return r
        result = self._check_live()
        result["at"] = self._now()
        self._last_result = result if result["allowed"] else None
        return result

    def _check_live(self):
        if not self.configured:
            return {"state": "LICENSE_NOT_CONFIGURED", "allowed": False,
                    "detail": "no licence state file configured for "
                              "chain-backed dispatch; the gate never "
                              "invents a licence"}
        try:
            state = self._read_state()
            artefact = state["artefact"]
        except Exception as e:
            return {"state": "LICENSE_STATE_UNREADABLE",
                    "allowed": False, "detail": f"state file: {e}"}
        client = self._client_or_none()
        if client is None:
            return {"state": "LICENSE_CLIENT_UNAVAILABLE",
                    "allowed": False,
                    "detail": "License Server client library not "
                              "importable"}
        try:
            if not state.get("activation_token"):
                install_id = state.get("install_id") or str(uuid.uuid4())
                r = client.activate(
                    artefact, install_id,
                    state.get("install_label", "cyan-eclipse-agent-wallet"))
                rstate = (r or {}).get("state", "LICENSE_UNKNOWN")
                if rstate == UNREACHABLE:
                    return self._grace_result(state)
                if rstate not in PAID_STATES:
                    return {"state": rstate, "allowed": False,
                            "detail": "activation refused by the "
                                      "License Server"}
                state["install_id"] = install_id
                state["activation_token"] = r.get("activation_token")
                state["license_id"] = r.get("license_id") or state.get(
                    "license_id")
                state["artefact_hash"] = artefact_hash(artefact)
                state["last_success_at"] = self._now()
                self._save_state(state)
                return {"state": rstate, "allowed": True,
                        "detail": "activated with the License Server "
                                  f"for feature {PAID_BOUNDARY_FEATURE}"}
            r = client.checkin(
                state["activation_token"],
                state.get("artefact_hash") or artefact_hash(artefact))
            rstate = (r or {}).get("state", "LICENSE_UNKNOWN")
            if rstate == UNREACHABLE:
                return self._grace_result(state)
            if rstate in PAID_STATES:
                state["last_success_at"] = self._now()
                self._save_state(state)
                return {"state": rstate, "allowed": True,
                        "detail": "check-in with the License Server"}
            return {"state": rstate, "allowed": False,
                    "detail": "check-in refused by the License Server"}
        except Exception as e:
            return {"state": "LICENSE_CHECK_FAILED", "allowed": False,
                    "detail": f"License Server check failed: "
                              f"{type(e).__name__}: {e}"}

    def require(self, feature=PAID_BOUNDARY_FEATURE):
        """Gate a paid-boundary act: returns the check dict when
        allowed, raises LicenceRefusal otherwise."""
        if feature != PAID_BOUNDARY_FEATURE:
            return {"state": "NOT_A_PAID_BOUNDARY", "allowed": True,
                    "detail": f"{feature} is not this product's paid "
                              "boundary and is never licence-gated"}
        result = self.check()
        if not result["allowed"]:
            raise LicenceRefusal(result["state"], result["detail"])
        return result
