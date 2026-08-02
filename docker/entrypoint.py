#!/usr/bin/env python3
from __future__ import annotations

import base64
import binascii
import http.client
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


APP_UID = int(os.environ.get("WG_MANAGER_UID", "10001"))
APP_GID = int(os.environ.get("WG_MANAGER_GID", "10001"))
STARTUP_TIMEOUT = int(os.environ.get("WG_CONTAINER_STARTUP_TIMEOUT_SECONDS", "90"))


def _status(label: str, message: str) -> None:
    print(f"{label}: {message}", flush=True)


def _validate_public_key(value: str) -> str:
    candidate = value.strip()
    try:
        raw = base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RuntimeError("WireGuard server public key is not valid base64") from error
    if len(raw) != 32:
        raise RuntimeError("WireGuard server public key must decode to 32 bytes")
    return candidate


def _safe_managed_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    resolved = path.resolve()
    unsafe = {
        Path("/"),
        Path("/opt"),
        Path("/run"),
        Path("/tmp"),
        Path("/var"),
        Path("/var/lib"),
    }
    if (
        not path.is_absolute()
        or absolute in unsafe
        or resolved in unsafe
        or len(resolved.parts) < 3
    ):
        raise RuntimeError(f"refusing unsafe managed data directory: {resolved}")
    return resolved


