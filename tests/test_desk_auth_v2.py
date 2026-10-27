"""Integration step (b) evidence: desk-auth v2 canonical message.

The v2 message binds wallet + asset x chain + amount in base units +
recipient + memo + nonce + idempotency key. Signing here is a
stdlib RFC 6979 helper over desk_verify's own curve code (the product
signer is desk_keys.DeskSigner, exercised when eth_account is
importable). v1 stays verifiable beside v2."""
import hashlib
import hmac
import unittest

import desk_verify
from desk_verify import _G, _N, _point_mul
from ledger_v2 import SANDBOX_ASSET_ID, SANDBOX_CHAIN_ID

PRIV = 0x1DCE8D2EC6184CAF0A971B96F8C41D1A6E3B9F5C7A2D4E6B8A0C1D2E3F4A5B6C % _N
PUB = _point_mul(PRIV, _G)
PUB_BYTES = PUB[0].to_bytes(32, "big") + PUB[1].to_bytes(32, "big")
WALLET = "wal_desk000001"
TO = "sandbox:bob"


def sign(priv: int, digest: bytes) -> bytes:
    """RFC 6979 deterministic ECDSA (test helper, stdlib only)."""
    z = int.from_bytes(digest, "big")
    bx = priv.to_bytes(32, "big") + (z % _N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + bx, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + bx, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < _N:
            break
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()
    r = _point_mul(cand, _G)[0] % _N
    s = (pow(cand, -1, _N) * (z + r * priv)) % _N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def v2_fields(**over):
    f = dict(wallet_id=WALLET, asset_id=SANDBOX_ASSET_ID,
             chain_id=SANDBOX_CHAIN_ID, to=TO, amount_base=2_500_000,
             memo="invoice 7", nonce=1, idempotency_key="idem-0001")
    f.update(over)
    return f


def sign_v2(**over) -> bytes:
    f = v2_fields(**over)
    return sign(PRIV, desk_verify.spend_digest(
        desk_verify.spend_message_v2(**f)))


class V2MessageTests(unittest.TestCase):
    def test_canonical_format_exact(self):
        msg = desk_verify.spend_message_v2(**v2_fields())
        self.assertEqual(
            msg.decode(),
            "agent-wallet-spend/v2|wal_desk000001|"
            "sandbox:local/token:USDC|sandbox:local|sandbox:bob|"
            "2500000|invoice 7|1|idem-0001")

    def test_roundtrip(self):
        self.assertTrue(desk_verify.verify_spend_v2(
            PUB_BYTES, sign_v2(), **v2_fields()))

    def test_every_bound_field_is_load_bearing(self):
        sig = sign_v2()
        for over in ({"amount_base": 2_500_001}, {"to": "sandbox:eve"},
                     {"asset_id": "sandbox:local/token:EURC"},
                     {"chain_id": "eip155:8453"}, {"nonce": 2},
                     {"idempotency_key": "idem-0002"}, {"memo": "invoice 8"},
                     {"wallet_id": "wal_other"}):
            self.assertFalse(
                desk_verify.verify_spend_v2(PUB_BYTES, sig,
                                            **v2_fields(**over)), over)

    def test_v1_and_v2_are_not_interchangeable(self):
        v1_sig = sign(PRIV, desk_verify.spend_digest(desk_verify.spend_message(
            WALLET, TO, 2_500_000, "invoice 7", 1)))
        self.assertFalse(desk_verify.verify_spend_v2(
            PUB_BYTES, v1_sig, **v2_fields()))
        self.assertFalse(desk_verify.verify_spend_v1(
            PUB_BYTES, sign_v2(), wallet_id=WALLET, to=TO,
            amount_micro=2_500_000, memo="invoice 7", nonce=1))

    def test_v1_still_verifies(self):
        v1_sig = sign(PRIV, desk_verify.spend_digest(desk_verify.spend_message(
            WALLET, TO, 2_500_000, "invoice 7", 1)))
        self.assertTrue(desk_verify.verify_spend_v1(
            PUB_BYTES, v1_sig, wallet_id=WALLET, to=TO,
            amount_micro=2_500_000, memo="invoice 7", nonce=1))

    def test_malformed_never_raises(self):
        for bad_sig in (b"", b"\x00" * 63, b"\xff" * 64, None):
            self.assertFalse(desk_verify.verify_spend_v2(
                PUB_BYTES, bad_sig, **v2_fields()))
        self.assertFalse(desk_verify.verify_spend_v2(
            PUB_BYTES, sign_v2(), **v2_fields(amount_base="x")))
        self.assertFalse(desk_verify.verify_spend_v2(
            b"\x00" * 10, sign_v2(), **v2_fields()))

    def test_desk_address_from_pubkey(self):
        addr = desk_verify.desk_address(PUB_BYTES)
        self.assertTrue(addr.startswith("0x") and len(addr) == 42)


try:
    import eth_account  # noqa: F401
    HAVE_ETH_ACCOUNT = True
except ImportError:
    HAVE_ETH_ACCOUNT = False


@unittest.skipUnless(HAVE_ETH_ACCOUNT,
                     "eth_account not importable in this interpreter")
class DeskSignerV2Tests(unittest.TestCase):
    def test_signer_roundtrip_and_binding(self):
        import tempfile
        from pathlib import Path
        import desk_envelope
        import desk_keys
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            master = vault / "master.key"
            desk_envelope.ensure_master_key(master)
            info = desk_keys.create_desk_key(vault, master, "desk-t",
                                             "test")
            signer = desk_keys.load_desk_signer(vault, master, "desk-t")
            pub = bytes.fromhex(info["public_key"])
            sig = bytes.fromhex(signer.sign_spend_v2(
                wallet_id="wal_x", asset_id=SANDBOX_ASSET_ID,
                chain_id=SANDBOX_CHAIN_ID, to=TO, amount_base=5,
                memo="", nonce=1, idempotency_key="k1"))
            self.assertTrue(desk_verify.verify_spend_v2(
                pub, sig, wallet_id="wal_x", asset_id=SANDBOX_ASSET_ID,
                chain_id=SANDBOX_CHAIN_ID, to=TO, amount_base=5, memo="",
                nonce=1, idempotency_key="k1"))
            desk_keys.bind_wallet(vault, "desk-t", "wal_x")
            signer = desk_keys.load_desk_signer(vault, master, "desk-t")
            with self.assertRaises(desk_keys.DeskSignerError):
                signer.sign_spend_v2(
                    wallet_id="wal_other", asset_id=SANDBOX_ASSET_ID,
                    chain_id=SANDBOX_CHAIN_ID, to=TO, amount_base=5,
                    memo="", nonce=1, idempotency_key="k1")


if __name__ == "__main__":
    unittest.main()
