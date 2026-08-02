from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from wg_manager import create_app
from wg_manager.db import get_db
from wg_manager.services import create_device, create_user

from conftest import csrf, login, logout


def _device_public_key(configuration: str) -> str:
    private_line = next(line for line in configuration.splitlines() if line.startswith("PrivateKey = "))
    raw = base64.b64decode(private_line.removeprefix("PrivateKey = "))
    private_key = X25519PrivateKey.from_private_bytes(raw)
    public_raw = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(public_raw).decode("ascii")


def _address(configuration: str) -> str:
    line = next(line for line in configuration.splitlines() if line.startswith("Address = "))
    return line.removeprefix("Address = ").split("/", 1)[0]


def test_full_business_acceptance_and_no_secret_persistence(app, client, caplog):
    user_password = secrets.token_urlsafe(18)
    assert login(client, "runtime-admin", app.runtime_admin_password).status_code == 302
    response = client.post(
        "/admin/users",
        data={"_csrf": csrf(client), "username": "quota-user", "password": user_password, "quota": "2"},
    )
    assert response.status_code == 302
    logout(client)

    assert login(client, "quota-user", user_password).status_code == 302
    device_page = client.get("/devices")
    assert device_page.headers["Cache-Control"].startswith("no-store")
    first = client.post(
        "/devices",
        data={"_csrf": csrf(client), "name": "first", "client_type": "windows", "delivery": "download"},
    )
    assert first.status_code == 200
    assert first.headers["Cache-Control"].startswith("no-store")
    first_config = first.get_data(as_text=True)
    assert "AllowedIPs = 0.0.0.0/0" in first_config
    assert "::/0" not in first_config
    assert "\nDNS = " not in first_config
    first_ip = _address(first_config)
    first_public = _device_public_key(first_config)

    second = client.post(
        "/devices",
        data={"_csrf": csrf(client), "name": "second", "client_type": "ios", "delivery": "download"},
    )
    assert second.status_code == 200
    second_config = second.get_data(as_text=True)
    second_ip = _address(second_config)
    assert second_ip != first_ip

    third = client.post(
        "/devices",
        data={"_csrf": csrf(client), "name": "third", "client_type": "linux", "delivery": "download"},
    )
    assert third.status_code == 409
    assert b"device quota exceeded" in third.data

    with app.app_context():
        connection = get_db()
        first_row = connection.execute("SELECT * FROM devices WHERE name = 'first'").fetchone()
        second_row = connection.execute("SELECT * FROM devices WHERE name = 'second'").fetchone()
        assert first_row["public_key"] == first_public
        assert "PrivateKey" not in {column for column in first_row.keys()}
        first_id = first_row["id"]
        second_id = second_row["id"]

    reset = client.post(
        f"/devices/{first_id}/reset",
        data={"_csrf": csrf(client), "delivery": "download"},
    )
    assert reset.status_code == 200
    reset_config = reset.get_data(as_text=True)
    reset_public = _device_public_key(reset_config)
    assert reset_public != first_public
    assert _address(reset_config) == first_ip
    state = json.loads(open(app.config["EXPECTED_PEERS_FILE"], encoding="utf-8").read())
    peer_keys = {peer["public_key"] for peer in state["peers"]}
    assert first_public not in peer_keys
    assert reset_public in peer_keys

    deleted = client.post(f"/devices/{second_id}/delete", data={"_csrf": csrf(client)})
    assert deleted.status_code == 302
    state = json.loads(open(app.config["EXPECTED_PEERS_FILE"], encoding="utf-8").read())
    assert second_id not in {peer["device_id"] for peer in state["peers"]}

    replacement = client.post(
        "/devices",
        data={"_csrf": csrf(client), "name": "replacement", "client_type": "android", "delivery": "download"},
    )
    assert replacement.status_code == 200
    assert _address(replacement.get_data(as_text=True)) == second_ip

    private_value = next(
        line.removeprefix("PrivateKey = ")
        for line in reset_config.splitlines()
        if line.startswith("PrivateKey = ")
    )
    for path in (app.config["DATA_DIR"],):
        for candidate in __import__("pathlib").Path(path).rglob("*"):
            if candidate.is_file():
                payload = candidate.read_bytes()
                assert user_password.encode() not in payload
                assert private_value.encode() not in payload
                assert reset_config.encode() not in payload
    assert user_password not in caplog.text
    assert private_value not in caplog.text
    assert reset_config not in caplog.text


