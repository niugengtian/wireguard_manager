from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from werkzeug.security import check_password_hash, generate_password_hash


DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(32), method="scrypt")


def hash_password(password: str) -> str:
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("password must contain 12 to 1024 characters")
    return generate_password_hash(password, method="scrypt")


def verify_password(stored_hash: str, candidate: str) -> bool:
    try:
        return check_password_hash(stored_hash, candidate)
    except (ValueError, TypeError):
        return False


def generate_keypair() -> tuple[str, str]:
    private_key = X25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.b64encode(private_raw).decode("ascii"), base64.b64encode(public_raw).decode("ascii")


def load_or_create_secret(data_dir: Path) -> str:
    path = data_dir / "session-secret"
    if path.exists():
        secret = path.read_text(encoding="ascii").strip()
        if len(secret) < 32:
            raise RuntimeError("session secret file is invalid")
        return secret
    secret = secrets.token_urlsafe(48)
    # O_EXCL avoids racing two first starts into silently replacing the key.
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_or_create_secret(data_dir)
    try:
        os.write(descriptor, secret.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return secret


def opaque_source_hash(secret: str, username: str, source: str) -> str:
    message = f"{username.casefold()}\0{source}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def throttle_status(connection, login_key: str, *, limit: int, window_seconds: int) -> int:
    now = int(time.time())
    row = connection.execute(
        "SELECT failures, window_started, blocked_until FROM login_throttle WHERE login_key = ?",
        (login_key,),
    ).fetchone()
    if row is None:
        return 0
    if row["blocked_until"] > now:
        return row["blocked_until"] - now
    if now - row["window_started"] >= window_seconds:
        connection.execute("DELETE FROM login_throttle WHERE login_key = ?", (login_key,))
        return 0
    if row["failures"] >= limit:
        return max(1, window_seconds - (now - row["window_started"]))
    return 0


def record_login_failure(connection, login_key: str, *, limit: int, window_seconds: int) -> None:
    now = int(time.time())
    row = connection.execute(
        "SELECT failures, window_started FROM login_throttle WHERE login_key = ?",
        (login_key,),
    ).fetchone()
    if row is None or now - row["window_started"] >= window_seconds:
        failures = 1
        started = now
    else:
        failures = row["failures"] + 1
        started = row["window_started"]
    blocked_until = started + window_seconds if failures >= limit else 0
    connection.execute(
        """INSERT INTO login_throttle(login_key, failures, window_started, blocked_until)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(login_key) DO UPDATE SET
             failures=excluded.failures,
             window_started=excluded.window_started,
             blocked_until=excluded.blocked_until""",
        (login_key, failures, started, blocked_until),
    )


def clear_login_failures(connection, login_key: str) -> None:
    connection.execute("DELETE FROM login_throttle WHERE login_key = ?", (login_key,))
