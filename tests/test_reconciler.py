from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import wg_manager.reconciler as reconciler_module
from wg_manager.reconciler import (
    ReconcileError,
    Section,
    apply_expected_state,
    parse_wg_config,
    render_wg_config,
)


def _public_key(index: int) -> str:
    return base64.b64encode(hashlib.sha256(f"peer-{index}".encode()).digest()).decode()


def _config(peers: list[tuple[str, str]], *, endpoint: bool = False) -> str:
    sections = [Section("Interface", ("PrivateKey = TEST_ONLY_NOT_A_REAL_KEY", "ListenPort = 51820"))]
    for key, allowed_ip in peers:
        lines = [f"PublicKey = {key}", f"AllowedIPs = {allowed_ip}"]
        if endpoint:
            lines.append("Endpoint = 192.0.2.10:51820")
        sections.append(Section("Peer", tuple(lines)))
    return render_wg_config(sections).decode()


def _write_expected(path: Path, peers: list[tuple[str, str]], interface: str = "wg0") -> str:
    payload = {
        "format": 1,
        "interface": interface,
        "peers": [
            {"device_id": f"device-{index}", "public_key": key, "allowed_ips": [allowed_ip]}
            for index, (key, allowed_ip) in enumerate(peers)
        ],
    }
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return hashlib.sha256(raw).hexdigest()


class FakeWg:
    def __init__(self, live_config: str):
        self.live_config = live_config
        self.commands: list[list[str]] = []
        self.corrupt_next_verification = False
        self.reject_next_candidate = False

    def __call__(self, command, **_kwargs):
        command = [str(item) for item in command]
        self.commands.append(command)
        if command[1:3] == ["showconf", "wg0"]:
            return subprocess.CompletedProcess(command, 0, self.live_config, "")
        if command[1:4] == ["show", "wg0", "allowed-ips"]:
            if self.corrupt_next_verification:
                self.corrupt_next_verification = False
                return subprocess.CompletedProcess(command, 0, "", "")
            lines = []
            for section in parse_wg_config(self.live_config)[1:]:
                lines.append(f"{section.value('PublicKey')}\t{section.value('AllowedIPs')}")
            return subprocess.CompletedProcess(command, 0, "\n".join(lines) + ("\n" if lines else ""), "")
        if command[1:3] == ["syncconf", "wg0"]:
            source = Path(command[3])
            if source.name.startswith("candidate-") and self.reject_next_candidate:
                self.reject_next_candidate = False
                return subprocess.CompletedProcess(command, 1, "", "rejected")
            self.live_config = source.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command}")


def _apply(tmp_path: Path, fake: FakeWg, expected: Path, digest: str | None = None):
    return apply_expected_state(
        expected_path=expected,
        interface="wg0",
        manifest_path=tmp_path / "reconciler-owned.json",
        status_path=tmp_path / "reconcile-status.json",
        runtime_dir=tmp_path / "runtime",
        wg_binary="/usr/bin/wg",
        runner=fake,
        requested_sha256=digest,
    )


def test_reconciler_hot_applies_many_peers_and_preserves_unmanaged_peer(tmp_path):
    unmanaged = (_public_key(9000), "10.77.255.250/32")
    fake = FakeWg(_config([unmanaged], endpoint=True))
    managed = [(_public_key(index), f"10.77.{index // 250}.{index % 250 + 2}/32") for index in range(300)]
    expected = tmp_path / "expected-peers.json"
    digest = _write_expected(expected, managed)

    status = _apply(tmp_path, fake, expected, digest)

    sections = parse_wg_config(fake.live_config)
    live = {section.value("PublicKey"): section for section in sections[1:]}
    assert status == {
        "format": 1,
        "status": "applied",
        "interface": "wg0",
        "desired_sha256": digest,
        "peer_count": 300,
        "request_id": None,
    }
    assert len(live) == 301
    assert unmanaged[0] in live
    assert live[unmanaged[0]].value("Endpoint") == "192.0.2.10:51820"
    assert {key for key, _allowed_ip in managed} <= set(live)
    assert any(command[1:3] == ["syncconf", "wg0"] for command in fake.commands)
    assert all("restart" not in command and "wg-quick" not in command for command in fake.commands)


def test_reset_and_delete_revoke_only_manager_owned_public_keys(tmp_path):
    unmanaged = (_public_key(9000), "10.77.255.250/32")
    old = [(_public_key(1), "10.77.0.2/32"), (_public_key(2), "10.77.0.3/32")]
    fake = FakeWg(_config([unmanaged], endpoint=True))
    expected = tmp_path / "expected-peers.json"
    _apply(tmp_path, fake, expected, _write_expected(expected, old))

    replacement = (_public_key(3), "10.77.0.2/32")
    _apply(tmp_path, fake, expected, _write_expected(expected, [replacement]))

    live = {
        section.value("PublicKey"): section.value("AllowedIPs")
        for section in parse_wg_config(fake.live_config)[1:]
    }
    assert live == {unmanaged[0]: unmanaged[1], replacement[0]: replacement[1]}
    manifest = json.loads((tmp_path / "reconciler-owned.json").read_text())
    assert manifest["owned_public_keys"] == [replacement[0]]


@pytest.mark.parametrize("failure", ["apply", "verify"])
def test_failed_live_apply_rolls_back(tmp_path, failure):
    original = _config([(_public_key(9000), "10.77.255.250/32")], endpoint=True)
    fake = FakeWg(original)
    if failure == "apply":
        fake.reject_next_candidate = True
    else:
        fake.corrupt_next_verification = True
    expected = tmp_path / "expected-peers.json"
    digest = _write_expected(expected, [(_public_key(1), "10.77.0.2/32")])

    with pytest.raises(ReconcileError):
        _apply(tmp_path, fake, expected, digest)

    assert fake.live_config == original
    sync_commands = [command for command in fake.commands if command[1:3] == ["syncconf", "wg0"]]
    assert len(sync_commands) == 2
    assert Path(sync_commands[-1][3]).name.startswith("rollback-")


def test_unmanaged_allowed_ip_overlap_fails_closed_without_live_change(tmp_path):
    original = _config([(_public_key(9000), "10.77.0.2/32")], endpoint=True)
    fake = FakeWg(original)
    expected = tmp_path / "expected-peers.json"
    digest = _write_expected(expected, [(_public_key(1), "10.77.0.2/32")])

    with pytest.raises(ReconcileError, match="overlaps an unmanaged live peer"):
        _apply(tmp_path, fake, expected, digest)

    assert fake.live_config == original
    assert not any(command[1:3] == ["syncconf", "wg0"] for command in fake.commands)


def test_metadata_persistence_failure_rolls_live_state_back(monkeypatch, tmp_path):
    original = _config([(_public_key(9000), "10.77.255.250/32")], endpoint=True)
    fake = FakeWg(original)
    expected = tmp_path / "expected-peers.json"
    digest = _write_expected(expected, [(_public_key(1), "10.77.0.2/32")])
    real_write = reconciler_module._atomic_json_write
    writes = 0

    def fail_status(path, payload, *, mode):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated status write failure")
        return real_write(path, payload, mode=mode)

    monkeypatch.setattr(reconciler_module, "_atomic_json_write", fail_status)

    with pytest.raises(ReconcileError, match="live state rolled back"):
        _apply(tmp_path, fake, expected, digest)

    assert fake.live_config == original
    assert not (tmp_path / "reconciler-owned.json").exists()
    assert not (tmp_path / "reconcile-status.json").exists()
