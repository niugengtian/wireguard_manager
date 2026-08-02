from __future__ import annotations

import ipaddress
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, request, session

from .db import close_db, get_db, initialize
from .policy import normalize_client_allowed_ips
from .security import load_or_create_secret


def create_app(test_config: dict | None = None) -> Flask:
    testing = bool(test_config and test_config.get("TESTING"))
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not testing:
        raise RuntimeError("wireguard-manager web and CLI processes must not run as root")

    project_root = Path(__file__).resolve().parent.parent
    data_dir = Path(
        os.environ.get(
            "WG_MANAGER_DATA_DIR",
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
            / "wireguard-manager",
        )
    ).expanduser()
    if test_config and "DATA_DIR" in test_config:
        data_dir = Path(test_config["DATA_DIR"])
    data_dir = data_dir.resolve()
    if not testing and _is_within(data_dir, project_root):
        raise RuntimeError("WG_MANAGER_DATA_DIR must be outside the source directory")
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    data_dir.chmod(0o700)
    reconcile_state_dir = Path(
        os.environ.get(
            "WG_RECONCILE_STATE_DIR",
            data_dir if testing else "/var/lib/wireguard-manager-reconciler",
        )
    ).expanduser()

    tunnel_cidr = os.environ.get("WG_TUNNEL_CIDR", "10.44.0.0/24")
    app = Flask(__name__)
    app.config.from_mapping(
        DATA_DIR=str(data_dir),
        DATABASE=str(data_dir / "manager.sqlite3"),
        INSTALLER_DIR=str(data_dir / "installers"),
        EXPECTED_PEERS_FILE=str(data_dir / "expected-peers.json"),
        RECONCILE_STATUS_FILE=str(reconcile_state_dir / "reconcile-status.json"),
        SECRET_KEY=os.environ.get("WG_MANAGER_SECRET_KEY") or load_or_create_secret(data_dir),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_env_bool("WG_COOKIE_SECURE", True),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=int(os.environ.get("WG_SESSION_MINUTES", "30"))),
        MAX_INSTALLER_BYTES=int(os.environ.get("WG_MAX_INSTALLER_BYTES", str(200 * 1024 * 1024))),
        MAX_CONTENT_LENGTH=int(os.environ.get("WG_MAX_INSTALLER_BYTES", str(200 * 1024 * 1024))) + 1024 * 1024,
        LOGIN_ATTEMPT_LIMIT=int(os.environ.get("WG_LOGIN_ATTEMPT_LIMIT", "5")),
        LOGIN_WINDOW_SECONDS=int(os.environ.get("WG_LOGIN_WINDOW_SECONDS", "300")),
        WG_TUNNEL_CIDR=tunnel_cidr,
        WG_RESERVED_IPS=os.environ.get("WG_RESERVED_IPS", ""),
        WG_SERVER_PUBLIC_KEY=os.environ.get(
            "WG_SERVER_PUBLIC_KEY", "mF/8Ssq4S08vD+zL/yQyAvTfGuWn7gR6x+PInwXvWnM="
        ),
        WG_ENDPOINT=os.environ.get("WG_ENDPOINT", "vpn.example.invalid:51820"),
        WG_DNS=os.environ.get("WG_DNS", ""),
        WG_ALLOWED_IPS=os.environ.get("WG_ALLOWED_IPS"),
        WG_INTERFACE=os.environ.get("WG_INTERFACE", "wg0"),
        WG_ADAPTER=os.environ.get("WG_ADAPTER", "file"),
        WG_RECONCILE_SOCKET=os.environ.get(
            "WG_RECONCILE_SOCKET", "/run/wireguard-manager/reconcile.sock"
        ),
        WG_RECONCILE_TIMEOUT_SECONDS=float(
            os.environ.get("WG_RECONCILE_TIMEOUT_SECONDS", "5")
        ),
        WG_RESET_ACTIVATION_DELAY_SECONDS=float(
            os.environ.get("WG_RESET_ACTIVATION_DELAY_SECONDS", "0" if testing else "2")
        ),
    )
    if test_config:
        app.config.update(test_config)

    # Split tunnel is the safe default. Full-tunnel routing must be an explicit
    # operator choice after forwarding, NAT, DNS, and recovery access are ready.
    app.config["WG_ALLOWED_IPS"] = normalize_client_allowed_ips(
        app.config.get("WG_ALLOWED_IPS") or app.config["WG_TUNNEL_CIDR"]
    )

    network = ipaddress.ip_network(app.config["WG_TUNNEL_CIDR"], strict=True)
    if network.version != 4 or network.num_addresses < 4 or network.num_addresses > 65536:
        raise RuntimeError("WG_TUNNEL_CIDR must be an IPv4 pool with 4-65536 addresses")
    app.config["WG_RESERVED_IPS"] = _normalize_reserved_ips(
        app.config.get("WG_RESERVED_IPS", ""), network
    )
    if app.config["WG_ADAPTER"] not in ("file", "dry-run", "reconciler"):
        raise RuntimeError("WG_ADAPTER must be file, dry-run, or reconciler")
    reset_delay = app.config["WG_RESET_ACTIVATION_DELAY_SECONDS"]
    minimum_reset_delay = 0 if testing else 1
    if not minimum_reset_delay <= reset_delay <= 30:
        raise RuntimeError(
            f"WG_RESET_ACTIVATION_DELAY_SECONDS must be {minimum_reset_delay}-30 seconds"
        )

    initialize(
        app.config["DATABASE"], default_client_allowed_ips=app.config["WG_ALLOWED_IPS"]
    )
    Path(app.config["INSTALLER_DIR"]).mkdir(parents=True, exist_ok=True, mode=0o700)

    from .routes import web

    app.register_blueprint(web)
    app.teardown_appcontext(close_db)

    @app.before_request
    def load_authenticated_user() -> None:
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = get_db().execute(
                "SELECT id, username, role, enabled, device_quota, session_version FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if (
                g.user is None
                or not g.user["enabled"]
                or g.user["session_version"] != session.get("session_version")
            ):
                session.clear()
                g.user = None

    @app.after_request
    def security_headers(response):
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store, max-age=0")
            response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response

    return app


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.casefold() in ("1", "true", "yes", "on")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalize_reserved_ips(value, network: ipaddress.IPv4Network) -> frozenset[str]:
    raw_values = value.split(",") if isinstance(value, str) else value
    reserved: set[str] = set()
    for raw in raw_values:
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError as error:
            raise RuntimeError("WG_RESERVED_IPS must contain comma-separated IPv4 addresses") from error
        if address.version != 4 or address not in network:
            raise RuntimeError("every WG_RESERVED_IPS address must be inside WG_TUNNEL_CIDR")
        reserved.add(str(address))
    return frozenset(reserved)
