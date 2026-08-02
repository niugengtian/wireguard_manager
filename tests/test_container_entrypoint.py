from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest


ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.py"


def load_entrypoint():
    spec = importlib.util.spec_from_file_location("wg_manager_container_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_container_public_key_validation():
    entrypoint = load_entrypoint()
    valid = base64.b64encode(bytes(range(32))).decode("ascii")
    assert entrypoint._validate_public_key(f" {valid}\n") == valid
    with pytest.raises(RuntimeError, match="base64"):
        entrypoint._validate_public_key("not-a-key")
    with pytest.raises(RuntimeError, match="32 bytes"):
        entrypoint._validate_public_key(base64.b64encode(b"short").decode("ascii"))


def test_container_managed_directory_boundary():
    entrypoint = load_entrypoint()
    assert entrypoint._safe_managed_directory(
        Path("/run/wireguard-manager")
    ) == Path("/run/wireguard-manager").resolve()
    assert entrypoint._safe_managed_directory(
        Path("/var/lib/wireguard-manager")
    ) == Path("/var/lib/wireguard-manager").resolve()
    for unsafe in (Path("/"), Path("/run"), Path("/var"), Path("/var/lib")):
        with pytest.raises(RuntimeError, match="unsafe"):
            entrypoint._safe_managed_directory(unsafe)


def test_container_rejects_placeholder_endpoint(monkeypatch):
    entrypoint = load_entrypoint()
    monkeypatch.setenv("WG_ENDPOINT", "vpn.example.invalid:51820")
    with pytest.raises(RuntimeError, match="WG_ENDPOINT"):
        entrypoint._validate_endpoint()
    monkeypatch.setenv("WG_ENDPOINT", "vpn.example.net:51820")
    entrypoint._validate_endpoint()