def _prepare_manager_data(path: Path) -> None:
    if os.geteuid() == 0:
        raise RuntimeError("manager container must run as non-root UID/GID 10001")
    if os.geteuid() != APP_UID or os.getegid() != APP_GID:
        raise RuntimeError("manager process has an unexpected uid/gid")
    resolved = _safe_managed_directory(path)
    resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved.chmod(0o700)
    if not os.access(resolved, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError("manager data directory is not accessible to UID/GID 10001")


def _prepare_reconciler_directories(state_dir: Path, runtime_dir: Path) -> None:
    if os.geteuid() != 0:
        raise RuntimeError("reconciler container must start as root")
    if os.getegid() != APP_GID:
        raise RuntimeError("reconciler container must run as UID/GID 0:10001")
    for path in (state_dir, runtime_dir):
        resolved = _safe_managed_directory(path)
        resolved.mkdir(parents=True, exist_ok=True)
        metadata = resolved.stat()
        if metadata.st_uid != 0 or metadata.st_gid != APP_GID:
            raise RuntimeError(
                f"reconciler directory must be owned by 0:{APP_GID}: {resolved}"
            )
        resolved.chmod(0o750)


def _drop_to_manager() -> None:
    if os.geteuid() != APP_UID or os.getegid() != APP_GID:
        raise RuntimeError("manager process has an unexpected uid/gid")


def _wait_until(description: str, predicate) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (OSError, subprocess.SubprocessError):
            pass
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {description}")


def _server_public_key() -> str:
    configured = os.environ.get("WG_SERVER_PUBLIC_KEY", "").strip()
    if configured:
        return _validate_public_key(configured)
    key_path = Path(
        os.environ.get(
            "WG_SERVER_PUBLIC_KEY_FILE",
            "/var/lib/wireguard-manager-reconciler/server-public-key",
        )
    )
    _wait_until("reconciler server public key", key_path.is_file)
    return _validate_public_key(key_path.read_text(encoding="ascii"))


def _wait_for_socket() -> Path:
    socket_path = Path(
        os.environ.get("WG_RECONCILE_SOCKET", "/run/wireguard-manager/reconcile.sock")
    )

    def ready() -> bool:
        return socket_path.exists() and stat.S_ISSOCK(socket_path.stat().st_mode)

    _wait_until("reconciler Unix socket", ready)
    return socket_path


def _wg_public_key(interface: str) -> str:
    def read_key() -> bool:
        result = subprocess.run(
            ["wg", "show", interface, "public-key"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        read_key.value = _validate_public_key(result.stdout)
        return True

    read_key.value = ""
    _wait_until(f"WireGuard interface {interface}", read_key)
    return read_key.value


def _atomic_public_key(path: Path, value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".server-public-key.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=True) as stream:
            stream.write(value + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _validate_endpoint() -> None:
    endpoint = os.environ.get("WG_ENDPOINT", "")
    if not endpoint or "example.invalid" in endpoint or endpoint.startswith("REPLACE_"):
        raise RuntimeError("WG_ENDPOINT must contain the real public hostname/IP and UDP port")


def _manager() -> None:
    os.umask(0o077)
    _validate_endpoint()
    data_dir = Path(os.environ.get("WG_MANAGER_DATA_DIR", "/var/lib/wireguard-manager"))
    _prepare_manager_data(data_dir)
    os.environ["WG_SERVER_PUBLIC_KEY"] = _server_public_key()
    _wait_for_socket()
    _drop_to_manager()
    subprocess.run(["wg-manager", "reconcile"], check=True)
    _status("READY", "manager Web process starting as non-root")
    os.execvp("wg-manager-web", ["wg-manager-web"])


def _reconciler() -> None:
    os.umask(0o007)
    state_dir = Path(
        os.environ.get(
            "WG_RECONCILE_STATE_DIR", "/var/lib/wireguard-manager-reconciler"
        )
    )
    runtime_dir = Path(
        os.environ.get("WG_RECONCILE_RUNTIME_DIR", "/run/wireguard-manager")
    )
    _prepare_reconciler_directories(state_dir, runtime_dir)
    interface = os.environ.get("WG_INTERFACE", "wg0")
    public_key = _wg_public_key(interface)
    _atomic_public_key(state_dir / "server-public-key", public_key)
    _status("READY", "reconciler starting; server public key exported without logging it")
    os.execvp("wg-manager-reconciler", ["wg-manager-reconciler", "serve"])


def _cli(arguments: list[str]) -> None:
    if not arguments:
        raise RuntimeError("cli role requires wg-manager arguments")
    os.umask(0o077)
    _validate_endpoint()
    data_dir = Path(os.environ.get("WG_MANAGER_DATA_DIR", "/var/lib/wireguard-manager"))
    _prepare_manager_data(data_dir)
    os.environ["WG_SERVER_PUBLIC_KEY"] = _server_public_key()
    _wait_for_socket()
    _drop_to_manager()
    os.execvp("wg-manager", ["wg-manager", *arguments])


def _healthcheck_manager() -> None:
    host = os.environ.get("WG_WEB_HOST", "10.44.0.1")
    port = int(os.environ.get("WG_WEB_PORT", "8081"))
    connection = http.client.HTTPConnection(host, port, timeout=3)
    try:
        connection.request("GET", "/login")
        response = connection.getresponse()
        response.read(1024)
        if response.status != 200:
            raise RuntimeError(f"unexpected manager health status: {response.status}")
    finally:
        connection.close()


def _healthcheck_reconciler() -> None:
    socket_path = Path(
        os.environ.get("WG_RECONCILE_SOCKET", "/run/wireguard-manager/reconcile.sock")
    )
    if not socket_path.exists() or not stat.S_ISSOCK(socket_path.stat().st_mode):
        raise RuntimeError("reconciler socket is unavailable")
    interface = os.environ.get("WG_INTERFACE", "wg0")
    result = subprocess.run(
        ["wg", "show", interface], capture_output=True, check=False, timeout=3
    )
    if result.returncode != 0:
        raise RuntimeError("WireGuard interface is unavailable")


def main(arguments: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    role = argv.pop(0) if argv else "manager"
    try:
        if role == "manager":
            _manager()
        elif role == "reconciler":
            _reconciler()
        elif role == "cli":
            _cli(argv)
        elif role == "healthcheck-manager":
            _healthcheck_manager()
        elif role == "healthcheck-reconciler":
            _healthcheck_reconciler()
        elif role == "version":
            os.execvp("wg-manager", ["wg-manager", "--version"])
        else:
            raise RuntimeError(f"unknown container role: {role}")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        _status("BLOCKED", str(error))
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
