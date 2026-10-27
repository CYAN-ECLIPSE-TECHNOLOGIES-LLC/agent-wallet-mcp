"""Fund signer for the B1 testnet cell (NEW key domain, silas-view S3.1).

Separate from the desk-authorization domain in every way that matters:
- Its envelopes live in a SEPARATE fund vault
  (~/.secrets/eclipse-agent-wallet-funds/) with its OWN master key.
  The desk vault (~/.secrets/eclipse-agent-wallet-desks/) is never
  read or written by this module.
- It signs CHAIN TRANSACTIONS (EIP-1559) on ONE chain only; desk keys
  sign spend authorizations and never sign transactions.
- Sign-only API: sign_pay() returns a signed artifact; no method
  returns key material. The private key exists in memory only
  transiently inside a sign call (existing house pattern).

Signer-side invariants (S3.1 rule 4) - the signer refuses even if the
server/dispatcher asks:
- a valid desk-auth v2 authorization for the exact intent must be
  presented by the dispatcher and verify against the desk public key
  (refuses DESK_AUTH_MISSING / DESK_AUTH_INVALID without it);
- envelope chain_id/family must match the artifact;
- ERC-20 pays refuse value != 0; native pays refuse contract-call data
  and must carry value == authorized amount;
- the amount encoded in the artifact must equal the amount in the
  v2-authorized intent;
- fee (gas x maxFeePerGas) must be <= the fee quote cap presented;
- the artifact's asset must be inside the envelope's asset_scope.

This build is TESTNET-ONLY: envelope creation and signer construction
refuse any chain_id other than eip155:84532.
"""
from __future__ import annotations

from pathlib import Path

from eth_account import Account

import desk_envelope
import desk_verify

TESTNET_CHAIN_ID = "eip155:84532"
DEFAULT_FUND_VAULT = Path("~/.secrets/eclipse-agent-wallet-funds")
_TRANSFER_SELECTOR = "a9059cbb"


class FundSignerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _require_intent_auth(authorized_intent) -> dict:
    if not isinstance(authorized_intent, dict):
        raise FundSignerError(
            "DESK_AUTH_MISSING",
            "fund signer requires the dispatcher to present a desk-auth "
            "v2 authorized intent; got none")
    missing = [f for f in ("wallet_id", "asset_id", "chain_id", "to",
                           "amount_base", "memo", "desk_nonce",
                           "idempotency_key", "desk_auth_signature")
               if f not in authorized_intent]
    if missing:
        raise FundSignerError(
            "DESK_AUTH_MISSING",
            f"authorized intent missing field(s): {', '.join(missing)}")
    return authorized_intent


