from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import socket
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MAX_EXPECTED_BYTES = 1024 * 1024
MAX_PEERS = 4096


class ReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Section:
    name: str
    lines: tuple[str, ...]

    def value(self, key: str) -> str | None:
        prefix = key.casefold()
        for line in self.lines:
            if "=" not in line:
                continue
            candidate, value = line.split("=", 1)
            if candidate.strip().casefold() == prefix:
                return value.strip()
        return None


@dataclass(frozen=True)
class DesiredPeer:
    device_id: str
    public_key: str
    allowed_ips: tuple[str, ...]


Runner = Callable[..., subprocess.CompletedProcess]


def parse_wg_config(payload: str) -> list[Section]:
    sections: list[Section] = []
    name: str | None = None
    lines: list[str] = []
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            if name is not None:
                sections.append(Section(name, tuple(lines)))
            name = line[1:-1]
            lines = []
        elif name is not None and line:
            lines.append(line)
    if name is not None:
        sections.append(Section(name, tuple(lines)))
    if not sections or sections[0].name != "Interface":
        raise ReconcileError("live WireGuard configuration has no Interface section")
    if sum(section.name == "Interface" for section in sections) != 1:
        raise ReconcileError("live WireGuard configuration has multiple Interface sections")
    for section in sections[1:]:
        if section.name != "Peer" or not section.value("PublicKey"):
            raise ReconcileError("live WireGuard configuration contains an invalid section")
    return sections


def render_wg_config(sections: list[Section]) -> bytes:
    output: list[str] = []
    for section in sections:
        output.append(f"[{section.name}]")
        output.extend(section.lines)
        output.append("")
    return ("\n".join(output).rstrip() + "\n").encode("utf-8")