def test_concurrent_device_creation_gets_unique_ips(app):
    password = secrets.token_urlsafe(18)
    with app.app_context():
        user = create_user(
            get_db(), username="concurrent", password=password, quota=10, role="user", actor_kind="system"
        )
        user_id = user["id"]

    def create(name):
        with app.app_context():
            device, _configuration = create_device(
                get_db(),
                app.config,
                user_id=user_id,
                name=name,
                client_type="linux",
                actor_user_id=user_id,
                actor_kind="web",
            )
            return device["static_ip"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        addresses = list(executor.map(create, ("parallel-a", "parallel-b")))
    assert len(addresses) == len(set(addresses)) == 2


def test_reserved_existing_peer_ip_is_not_allocated(tmp_path):
    data_dir = tmp_path / "reserved-data"
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(data_dir / "manager.sqlite3"),
            "INSTALLER_DIR": str(data_dir / "installers"),
            "EXPECTED_PEERS_FILE": str(data_dir / "expected-peers.json"),
            "WG_RESERVED_IPS": "10.44.0.2, 10.44.0.3",
        }
    )
    with application.app_context():
        user = create_user(
            get_db(), username="reserved-user", password=secrets.token_urlsafe(18), quota=1, actor_kind="system"
        )
        device, _configuration = create_device(
            get_db(),
            application.config,
            user_id=user["id"],
            name="new-peer",
            client_type="linux",
            actor_user_id=None,
            actor_kind="system",
        )
    assert device["static_ip"] == "10.44.0.4"


def test_admin_edits_per_device_client_routes_and_reset_delivers_policy(app, client):
    password = secrets.token_urlsafe(18)
    with app.app_context():
        user = create_user(
            get_db(), username="route-user", password=password, quota=2, actor_kind="system"
        )
        device, _configuration = create_device(
            get_db(),
            app.config,
            user_id=user["id"],
            name="scoped-laptop",
            client_type="linux",
            actor_user_id=user["id"],
            actor_kind="web",
        )

    login(client, "route-user", password)
    denied = client.post(
        f"/admin/devices/{device['id']}/allowed-ips",
        data={"_csrf": csrf(client), "client_allowed_ips": "10.0.0.0/8"},
    )
    assert denied.status_code == 403
    logout(client)

    login(client, "runtime-admin", app.runtime_admin_password)
    updated = client.post(
        f"/admin/devices/{device['id']}/allowed-ips",
        data={
            "_csrf": csrf(client),
            "client_allowed_ips": "172.31.0.0/16, 10.0.0.0/8",
        },
    )
    assert updated.status_code == 302
    admin_page = client.get("/admin")
    assert b"10.0.0.0/8, 172.31.0.0/16" in admin_page.data
    assert b"Reset required" in admin_page.data
    with app.app_context():
        row = get_db().execute("SELECT * FROM devices WHERE id = ?", (device["id"],)).fetchone()
        assert row["policy_revision"] == 2
        assert row["delivered_policy_revision"] == 1
    logout(client)

    login(client, "route-user", password)
    reset = client.post(
        f"/devices/{device['id']}/reset",
        data={"_csrf": csrf(client), "delivery": "download"},
    )
    assert reset.status_code == 200
    assert "AllowedIPs = 10.0.0.0/8, 172.31.0.0/16" in reset.get_data(as_text=True)
    with app.app_context():
        row = get_db().execute("SELECT * FROM devices WHERE id = ?", (device["id"],)).fetchone()
        assert row["policy_revision"] == row["delivered_policy_revision"] == 2


