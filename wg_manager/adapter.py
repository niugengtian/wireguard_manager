from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def desired_state(connection, interface: str) -> dict:
    peers = connection.execute(
        "SELECT id, public_key, static_ip FROM devices ORDER BY static_ip, id"
    ).fetchall()
    return {
        "format": 1,
        "interface": interface,
        "peers": [
            {
                "allowed_ips": [f"{row['static_ip']}/32"],
                "device_id": row["id"],
                "public_key": row["public_key"],
            }
            for row in peers
        ],
    }


def canonical_state_bytes(connection, interface: str) -> bytes:
    return (json.dumps(desired_state(connection, interface), sort_keys=True, indent=2) + "\n").encode("utf-8")


class FileAdapter:
    def __init__(self, path: str | Path, interface: str):
        self.path = Path(path)
        self.interface = interface

    def reconcile(self, connection) -> str:
        payload = canonical_state_bytes(connection, self.interface)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists():
            _atomic_write(self.path.with_suffix(self.path.suffix + ".previous"), self.path.read_bytes())
        _atomic_write(self.path, payload)
        return hashlib.sha256(payload).hexdigest()


class DryRunAdapter:
    def __init__(self, interface: str):
        self.interface = interface

    def reconcile(self, connection) -> str:
        return hashlib.sha256(canonical_state_bytes(connection, self.interface)).hexdigest()


def make_adapter(kind: str, *, path: str | Path, interface: str):
    if kind == "file":
        return FileAdapter(path, interface)
    if kind == "dry-run":
        return DryRunAdapter(interface)
    raise RuntimeError("only file and dry-run adapters are enabled; live wg/syncconf requires explicit approval")


def _atomic_write(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise
