from __future__ import annotations

import secrets

import pytest

from wg_manager import create_app
from wg_manager.db import get_db
from wg_manager.services import create_user


@pytest.fixture
def app(tmp_path):
    application = create_app(
        {
            "TESTING": True,
            "DATA_DIR": str(tmp_path / "data"),
            "DATABASE": str(tmp_path / "data" / "manager.sqlite3"),
            "INSTALLER_DIR": str(tmp_path / "data" / "installers"),
            "EXPECTED_PEERS_FILE": str(tmp_path / "data" / "expected-peers.json"),
            "SESSION_COOKIE_SECURE": False,
            "LOGIN_ATTEMPT_LIMIT": 3,
            "LOGIN_WINDOW_SECONDS": 60,
            "WG_RESET_ACTIVATION_DELAY_SECONDS": 0,
        }
    )
    application.runtime_admin_password = secrets.token_urlsafe(18)
    with application.app_context():
        create_user(
            get_db(),
            username="runtime-admin",
            password=application.runtime_admin_password,
            quota=10,
            role="admin",
            actor_kind="system",
        )
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client) -> str:
    with client.session_transaction() as session:
        return session["csrf"]


def login(client, username: str, password: str):
    client.get("/login")
    return client.post(
        "/login",
        data={"_csrf": csrf(client), "username": username, "password": password},
    )


def logout(client):
    return client.post("/logout", data={"_csrf": csrf(client)})
