from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import struct
import tempfile
from pathlib import Path


def desired_state(connection, interface: str) -> dict:
    peers = connection.execute(
        """SELECT devices.id, devices.public_key, devices.static_ip
           FROM devices JOIN users ON users.id = devices.user_id
           WHERE users.enabled = 1
           ORDER BY devices.static_ip, devices.id"""
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


class ReconcilerAdapter:
    def __init__(
        self,
        path: str | Path,
        interface: str,
        *,
        socket_path: str | Path,
        status_path: str | Path,
        timeout: float,
    ):
        self.file_adapter = FileAdapter(path, interface)
        self.socket_path = Path(socket_path)
        self.status_path = Path(status_path)
        self.timeout = timeout

    def reconcile(self, connection) -> str:
        expected_path = self.file_adapter.path
        previous_payload = expected_path.read_bytes() if expected_path.exists() else None
        digest = self.file_adapter.reconcile(connection)
        request_id = secrets.token_hex(16)
        try:
            _request_live_reconcile(self.socket_path, digest, request_id, self.timeout)
        except (OSError, RuntimeError):
            if _applied_status_matches(self.status_path, digest, request_id):
                return digest
            if previous_payload is None:
                expected_path.unlink(missing_ok=True)
            else:
                _atomic_write(expected_path, previous_payload)
            raise
        return digest


def make_adapter(
    kind: str,
    *,
    path: str | Path,
    interface: str,
    socket_path: str | Path | None = None,
    status_path: str | Path | None = None,
    timeout: float = 5,
):
    if kind == "file":
        return FileAdapter(path, interface)
    if kind == "dry-run":
        return DryRunAdapter(interface)
    if kind == "reconciler":
        if socket_path is None or status_path is None:
            raise RuntimeError("reconciler adapter requires socket and status paths")
        return ReconcilerAdapter(
            path,
            interface,
            socket_path=socket_path,
            status_path=status_path,
            timeout=timeout,
        )
    raise RuntimeError("unknown WireGuard adapter")


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


def _request_live_reconcile(
    socket_path: Path, digest: str, request_id: str, timeout: float
) -> None:
    request = json.dumps(
        {
            "action": "apply",
            "desired_sha256": digest,
            "request_id": request_id,
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("live reconciler requires Linux SO_PEERCRED support")
        _pid, peer_uid, _peer_gid = struct.unpack(
            "3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        )
        if peer_uid != 0:
            raise RuntimeError("reconciler socket is not owned by a root process")
        client.sendall(request)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 16384:
                raise RuntimeError("reconciler response is too large")
    try:
        payload = json.loads(response)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise RuntimeError("invalid reconciler response") from error
    if not isinstance(payload, dict):
        raise RuntimeError("invalid reconciler response")
    if (
        payload.get("status") != "applied"
        or payload.get("desired_sha256") != digest
        or payload.get("request_id") != request_id
    ):
        message = str(payload.get("message", "live reconciliation failed"))
        raise RuntimeError(message[:300])


def _applied_status_matches(path: Path, digest: str, request_id: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("status") == "applied"
        and payload.get("desired_sha256") == digest
        and payload.get("request_id") == request_id
    )