def test_web_device_lifecycle_waits_for_live_reconciler(monkeypatch, tmp_path):
    live_revisions = []
    monkeypatch.setattr(
        "wg_manager.adapter._request_live_reconcile",
        lambda _socket, digest, _request_id, _timeout: live_revisions.append(digest),
    )
    data_dir = tmp_path / "live-data"
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(data_dir),
            "DATABASE": str(data_dir / "manager.sqlite3"),
            "INSTALLER_DIR": str(data_dir / "installers"),
            "EXPECTED_PEERS_FILE": str(data_dir / "expected-peers.json"),
            "RECONCILE_STATUS_FILE": str(data_dir / "reconcile-status.json"),
            "SESSION_COOKIE_SECURE": False,
            "WG_ADAPTER": "reconciler",
        }
    )
    password = secrets.token_urlsafe(18)
    with application.app_context():
        create_user(
            get_db(), username="live-user", password=password, quota=2, actor_kind="system"
        )
    browser = application.test_client()
    assert login(browser, "live-user", password).status_code == 302
    created = browser.post(
        "/devices",
        data={
            "_csrf": csrf(browser),
            "name": "live-laptop",
            "client_type": "linux",
            "delivery": "download",
        },
    )
    assert created.status_code == 200
    with application.app_context():
        device_id = get_db().execute("SELECT id FROM devices").fetchone()["id"]
    reset = browser.post(
        f"/devices/{device_id}/reset",
        data={"_csrf": csrf(browser), "delivery": "download"},
    )
    assert reset.status_code == 200
    deleted = browser.post(f"/devices/{device_id}/delete", data={"_csrf": csrf(browser)})
    assert deleted.status_code == 302
    assert len(live_revisions) == 3
    assert len(set(live_revisions)) == 3


def test_object_authorization_rbac_installer_integrity_and_qr_no_store(app, client):
    first_password = secrets.token_urlsafe(18)
    second_password = secrets.token_urlsafe(18)
    with app.app_context():
        first_user = create_user(
            get_db(), username="first-user", password=first_password, quota=3, actor_kind="system"
        )
        second_user = create_user(
            get_db(), username="second-user", password=second_password, quota=3, actor_kind="system"
        )
        other_device, _ = create_device(
            get_db(),
            app.config,
            user_id=second_user["id"],
            name="other-device",
            client_type="macos",
            actor_user_id=second_user["id"],
            actor_kind="web",
        )

    login(client, "first-user", first_password)
    denied_object = client.post(
        f"/devices/{other_device['id']}/reset",
        data={"_csrf": csrf(client), "delivery": "download"},
    )
    assert denied_object.status_code == 404
    denied_upload = client.post(
        "/admin/installers",
        data={"_csrf": csrf(client)},
    )
    assert denied_upload.status_code == 403

    qr = client.post(
        "/devices",
        data={"_csrf": csrf(client), "name": "phone", "client_type": "android", "delivery": "qr"},
    )
    assert qr.status_code == 200
    assert qr.headers["Cache-Control"].startswith("no-store")
    assert b"data:image/png;base64," in qr.data
    logout(client)

    login(client, "runtime-admin", app.runtime_admin_password)
    artifact = secrets.token_bytes(4096)
    uploaded = client.post(
        "/admin/installers",
        data={
            "_csrf": csrf(client),
            "platform": "windows",
            "architecture": "x86_64",
            "version": "runtime-version",
            "license_name": "verified-runtime-terms",
            "license_source_url": "https://www.wireguard.com/install/",
            "redistribution_confirmed": "1",
            "installer": (io.BytesIO(artifact), "runtime-installer.msi"),
        },
        content_type="multipart/form-data",
    )
    assert uploaded.status_code == 302
    with app.app_context():
        installer = get_db().execute("SELECT * FROM installers").fetchone()
        assert installer["sha256"] == hashlib.sha256(artifact).hexdigest()
        installer_id = installer["id"]
    logout(client)

    login(client, "first-user", first_password)
    downloaded = client.get(f"/installers/{installer_id}/download")
    assert downloaded.status_code == 200
    assert downloaded.data == artifact
    assert hashlib.sha256(downloaded.data).hexdigest() == installer["sha256"]
    assert downloaded.headers["Cache-Control"].startswith("no-store")


def test_csrf_and_login_rate_limit(app, client):
    help_page = client.get("/help")
    assert b"\xe5\xb8\xae\xe5\x8a\xa9" in help_page.data
    assert b"User quick start" in help_page.data
    assert client.post("/login", data={"username": "runtime-admin"}).status_code == 400
    client.get("/login")
    for _ in range(3):
        response = client.post(
            "/login",
            data={"_csrf": csrf(client), "username": "runtime-admin", "password": secrets.token_urlsafe(12)},
        )
        assert response.status_code == 401
    limited = client.post(
        "/login",
        data={"_csrf": csrf(client), "username": "runtime-admin", "password": secrets.token_urlsafe(12)},
    )
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
