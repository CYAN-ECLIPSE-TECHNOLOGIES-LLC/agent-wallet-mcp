"""Envelope-encryption primitives (INTERNAL) — desk-key domain.

Copied pattern-for-pattern from Eclipse Anchor's anchor/_envelope.py
(the house custody pattern): a master key (32 random bytes, file mode
0600, OUTSIDE the repo) wraps a per-key random DEK with AES-256-GCM;
the DEK encrypts the key material with AES-256-GCM. This module is the
only place raw desk key bytes are decrypted, and it is private: the
signer class in desk_keys.py calls it internally and exposes a
sign-only API. No function here returns key material to callers of the
signer.
"""
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _ub64(s: str) -> bytes:
    return base64.b64decode(s)


def load_master_key(master_key_path) -> bytes:
    p = Path(master_key_path).expanduser()
    key = p.read_bytes()
    if len(key) != 32:
        raise ValueError("master key must be exactly 32 bytes")
    return key


def ensure_master_key(master_key_path) -> Path:
    """Create the vault master key (0600) if absent; return its path."""
    p = Path(master_key_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    if not p.exists():
        p.write_bytes(os.urandom(32))
        os.chmod(p, 0o600)
        # verify the write landed intact (VM write-latch discipline)
        if len(p.read_bytes()) != 32:
            raise IOError(f"master key write verification failed for {p}")
    return p


def create_envelope(master_key_path, plaintext: bytes, meta: dict) -> dict:
    master = load_master_key(master_key_path)
    dek = os.urandom(32)
    mk = AESGCM(master)
    dek_nonce = os.urandom(12)
    wrapped_dek = mk.encrypt(dek_nonce, dek, None)
    dk = AESGCM(dek)
    data_nonce = os.urandom(12)
    ciphertext = dk.encrypt(data_nonce, plaintext, None)
    env = {
        "version": "envelope/1",
        "scheme": "AES-256-GCM; DEK wrapped by master key (AES-256-GCM)",
        "dek_nonce": _b64(dek_nonce),
        "wrapped_dek": _b64(wrapped_dek),
        "data_nonce": _b64(data_nonce),
        "ciphertext": _b64(ciphertext),
    }
    env.update(meta)
    return env


def open_envelope(master_key_path, envelope: dict) -> bytes:
    """Decrypt and return plaintext. Internal to the signer class only."""
    master = load_master_key(master_key_path)
    mk = AESGCM(master)
    dek = mk.decrypt(_ub64(envelope["dek_nonce"]), _ub64(envelope["wrapped_dek"]), None)
    dk = AESGCM(dek)
    return dk.decrypt(_ub64(envelope["data_nonce"]), _ub64(envelope["ciphertext"]), None)


def write_envelope(path, envelope: dict) -> None:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    p.write_text(json.dumps(envelope, indent=1))
    os.chmod(p, 0o600)
    # verify the write landed intact (VM write-latch discipline)
    back = json.loads(p.read_text())
    if back.get("ciphertext") != envelope.get("ciphertext"):
        raise IOError(f"envelope write verification failed for {p}")


def read_envelope(path) -> dict:
    return json.loads(Path(path).expanduser().read_text())
