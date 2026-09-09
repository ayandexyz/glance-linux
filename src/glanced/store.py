"""Encrypted enrollment store.

Face data never leaves this machine and is never stored as an image. Each
enrolled capture is a 512-d embedding; the original frame is discarded the
moment the embedding exists.

Key handling is where Linux and macOS diverge most. Upstream wraps its AES key
in the macOS Keychain behind `userPresence`, so Touch ID gates it. There is no
single equivalent here, so the key is derived with scrypt from a passphrase the
user sets at enrollment, and held in memory only while a session is authorized
(see `SessionAutoLocker` upstream — the same idle re-lock applies).

What is deliberately absent: there is no stored login password. Upstream has to
keep one, because macOS offers third-party code no way to authorize a login and
Glance unlocks by typing it. Linux has PAM, so the daemon authorizes the session
directly and never needs the user's password at all. That removes the single
most sensitive secret in the macOS design.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

DEFAULT_STORE_PATH = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
) / "glance" / "enrollment.bin"

SCRYPT_N = 2**16
SCRYPT_R = 8
SCRYPT_P = 1
KEY_BYTES = 32
SALT_BYTES = 16
NONCE_BYTES = 12


@dataclass
class Identity:
    """One enrolled face. Several may belong to the same person — with glasses,
    with a beard, under different lighting — and each can be disabled without
    being deleted, matching upstream's multiple-identity model."""

    name: str
    #: (N, 512) float32, L2-normalized. One row per enrollment capture.
    embeddings: np.ndarray
    enabled: bool = True

    def best_similarity(self, probe: np.ndarray) -> float:
        if len(self.embeddings) == 0:
            return -1.0
        return float(np.max(self.embeddings @ probe))


@dataclass
class EnrollmentStore:
    identities: list[Identity] = field(default_factory=list)

    def match(self, probe: np.ndarray, threshold: float) -> Optional[tuple[Identity, float]]:
        """Best enabled identity above `threshold`, or None."""
        best: Optional[tuple[Identity, float]] = None
        for identity in self.identities:
            if not identity.enabled:
                continue
            score = identity.best_similarity(probe)
            if score >= threshold and (best is None or score > best[1]):
                best = (identity, score)
        return best


def derive_key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    return Scrypt(salt=salt, length=KEY_BYTES, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        passphrase.encode("utf-8")
    )


def save(store: EnrollmentStore, passphrase: str, path: Path = DEFAULT_STORE_PATH) -> None:
    """Encrypt and write, AES-256-GCM. Salt and nonce are prepended in the clear
    — both are public inputs, and keeping them with the ciphertext is what makes
    the file self-describing."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    payload = json.dumps(
        {
            "version": 1,
            "identities": [
                {
                    "name": i.name,
                    "enabled": i.enabled,
                    "embeddings": np.asarray(i.embeddings, dtype=np.float32).tolist(),
                }
                for i in store.identities
            ],
        }
    ).encode("utf-8")

    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(derive_key(passphrase, salt)).encrypt(nonce, payload, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(salt + nonce + ciphertext)
    os.chmod(temporary, 0o600)
    temporary.replace(path)  # atomic, so an interrupted save cannot truncate an enrollment


def load(passphrase: str, path: Path = DEFAULT_STORE_PATH) -> EnrollmentStore:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not path.exists():
        return EnrollmentStore()
    blob = path.read_bytes()
    salt, nonce, ciphertext = (
        blob[:SALT_BYTES],
        blob[SALT_BYTES : SALT_BYTES + NONCE_BYTES],
        blob[SALT_BYTES + NONCE_BYTES :],
    )
    payload = json.loads(
        AESGCM(derive_key(passphrase, salt)).decrypt(nonce, ciphertext, None).decode("utf-8")
    )
    return EnrollmentStore(
        identities=[
            Identity(
                name=entry["name"],
                embeddings=np.asarray(entry["embeddings"], dtype=np.float32).reshape(-1, 512),
                enabled=entry.get("enabled", True),
            )
            for entry in payload["identities"]
        ]
    )