def load_expected_state(path: Path, interface: str) -> tuple[list[DesiredPeer], str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReconcileError("unable to open expected peer state safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ReconcileError("expected peer state must be a regular file")
        if metadata.st_mode & 0o022:
            raise ReconcileError("expected peer state must not be group/world writable")
        if metadata.st_size <= 0 or metadata.st_size > MAX_EXPECTED_BYTES:
            raise ReconcileError("expected peer state has an invalid size")
        raw = bytearray()
        while len(raw) <= MAX_EXPECTED_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_EXPECTED_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if not raw or len(raw) > MAX_EXPECTED_BYTES:
            raise ReconcileError("expected peer state has an invalid size")
        raw = bytes(raw)
    finally:
        os.close(descriptor)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReconcileError("expected peer state is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ReconcileError("expected peer state must be a JSON object")
    if payload.get("format") != 1 or payload.get("interface") != interface:
        raise ReconcileError("expected peer state format/interface mismatch")
    raw_peers = payload.get("peers")
    if not isinstance(raw_peers, list) or len(raw_peers) > MAX_PEERS:
        raise ReconcileError("expected peer collection is invalid")

    peers: list[DesiredPeer] = []
    keys: set[str] = set()
    networks: set[ipaddress.IPv4Network] = set()
    for raw_peer in raw_peers:
        if not isinstance(raw_peer, dict):
            raise ReconcileError("expected peer entry is invalid")
        public_key = str(raw_peer.get("public_key", ""))
        _validate_public_key(public_key)
        if public_key in keys:
            raise ReconcileError("duplicate desired public key")
        keys.add(public_key)
        raw_allowed = raw_peer.get("allowed_ips")
        if not isinstance(raw_allowed, list) or len(raw_allowed) != 1:
            raise ReconcileError("each endpoint device must have exactly one server AllowedIPs entry")
        try:
            network = ipaddress.ip_network(str(raw_allowed[0]), strict=True)
        except ValueError as error:
            raise ReconcileError("invalid desired server AllowedIPs") from error
        if network.version != 4 or network.prefixlen != 32:
            raise ReconcileError("endpoint device server AllowedIPs must be a unique IPv4 /32")
        if network in networks:
            raise ReconcileError("duplicate desired tunnel address")
        networks.add(network)
        device_id = str(raw_peer.get("device_id", ""))
        if not device_id or len(device_id) > 128:
            raise ReconcileError("invalid desired device id")
        peers.append(DesiredPeer(device_id, public_key, (str(network),)))
    return peers, digest


def merge_peer_state(
    current: list[Section],
    desired: list[DesiredPeer],
    owned_keys: set[str],
    *,
    allow_initial_adoption: bool = False,
) -> tuple[list[Section], set[str]]:
    interface = current[0]
    current_peers = current[1:]
    current_by_key = {section.value("PublicKey"): section for section in current_peers}
    if len(current_by_key) != len(current_peers):
        raise ReconcileError("live WireGuard configuration has duplicate public keys")
    desired_by_key = {peer.public_key: peer for peer in desired}

    unmanaged = {
        key: section
        for key, section in current_by_key.items()
        if key not in owned_keys and key not in desired_by_key
    }
    unmanaged_networks: list[
        tuple[str, ipaddress.IPv4Network | ipaddress.IPv6Network]
    ] = []
    for key, section in unmanaged.items():
        for network in _section_allowed_networks(section):
            unmanaged_networks.append((key, network))

    for peer in desired:
        desired_network = ipaddress.ip_network(peer.allowed_ips[0])
        for _key, unmanaged_network in unmanaged_networks:
            if desired_network.overlaps(unmanaged_network):
                raise ReconcileError(
                    "desired tunnel IP overlaps an unmanaged live peer; import/reserve it first"
                )
        existing = current_by_key.get(peer.public_key)
        if existing is not None and peer.public_key not in owned_keys:
            routes_match = set(_section_allowed_ips(existing)) == set(peer.allowed_ips)
            if not allow_initial_adoption or not routes_match:
                raise ReconcileError(
                    "desired public key already exists as an unmanaged peer; explicit initial adoption is required"
                )

    result: list[Section] = [interface]
    emitted: set[str] = set()
    for section in current_peers:
        key = section.value("PublicKey")
        if key in owned_keys and key not in desired_by_key:
            continue
        desired_peer = desired_by_key.get(key)
        if desired_peer is None:
            result.append(section)
        else:
            result.append(_replace_allowed_ips(section, desired_peer.allowed_ips))
            emitted.add(key)

    for peer in desired:
        if peer.public_key in emitted:
            continue
        result.append(
            Section(
                "Peer",
                (
                    f"PublicKey = {peer.public_key}",
                    f"AllowedIPs = {', '.join(peer.allowed_ips)}",
                ),
            )
        )
    return result, set(desired_by_key)


def apply_expected_state(
    *,
    expected_path: Path,
    interface: str,
    manifest_path: Path,
    status_path: Path,
    runtime_dir: Path,
    wg_binary: str = "/usr/bin/wg",
    runner: Runner = subprocess.run,
    requested_sha256: str | None = None,
    request_id: str | None = None,
) -> dict:
    desired, digest = load_expected_state(expected_path, interface)
    if requested_sha256 is not None and requested_sha256 != digest:
        raise ReconcileError("requested revision no longer matches expected state")
    manifest_exists = manifest_path.exists()
    owned_keys = _load_owned_keys(manifest_path, interface)
    current_result = runner(
        [wg_binary, "showconf", interface], capture_output=True, text=True, check=False
    )
    if current_result.returncode != 0:
        raise ReconcileError("unable to read live WireGuard configuration")
    current = parse_wg_config(current_result.stdout)
    candidate, new_owned_keys = merge_peer_state(
        current,
        desired,
        owned_keys,
        allow_initial_adoption=not manifest_exists,
    )
    previous_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    previous_status = status_path.read_bytes() if status_path.exists() else None

    runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    rollback_path = _write_private_temp(runtime_dir, "rollback-", render_wg_config(current))
    candidate_path = _write_private_temp(runtime_dir, "candidate-", render_wg_config(candidate))
    try:
        result = runner(
            [wg_binary, "syncconf", interface, str(candidate_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            if result.returncode != 0:
                raise ReconcileError("wg syncconf rejected the candidate configuration")
            _verify_live_state(
                runner=runner,
                wg_binary=wg_binary,
                interface=interface,
                candidate=candidate,
                desired=desired,
                removed_owned=owned_keys - new_owned_keys,
            )
        except ReconcileError:
            _rollback_live(
                runner=runner,
                wg_binary=wg_binary,
                interface=interface,
                rollback_path=rollback_path,
            )
            raise
        manifest = {
            "format": 1,
            "interface": interface,
            "desired_sha256": digest,
            "owned_public_keys": sorted(new_owned_keys),
        }
        status = {
            "format": 1,
            "status": "applied",
            "interface": interface,
            "desired_sha256": digest,
            "peer_count": len(desired),
            "request_id": request_id,
        }
        try:
            _atomic_json_write(manifest_path, manifest, mode=0o640)
            _atomic_json_write(status_path, status, mode=0o640)
        except OSError as error:
            try:
                _rollback_live(
                    runner=runner,
                    wg_binary=wg_binary,
                    interface=interface,
                    rollback_path=rollback_path,
                )
            finally:
                _restore_file(manifest_path, previous_manifest, mode=0o640)
                _restore_file(status_path, previous_status, mode=0o640)
            raise ReconcileError("unable to persist verified reconcile state; live state rolled back") from error
        return status
    finally:
        candidate_path.unlink(missing_ok=True)
        rollback_path.unlink(missing_ok=True)


def serve(
    *,
    socket_path: Path,
    expected_path: Path,
    interface: str,
    manifest_path: Path,
    status_path: Path,
    runtime_dir: Path,
    wg_binary: str,
) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    socket_path.parent.chmod(0o750)
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.lstat().st_mode):
            raise ReconcileError("refusing to replace a non-socket reconcile path")
        socket_path.unlink()
    previous_umask = os.umask(0o007)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o660)
        server.listen(32)
    finally:
        os.umask(previous_umask)
    try:
        while True:
            connection, _address = server.accept()
            with connection:
                response = _handle_request(
                    connection,
                    expected_path=expected_path,
                    interface=interface,
                    manifest_path=manifest_path,
                    status_path=status_path,
                    runtime_dir=runtime_dir,
                    wg_binary=wg_binary,
                )
                try:
                    connection.sendall(
                        json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n"
                    )
                except OSError:
                    pass
    finally:
        server.close()
        socket_path.unlink(missing_ok=True)


def _handle_request(connection: socket.socket, **apply_kwargs) -> dict:
    try:
        raw = bytearray()
        while not raw.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > 16384:
                raise ReconcileError("request is too large")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ReconcileError("request must be a JSON object")
        if request.get("action") != "apply":
            raise ReconcileError("unsupported reconciler action")
        digest = str(request.get("desired_sha256", ""))
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ReconcileError("invalid desired revision")
        request_id = str(request.get("request_id", ""))
        if len(request_id) != 32 or any(
            character not in "0123456789abcdef" for character in request_id
        ):
            raise ReconcileError("invalid request id")
        return apply_expected_state(
            requested_sha256=digest, request_id=request_id, **apply_kwargs
        )
    except (OSError, ValueError, json.JSONDecodeError, ReconcileError) as error:
        return {"status": "failed", "message": str(error)[:300]}


def _verify_live_state(
    *,
    runner: Runner,
    wg_binary: str,
    interface: str,
    candidate: list[Section],
    desired: list[DesiredPeer],
    removed_owned: set[str],
) -> None:
    result = runner(
        [wg_binary, "show", interface, "allowed-ips"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ReconcileError("unable to verify live WireGuard peers")
    live: dict[str, tuple[str, ...]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2:
            raise ReconcileError("unexpected live AllowedIPs output")
        live[fields[0]] = tuple(part.strip() for part in fields[1].split(",") if part.strip())
    candidate_keys = {
        section.value("PublicKey") for section in candidate if section.name == "Peer"
    }
    if set(live) != candidate_keys:
        raise ReconcileError("live peer set does not match candidate configuration")
    for peer in desired:
        if set(live.get(peer.public_key, ())) != set(peer.allowed_ips):
            raise ReconcileError("live peer AllowedIPs verification failed")
    if removed_owned & set(live):
        raise ReconcileError("revoked public key remains active")


def _replace_allowed_ips(section: Section, allowed_ips: tuple[str, ...]) -> Section:
    lines = [line for line in section.lines if _line_key(line) != "allowedips"]
    lines.append(f"AllowedIPs = {', '.join(allowed_ips)}")
    return Section(section.name, tuple(lines))


def _section_allowed_ips(section: Section) -> tuple[str, ...]:
    value = section.value("AllowedIPs") or ""
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _section_allowed_networks(
    section: Section,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    try:
        return tuple(ipaddress.ip_network(item, strict=True) for item in _section_allowed_ips(section))
    except ValueError as error:
        raise ReconcileError("live peer has invalid AllowedIPs") from error


def _line_key(line: str) -> str:
    return line.split("=", 1)[0].strip().casefold() if "=" in line else ""


def _rollback_live(
    *,
    runner: Runner,
    wg_binary: str,
    interface: str,
    rollback_path: Path,
) -> None:
    rollback = runner(
        [wg_binary, "syncconf", interface, str(rollback_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if rollback.returncode != 0:
        raise ReconcileError("live apply failed and rollback also failed")


def _validate_public_key(value: str) -> None:
    try:
        raw = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise ReconcileError("invalid peer public key") from error
    if len(raw) != 32:
        raise ReconcileError("invalid peer public key length")


def _load_owned_keys(path: Path, interface: str) -> set[str]:
    if not path.exists():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReconcileError("managed-peer manifest is unreadable") from error
    if not isinstance(payload, dict):
        raise ReconcileError("managed-peer manifest is invalid")
    if payload.get("format") != 1 or payload.get("interface") != interface:
        raise ReconcileError("managed-peer manifest does not match the interface")
    values = payload.get("owned_public_keys")
    if not isinstance(values, list) or len(values) > MAX_PEERS:
        raise ReconcileError("managed-peer manifest is invalid")
    result = set()
    for value in values:
        key = str(value)
        _validate_public_key(key)
        result.add(key)
    return result


def _write_private_temp(directory: Path, prefix: str, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=directory)
    path = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        raise
    return path


def _atomic_json_write(path: Path, payload: dict, *, mode: int) -> None:
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _atomic_bytes_write(path, raw, mode=mode)


def _atomic_bytes_write(path: Path, raw: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
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
        temporary.unlink(missing_ok=True)
        raise


def _restore_file(path: Path, previous: bytes | None, *, mode: int) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        _atomic_bytes_write(path, previous, mode=mode)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wg-manager-reconciler",
        description="Least-privilege live WireGuard peer reconciler",
    )
    parser.add_argument("command", choices=("serve", "once"), nargs="?", default="serve")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    data_dir = Path(os.environ.get("WG_MANAGER_DATA_DIR", "/var/lib/wireguard-manager"))
    state_dir = Path(
        os.environ.get(
            "WG_RECONCILE_STATE_DIR", "/var/lib/wireguard-manager-reconciler"
        )
    )
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o750)
    state_dir.chmod(0o750)
    interface = os.environ.get("WG_INTERFACE", "wg0")
    runtime_dir = Path(os.environ.get("WG_RECONCILE_RUNTIME_DIR", "/run/wireguard-manager"))
    kwargs = {
        "expected_path": data_dir / "expected-peers.json",
        "interface": interface,
        "manifest_path": state_dir / "reconciler-owned.json",
        "status_path": state_dir / "reconcile-status.json",
        "runtime_dir": runtime_dir,
        "wg_binary": os.environ.get("WG_RECONCILER_WG_BINARY", "/usr/bin/wg"),
    }
    if args.command == "once":
        status = apply_expected_state(**kwargs)
        print(
            f"VERIFIED: desired state applied; sha256={status['desired_sha256']} peers={status['peer_count']}"
        )
        return
    serve(socket_path=Path(os.environ.get("WG_RECONCILE_SOCKET", runtime_dir / "reconcile.sock")), **kwargs)


if __name__ == "__main__":
    main()
