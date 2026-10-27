"""Herald mandate verification bridge (F-1 closure, B1 testnet bench).

The bench found that no mandate-status check existed anywhere in this
cell's dispatch path: the Dispatcher and FundSigner accepted no mandate
input, so a revoked Herald mandate could neither be presented nor
refused, and receipts reserved mandate_id as permanently null.

This module is the bridge. It does NOT re-implement any crypto: it
imports the proven verifier from eclipse-herald-mcp
(`mandates.verify_mandate_for_payment` — signature + expiry + status-
list revocation + scope enforcement) and the proven status-list store
(`status_list.StatusList`, whose load validates the whole store and
fails closed). A FRESH status list is loaded on every verification so
a revocation lands at the next dispatch, never at cache expiry.

Herald refusal codes map onto wallet RefusalCodes:
  MANDATE_REVOKED              -> MANDATE_REVOKED
  MANDATE_EXPIRED              -> MANDATE_EXPIRED
  MANDATE_SIGNATURE_INVALID    -> MANDATE_INVALID
  MANDATE_MALFORMED            -> MANDATE_INVALID
  MANDATE_STATUS_UNAVAILABLE   -> MANDATE_UNKNOWN when the mandate's
                                  status index was never allocated,
                                  else MANDATE_STATUS_UNAVAILABLE
  MANDATE_STATUS_LIST_MISMATCH -> MANDATE_STATUS_UNAVAILABLE
  MANDATE_SCOPE_* / _CAP /
  MANDATE_CONTEXT_INCOMPLETE   -> MANDATE_SCOPE

Fail-closed throughout: an unloadable status list, a mismatched list,
or a verifier exception refuses the payment pre-sign; nothing here
ever waves a mandate through.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from state_machine import MandateCheckFailed

_HERALD_MODULES = None


def default_herald_dir() -> Path:
    """Where the Herald ASSETS live (keys/jwks.json + state/)."""
    env = os.environ.get("ECLIPSE_HERALD_DIR")
    if env:
        return Path(env)
    return Path.home() / "workspace" / "cyan-eclipse" / "eclipse-herald-mcp"


def _code_dir() -> Path:
    """Where the Herald CODE lives (mandates.py / status_list.py).
    Kept distinct from the assets dir: a deployment (or a test
    fixture) may hold JWKS + status list elsewhere, but the verifier
    implementation is the one proven path in the Herald repo."""
    env = os.environ.get("ECLIPSE_HERALD_CODE_DIR")
    if env:
        return Path(env)
    return Path.home() / "workspace" / "cyan-eclipse" / "eclipse-herald-mcp"


def _herald_modules():
    """Import Herald's mandates + status_list from its repo, once.
    The Herald-only module names (mandates, status_list, identity,
    jwscore) resolve from the code dir, which is APPENDED to
    sys.path: this package's own modules (server, receipts, ...)
    keep precedence for any name both repos carry."""
    global _HERALD_MODULES
    if _HERALD_MODULES is not None:
        return _HERALD_MODULES
    d = str(_code_dir())
    if d not in sys.path:
        sys.path.append(d)
    import mandates as herald_mandates
    import status_list as herald_status_list
    _HERALD_MODULES = (herald_mandates, herald_status_list)
    return _HERALD_MODULES


def _map_refusal(refusal) -> MandateCheckFailed:
    code = getattr(refusal, "code", "") or ""
    reason = getattr(refusal, "reason", None) or str(refusal)
    if code == "MANDATE_REVOKED":
        mapped = "MANDATE_REVOKED"
    elif code == "MANDATE_EXPIRED":
        mapped = "MANDATE_EXPIRED"
    elif code in ("MANDATE_SIGNATURE_INVALID", "MANDATE_MALFORMED"):
        mapped = "MANDATE_INVALID"
    elif code == "MANDATE_STATUS_LIST_MISMATCH":
        mapped = "MANDATE_STATUS_UNAVAILABLE"
    elif code == "MANDATE_STATUS_UNAVAILABLE":
        mapped = ("MANDATE_UNKNOWN" if "not allocated" in reason
                  else "MANDATE_STATUS_UNAVAILABLE")
    elif code.startswith("MANDATE_SCOPE") or \
            code == "MANDATE_CONTEXT_INCOMPLETE":
        mapped = "MANDATE_SCOPE"
    else:
        mapped = "MANDATE_INVALID"
    return MandateCheckFailed(mapped, f"{code}: {reason}")


class HeraldMandateVerifier:
    """Dispatcher mandate_verifier: callable(intent, payment_context)
    -> jti (str). Raises MandateCheckFailed on any refusal."""

    def __init__(self, *, jwks: dict, status_list_path):
        if not isinstance(jwks, dict) or not jwks.get("keys"):
            raise ValueError("jwks must be a JWKS dict with keys")
        self.jwks = jwks
        self.status_list_path = Path(status_list_path)

    def verify(self, credential, payment_context: dict,
               now_ts: int | None = None) -> str:
        mandates, status_list_mod = _herald_modules()
        try:
            status_list = status_list_mod.StatusList.load(
                self.status_list_path)
        except Exception as e:
            raise MandateCheckFailed(
                "MANDATE_STATUS_UNAVAILABLE",
                f"mandate status list cannot be loaded "
                f"({self.status_list_path}): {e}")
        try:
            out = mandates.verify_mandate_for_payment(
                credential, self.jwks, status_list, payment_context,
                now_ts)
        except mandates.MandateRefusal as e:
            raise _map_refusal(e)
        except Exception as e:
            raise MandateCheckFailed(
                "MANDATE_INVALID", f"mandate verification error: {e}")
        return out["mandate_jti"]

    def __call__(self, intent, payment_context: dict) -> str:
        return self.verify(intent.mandate_credential, payment_context)


def production_verifier(herald_dir: Path | None = None
                        ) -> HeraldMandateVerifier | None:
    """Build the verifier against the live Herald assets (public JWKS
    at keys/jwks.json, status list at state/status-list.json). Returns
    None when those assets are absent — the Dispatcher then refuses
    any presented mandate as MANDATE_STATUS_UNAVAILABLE (fail closed)
    while mandate-less dispatch is unaffected."""
    d = Path(herald_dir) if herald_dir else default_herald_dir()
    jwks_path = d / "keys" / "jwks.json"
    sl_path = d / "state" / "status-list.json"
    if not jwks_path.exists() or not sl_path.exists():
        return None
    try:
        jwks = json.loads(jwks_path.read_text())
    except Exception:
        return None
    return HeraldMandateVerifier(jwks=jwks, status_list_path=sl_path)

# Eclipse Agent Wallet MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
