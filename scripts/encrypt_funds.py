import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


ITERATIONS = 250_000


def b64url(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def derive_key(password, salt):
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_file(input_path, output_path, password):
    plaintext = Path(input_path).read_bytes()
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = derive_key(password, salt)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    payload = {
        "version": 1,
        "algorithm": "AES-GCM",
        "kdf": "PBKDF2-SHA256",
        "iterations": ITERATIONS,
        "salt": b64url(salt),
        "iv": b64url(iv),
        "ciphertext": b64url(ciphertext),
    }
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/funds.raw.json")
    parser.add_argument("--output", default="data/funds.enc.json")
    args = parser.parse_args()
    password = os.environ.get("DASHBOARD_PASSWORD")
    if not password:
        raise SystemExit("DASHBOARD_PASSWORD environment variable is required.")
    encrypt_file(args.input, args.output, password)
    print(f"Wrote encrypted data to {args.output}")


if __name__ == "__main__":
    main()