class FundSigner:
    DOMAIN = "fund"

    def __init__(self, envelope_path, master_key_path,
                 expected_chain_id: str = TESTNET_CHAIN_ID):
        if expected_chain_id != TESTNET_CHAIN_ID:
            raise FundSignerError(
                "MAINNET_REFUSED",
                f"this build's fund signer is testnet-only "
                f"({TESTNET_CHAIN_ID}); refusing {expected_chain_id!r}")
        self._envelope_path = Path(envelope_path).expanduser()
        self._master_key_path = master_key_path
        env = desk_envelope.read_envelope(self._envelope_path)
        if env.get("domain") != "fund":
            raise FundSignerError("WRONG_DOMAIN",
                                  "envelope is not a fund-domain key")
        if env.get("family") != "evm":
            raise FundSignerError("WRONG_DOMAIN",
                                  "envelope family is not evm")
        if env.get("chain_id") != expected_chain_id:
            raise FundSignerError(
                "CHAIN_MISMATCH",
                f"envelope chain {env.get('chain_id')} != "
                f"{expected_chain_id}")
        self.chain_id = expected_chain_id
        self.family = "evm"
        self.address = env["address"]
        self.public_key_hex = env["public_key"]
        self.asset_scope = list(env.get("asset_scope") or [])
        self.wallet_id = env.get("wallet_id")

    # -- sign-only API ---------------------------------------------------
    def sign_pay(self, artifact: dict, *, authorized_intent: dict,
                 desk_public_key: bytes,
                 fee_quote_max_base: int) -> dict:
        auth = _require_intent_auth(authorized_intent)
        # 1. desk-auth v2 must verify for the EXACT intent fields
        try:
            sig = bytes.fromhex(str(auth["desk_auth_signature"]).removeprefix("0x"))
        except ValueError:
            raise FundSignerError("DESK_AUTH_INVALID",
                                  "desk_auth_signature is not hex")
        if not desk_verify.verify_spend_v2(
                desk_public_key, sig, wallet_id=auth["wallet_id"],
                asset_id=auth["asset_id"], chain_id=auth["chain_id"],
                to=auth["to"], amount_base=auth["amount_base"],
                memo=auth["memo"], nonce=auth["desk_nonce"],
                idempotency_key=auth["idempotency_key"]):
            raise FundSignerError(
                "DESK_AUTH_INVALID",
                "desk-auth v2 signature does not verify for this intent")
        # 2. envelope chain/family must match the artifact + intent
        if artifact.get("chain_id") != self.chain_id or \
                auth["chain_id"] != self.chain_id:
            raise FundSignerError(
                "CHAIN_MISMATCH",
                f"artifact/intent chain != envelope chain {self.chain_id}")
        if artifact.get("asset_id") != auth["asset_id"]:
            raise FundSignerError("AMOUNT_MISMATCH",
                                  "artifact asset != authorized asset")
        if self.asset_scope and auth["asset_id"] not in self.asset_scope:
            raise FundSignerError(
                "SCOPE_REFUSED",
                f"asset {auth['asset_id']} is outside this fund key's "
                f"asset_scope {self.asset_scope}")
        amount = artifact.get("amount_base")
        if isinstance(amount, bool) or not isinstance(amount, int) \
                or amount != auth["amount_base"]:
            raise FundSignerError(
                "AMOUNT_MISMATCH",
                "artifact amount != v2-authorized intent amount")
        # 3. per-asset rail rules (S3.1 rule 4)
        data = artifact.get("data") or "0x"
        value = artifact.get("value_base")
        if artifact.get("transfer_method") == "erc20":
            if value != 0:
                raise FundSignerError(
                    "ERC20_VALUE_REFUSED",
                    "ERC-20 pays must carry value == 0")
            body = data[2:] if data.startswith("0x") else data
            if not body.startswith(_TRANSFER_SELECTOR) or len(body) != 136:
                raise FundSignerError("ARTIFACT_INVALID",
                                      "ERC-20 artifact data is not a "
                                      "transfer(address,uint256) call")
            encoded_to = "0x" + body[32:72]
            encoded_amount = int(body[72:136], 16)
            if encoded_amount != amount:
                raise FundSignerError(
                    "AMOUNT_MISMATCH",
                    "calldata amount != authorized amount")
            if encoded_to.lower() != str(auth["to"]).lower():
                raise FundSignerError(
                    "AMOUNT_MISMATCH",
                    "calldata recipient != authorized recipient")
        elif artifact.get("transfer_method") == "native-evm":
            if data not in ("0x", ""):
                raise FundSignerError(
                    "NATIVE_DATA_REFUSED",
                    "native pays must not carry contract-call data")
            if value != amount:
                raise FundSignerError(
                    "AMOUNT_MISMATCH",
                    "native value != authorized amount")
        else:
            raise FundSignerError(
                "ARTIFACT_INVALID",
                f"unsupported transfer_method "
                f"{artifact.get('transfer_method')!r}")
        # 4. fee must be <= the quote cap presented
        for f in ("nonce", "gas", "max_fee_per_gas",
                  "max_priority_fee_per_gas"):
            v = artifact.get(f)
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise FundSignerError("ARTIFACT_INVALID",
                                      f"artifact field {f} must be a "
                                      "non-negative int")
        if isinstance(fee_quote_max_base, bool) or \
                not isinstance(fee_quote_max_base, int):
            raise FundSignerError("FEE_OVER_CAP",
                                  "a fee quote cap (int) must be presented")
        fee_max = artifact["gas"] * artifact["max_fee_per_gas"]
        if fee_max > fee_quote_max_base:
            raise FundSignerError(
                "FEE_OVER_CAP",
                f"fee {fee_max} wei exceeds quote cap "
                f"{fee_quote_max_base} wei")
        # 5. sign (key transient in memory only)
        tx = {"type": 2, "chainId": int(self.chain_id.split(":")[1]),
              "nonce": artifact["nonce"], "gas": artifact["gas"],
              "maxFeePerGas": artifact["max_fee_per_gas"],
              "maxPriorityFeePerGas": artifact["max_priority_fee_per_gas"],
              "to": artifact["to"], "value": value,
              "data": bytes.fromhex(data[2:]) if data.startswith("0x")
              and len(data) > 2 else b""}
        raw_key = None
        try:
            raw_key = desk_envelope.open_envelope(
                self._master_key_path,
                desk_envelope.read_envelope(self._envelope_path))
            acct = Account.from_key(raw_key)
            if acct.address.lower() != self.address.lower():
                raise FundSignerError(
                    "CHAIN_MISMATCH",
                    "decrypted key does not match envelope address")
            signed = acct.sign_transaction(tx)
        finally:
            raw_key = None
        raw = signed.raw_transaction.hex()
        return {**artifact, "signed": True,
                "raw_tx": "0x" + raw,
                "tx_hash": "0x" + signed.hash.hex(),
                "from": self.address, "fee_max_base": fee_max}


def create_fund_key(secrets_dir, master_key_path, *, chain_id: str,
                    asset_scope: list, purpose: str,
                    wallet_id: str | None = None) -> dict:
    """Generate a NEW fund key for the testnet cell, envelope-encrypt
    it into the FUND vault, return PUBLIC info only. Refuses any
    non-testnet chain and refuses to overwrite an existing envelope."""
    if chain_id != TESTNET_CHAIN_ID:
        raise FundSignerError(
            "MAINNET_REFUSED",
            f"this build creates fund keys for {TESTNET_CHAIN_ID} only; "
            f"refusing {chain_id!r}")
    acct = Account.create()
    pub_bytes = acct._key_obj.public_key.to_bytes()
    env = desk_envelope.create_envelope(
        master_key_path, bytes.fromhex(acct.key.hex()),
        {"domain": "fund", "family": "evm", "chain_id": chain_id,
         "asset_scope": list(asset_scope), "purpose": purpose,
         "address": acct.address, "public_key": pub_bytes.hex(),
         "curve": "secp256k1", "wallet_id": wallet_id})
    path = Path(secrets_dir).expanduser() / \
        f"fund-key-evm-{chain_id.split(':')[1]}.json"
    if path.exists():
        raise FundSignerError(
            f"refusing to overwrite existing fund envelope: {path}")
    desk_envelope.write_envelope(path, env)
    return {"address": acct.address, "public_key": pub_bytes.hex(),
            "chain_id": chain_id, "asset_scope": list(asset_scope),
            "envelope": str(path)}


def load_fund_signer(secrets_dir, master_key_path,
                     chain_id: str = TESTNET_CHAIN_ID) -> FundSigner:
    return FundSigner(
        Path(secrets_dir).expanduser() /
        f"fund-key-evm-{chain_id.split(':')[1]}.json",
        master_key_path, chain_id)
