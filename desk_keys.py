"""Desk-domain signer (per-desk secp256k1 spend-authorization keys).

Domain rules (mirroring Eclipse Anchor's fund signer, adapted to the
desk domain):
- One key per desk; the desk id — and, once issued, the wallet id — are
  bound into the envelope and checked before any signature.
- Sign-only API: `sign_spend` returns a signature hex. There is NO
  method that returns raw key bytes, and no transaction-signing method
  (a desk key authorizes ledger spends inside the wallet policy engine;
  it is not a chain fund key).
- The private key exists in memory only transiently inside a sign call.
Custody: envelope-encrypted under ~/.secrets (see desk_envelope.py).
"""
from pathlib import Path

from eth_account import Account
from eth_account.messages import encode_defunct

import desk_envelope
import desk_verify


class DeskSignerError(Exception):
    pass


class DeskSigner:
    DOMAIN = "desk"

    def __init__(self, envelope_path, master_key_path, expected_desk_id: str):
        self._envelope_path = Path(envelope_path).expanduser()
        self._master_key_path = master_key_path
        env = desk_envelope.read_envelope(self._envelope_path)
        if env.get("domain") != "desk":
            raise DeskSignerError("envelope is not a desk-domain key")
        if env.get("desk_id") != expected_desk_id:
            raise DeskSignerError(
                f"desk separation violated: envelope desk {env.get('desk_id')} "
                f"!= requested desk {expected_desk_id}")
        self.desk_id = expected_desk_id
        self.public_key_hex = env["public_key"]
        self.address = env["address"]
        self.wallet_id = env.get("wallet_id")  # set at issuance binding

    # -- sign-only API ---------------------------------------------------
    def _sign(self, msg: bytes, wallet_id: str) -> str:
        if self.wallet_id is not None and wallet_id != self.wallet_id:
            raise DeskSignerError(
                f"refused: desk {self.desk_id} key is bound to wallet "
                f"{self.wallet_id}, not {wallet_id}")
        raw_key = None
        try:
            raw_key = desk_envelope.open_envelope(
                self._master_key_path,
                desk_envelope.read_envelope(self._envelope_path))
            acct = Account.from_key(raw_key)
            if acct.address.lower() != self.address.lower():
                raise DeskSignerError("decrypted key does not match envelope address")
            signed = Account.sign_message(encode_defunct(msg), raw_key)
            return signed.signature.hex()
        finally:
            raw_key = None  # transient only; drop the reference

    def sign_spend(self, *, wallet_id: str, to: str, amount_micro: int,
                   memo: str, nonce: int) -> str:
        """Sign one v1 spend authorization (the certified v0.2.0
        message). Refuses if this desk key is bound to a different
        wallet (cross-desk/cross-wallet isolation enforced at the
        signer, before the server even sees it)."""
        msg = desk_verify.spend_message(wallet_id, to, amount_micro, memo, nonce)
        return self._sign(msg, wallet_id)

    def sign_spend_v2(self, *, wallet_id: str, asset_id: str,
                      chain_id: str, to: str, amount_base: int,
                      memo: str, nonce: int, idempotency_key: str) -> str:
        """Sign one v2 spend authorization (silas-view S3.2): the
        signature binds wallet + asset x chain + amount in base units
        + recipient + memo + nonce + idempotency key. Same binding
        refusal as v1."""
        msg = desk_verify.spend_message_v2(
            wallet_id, asset_id, chain_id, to, amount_base, memo, nonce,
            idempotency_key)
        return self._sign(msg, wallet_id)


def create_desk_key(secrets_dir, master_key_path, desk_id: str, purpose: str) -> dict:
    """Generate a NEW desk key, envelope-encrypt it, return PUBLIC info only."""
    acct = Account.create()
    pub_bytes = acct._key_obj.public_key.to_bytes()  # 64-byte X||Y
    env = desk_envelope.create_envelope(
        master_key_path, bytes.fromhex(acct.key.hex()),
        {"domain": "desk", "desk_id": desk_id, "purpose": purpose,
         "address": acct.address, "public_key": pub_bytes.hex(),
         "curve": "secp256k1", "wallet_id": None})
    path = Path(secrets_dir).expanduser() / f"desk-key-{desk_id}.json"
    if path.exists():
        raise DeskSignerError(f"refusing to overwrite existing key envelope: {path}")
    desk_envelope.write_envelope(path, env)
    return {"address": acct.address, "public_key": pub_bytes.hex(),
            "desk_id": desk_id, "envelope": str(path)}


def bind_wallet(secrets_dir, desk_id: str, wallet_id: str) -> None:
    """Record the issued wallet id in the desk envelope's (plaintext,
    Anchor-style) metadata so the signer can enforce its binding.
    Ciphertext/key material are not touched."""
    path = Path(secrets_dir).expanduser() / f"desk-key-{desk_id}.json"
    env = desk_envelope.read_envelope(path)
    if env.get("wallet_id") not in (None, wallet_id):
        raise DeskSignerError(
            f"desk {desk_id} already bound to wallet {env.get('wallet_id')}")
    env["wallet_id"] = wallet_id
    desk_envelope.write_envelope(path, env)


def load_desk_signer(secrets_dir, master_key_path, desk_id: str) -> DeskSigner:
    return DeskSigner(Path(secrets_dir).expanduser() / f"desk-key-{desk_id}.json",
                      master_key_path, desk_id)
