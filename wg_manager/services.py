from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .adapter import make_adapter
from .db import transaction
from .security import generate_keypair, hash_password


CLIENT_TYPES = ("windows", "macos", "linux", "ios", "android")
ARCHITECTURES = ("x86", "x86_64", "arm", "arm64", "universal", "any")
ALLOWED_INSTALLER_SUFFIXES = {
    "windows": (".msi", ".exe"),
    "macos": (".pkg", ".dmg"),
    "linux": (".deb", ".rpm", ".appimage", ".tar.gz"),
    "ios": (".ipa",),
    "android": (".apk",),
}
ALLOWED_INSTALLER_MEDIA_TYPES = {
    "application/octet-stream",
    "application/x-msdownload",
    "application/x-msi",
    "application/vnd.apple.installer+xml",
    "application/x-apple-diskimage",
    "application/vnd.android.package-archive",
    "application/vnd.debian.binary-package",
    "application/x-rpm",
    "application/gzip",
    "application/x-tar",
}
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}\Z")
DEVICE_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,80}\Z")


def bi(zh: str, en: str) -> str:
    return f"{zh} / {en}"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def audit(
    connection,
    *,
    action: str,
    object_type: str,
    object_id: str | None,
    actor_user_id: int | None,
    actor_kind: str,
    outcome: str = "success",
    source_hash: str | None = None,
    details: dict | None = None,
) -> None:
    connection.execute(
        """INSERT INTO audit_events(
             actor_user_id, actor_kind, action, object_type, object_id,
             outcome, source_hash, details_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            actor_user_id,
            actor_kind,
            action,
            object_type,
            object_id,
            outcome,
            source_hash,
            json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
        ),
    )


def create_user(
    connection,
    *,
    username: str,
    password: str,
    quota: int,
    role: str = "user",
    actor_user_id: int | None = None,
    actor_kind: str = "cli",
):
    username = username.strip()
    if not USERNAME_RE.fullmatch(username):
        raise DomainError(bi("用户名须为 3-64 个安全字符", "username must be 3-64 safe characters"))
    if role not in ("admin", "user"):
        raise DomainError(bi("角色无效", "invalid role"))
    if not 0 <= quota <= 100:
        raise DomainError(bi("配额须在 0 到 100 之间", "quota must be between 0 and 100"))
    try:
        password_hash = hash_password(password)
    except ValueError as error:
        raise DomainError(bi("密码长度须为 12-1024 个字符", str(error))) from error
    try:
        with transaction(connection, immediate=True):
            cursor = connection.execute(
                "INSERT INTO users(username, password_hash, role, device_quota) VALUES (?, ?, ?, ?)",
                (username, password_hash, role, quota),
            )
            user_id = cursor.lastrowid
            audit(
                connection,
                action="user.create",
                object_type="user",
                object_id=str(user_id),
                actor_user_id=actor_user_id,
                actor_kind=actor_kind,
                details={"quota": quota, "role": role},
            )
    except Exception as error:
        if "UNIQUE constraint failed: users.username" in str(error):
            raise DomainError(bi("用户名已存在", "username already exists"), 409) from error
        raise
    return connection.execute(
        "SELECT id, username, role, enabled, device_quota, created_at FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()


def update_user(
    connection,
    *,
    user_id: int,
    enabled: bool,
    quota: int,
    actor_user_id: int | None,
    actor_kind: str,
) -> None:
    if not 0 <= quota <= 100:
        raise DomainError(bi("配额须在 0 到 100 之间", "quota must be between 0 and 100"))
    with transaction(connection, immediate=True):
        row = connection.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise DomainError(bi("用户不存在", "user not found"), 404)
        if row["role"] == "admin" and not enabled:
            enabled_admins = connection.execute(
                "SELECT count(*) AS count FROM users WHERE role = 'admin' AND enabled = 1"
            ).fetchone()["count"]
            if enabled_admins <= 1:
                raise DomainError(bi("不能禁用最后一个启用的管理员", "cannot disable the last enabled administrator"), 409)
        connection.execute(
            """UPDATE users SET
               session_version = session_version + CASE WHEN enabled <> ? THEN 1 ELSE 0 END,
               enabled = ?, device_quota = ?,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (int(enabled), int(enabled), quota, user_id),
        )
        audit(
            connection,
            action="user.update",
            object_type="user",
            object_id=str(user_id),
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            details={"enabled": enabled, "quota": quota},
        )


def set_user_password(
    connection,
    *,
    user_id: int,
    password: str,
    actor_user_id: int | None,
    actor_kind: str,
) -> None:
    try:
        password_hash = hash_password(password)
    except ValueError as error:
        raise DomainError(bi("密码长度须为 12-1024 个字符", str(error))) from error
    with transaction(connection, immediate=True):
        updated = connection.execute(
            """UPDATE users SET password_hash = ?, session_version = session_version + 1,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (password_hash, user_id),
        ).rowcount
        if not updated:
            raise DomainError(bi("用户不存在", "user not found"), 404)
        audit(
            connection,
            action="user.password_reset",
            object_type="user",
            object_id=str(user_id),
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
        )


def create_device(
    connection,
    config,
    *,
    user_id: int,
    name: str,
    client_type: str,
    actor_user_id: int | None,
    actor_kind: str,
) -> tuple[dict, str]:
    name = name.strip()
    if not DEVICE_NAME_RE.fullmatch(name):
        raise DomainError(bi("设备名称须为 1-80 个可打印字符", "device name must be 1-80 printable characters"))
    if client_type not in CLIENT_TYPES:
        raise DomainError(bi("客户端类型无效", "invalid client type"))
    private_key, public_key = generate_keypair()
    device_id = str(uuid.uuid4())
    quota_denied = False
    try:
        with transaction(connection, immediate=True):
            user = connection.execute(
                "SELECT enabled, device_quota FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if user is None:
                raise DomainError(bi("用户不存在", "user not found"), 404)
            if not user["enabled"]:
                raise DomainError(bi("用户已禁用", "user is disabled"), 403)
            count = connection.execute(
                "SELECT count(*) AS count FROM devices WHERE user_id = ?", (user_id,)
            ).fetchone()["count"]
            if count >= user["device_quota"]:
                audit(
                    connection,
                    action="device.create",
                    object_type="device",
                    object_id=None,
                    actor_user_id=actor_user_id,
                    actor_kind=actor_kind,
                    outcome="denied",
                    details={"reason": "quota"},
                )
                quota_denied = True
            else:
                static_ip = allocate_ip(connection, config["WG_TUNNEL_CIDR"])
                connection.execute(
                    """INSERT INTO devices(id, user_id, name, client_type, static_ip, public_key)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (device_id, user_id, name, client_type, static_ip, public_key),
                )
                state_hash = _reconcile(connection, config)
                audit(
                    connection,
                    action="device.create",
                    object_type="device",
                    object_id=device_id,
                    actor_user_id=actor_user_id,
                    actor_kind=actor_kind,
                    details={"client_type": client_type, "state_sha256": state_hash},
                )
    except Exception as error:
        if "UNIQUE constraint failed: devices.user_id, devices.name" in str(error):
            raise DomainError(bi("设备名称已存在", "device name already exists"), 409) from error
        raise
    if quota_denied:
        raise DomainError(bi("设备配额已用完", "device quota exceeded"), 409)
    device = {
        "id": device_id,
        "user_id": user_id,
        "name": name,
        "client_type": client_type,
        "static_ip": static_ip,
        "public_key": public_key,
        "key_generation": 1,
    }
    return device, build_client_config(config, static_ip, private_key)


def reset_device(
    connection,
    config,
    *,
    device_id: str,
    owner_user_id: int | None,
    actor_user_id: int | None,
    actor_kind: str,
) -> tuple[dict, str]:
    private_key, public_key = generate_keypair()
    with transaction(connection, immediate=True):
        sql = "SELECT * FROM devices WHERE id = ?"
        parameters: tuple = (device_id,)
        if owner_user_id is not None:
            sql += " AND user_id = ?"
            parameters += (owner_user_id,)
        device = connection.execute(sql, parameters).fetchone()
        if device is None:
            raise DomainError(bi("设备不存在或无权访问", "device not found or not authorized"), 404)
        connection.execute(
            """UPDATE devices SET public_key = ?, key_generation = key_generation + 1,
               updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (public_key, device_id),
        )
        state_hash = _reconcile(connection, config)
        audit(
            connection,
            action="device.reset",
            object_type="device",
            object_id=device_id,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            details={"ip_preserved": True, "state_sha256": state_hash},
        )
    updated = dict(device)
    updated["public_key"] = public_key
    updated["key_generation"] += 1
    return updated, build_client_config(config, device["static_ip"], private_key)


def delete_device(
    connection,
    config,
    *,
    device_id: str,
    owner_user_id: int | None,
    actor_user_id: int | None,
    actor_kind: str,
) -> str:
    with transaction(connection, immediate=True):
        sql = "SELECT static_ip FROM devices WHERE id = ?"
        parameters: tuple = (device_id,)
        if owner_user_id is not None:
            sql += " AND user_id = ?"
            parameters += (owner_user_id,)
        device = connection.execute(sql, parameters).fetchone()
        if device is None:
            raise DomainError(bi("设备不存在或无权访问", "device not found or not authorized"), 404)
        connection.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        state_hash = _reconcile(connection, config)
        audit(
            connection,
            action="device.delete",
            object_type="device",
            object_id=device_id,
            actor_user_id=actor_user_id,
            actor_kind=actor_kind,
            details={"ip_release": "immediate_after_peer_removal", "state_sha256": state_hash},
        )
    return device["static_ip"]


def allocate_ip(connection, cidr: str) -> str:
    network = ipaddress.ip_network(cidr, strict=True)
    if network.version != 4:
        raise DomainError(bi("仅支持 IPv4 隧道地址池", "only IPv4 tunnel pools are supported"))
    if network.num_addresses > 65536:
        raise DomainError(bi("隧道地址池过大", "tunnel pool is too large"))
    used = {
        int(ipaddress.ip_address(row["static_ip"]))
        for row in connection.execute("SELECT static_ip FROM devices")
    }
    # Network, the first host (server gateway), and broadcast are reserved.
    for value in range(int(network.network_address) + 2, int(network.broadcast_address)):
        if value not in used:
            return str(ipaddress.ip_address(value))
    raise DomainError(bi("隧道 IP 地址池已耗尽", "tunnel IP pool exhausted"), 409)


def build_client_config(config, static_ip: str, private_key: str) -> str:
    _validate_wireguard_settings(config)
    prefix = ipaddress.ip_network(config["WG_TUNNEL_CIDR"]).prefixlen
    dns_line = f"DNS = {config['WG_DNS']}\n" if config["WG_DNS"] else ""
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {static_ip}/{prefix}\n"
        f"{dns_line}\n"
        "[Peer]\n"
        f"PublicKey = {config['WG_SERVER_PUBLIC_KEY']}\n"
        f"Endpoint = {config['WG_ENDPOINT']}\n"
        f"AllowedIPs = {config['WG_ALLOWED_IPS']}\n"
        "PersistentKeepalive = 25\n"
    )


def store_installer(
    connection,
    config,
    *,
    stream,
    filename: str,
    platform: str,
    architecture: str,
    version: str,
    media_type: str,
    license_name: str,
    license_source_url: str,
    redistribution_confirmed: bool,
    actor_user_id: int | None,
    actor_kind: str,
):
    filename = filename.strip()
    if not filename or Path(filename).name != filename or "\x00" in filename:
        raise DomainError(bi("安装包文件名不安全", "unsafe installer filename"))
    if platform not in CLIENT_TYPES or architecture not in ARCHITECTURES:
        raise DomainError(bi("平台或架构无效", "invalid platform or architecture"))
    suffix = _installer_suffix(filename, platform)
    media_type = (media_type or "application/octet-stream").split(";", 1)[0].strip().casefold()
    if media_type not in ALLOWED_INSTALLER_MEDIA_TYPES:
        raise DomainError(bi("不允许此安装包媒体类型", "installer media type is not allowed"))
    if not (1 <= len(version.strip()) <= 64):
        raise DomainError(bi("版本须为 1-64 个字符", "version must be 1-64 characters"))
    if not redistribution_confirmed:
        raise DomainError(bi("必须确认再分发许可", "redistribution license confirmation is required"))
    if not (1 <= len(license_name.strip()) <= 120):
        raise DomainError(bi("必须填写许可证或条款名称", "license name is required"))
    parsed_url = urlparse(license_source_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc or len(license_source_url) > 500:
        raise DomainError(bi("许可证来源必须是 HTTPS URL", "license source must be an HTTPS URL"))
    installer_id = str(uuid.uuid4())
    stored_filename = installer_id + suffix
    directory = Path(config["INSTALLER_DIR"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=directory)
    sha256 = hashlib.sha256()
    size = 0
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as destination:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > config["MAX_INSTALLER_BYTES"]:
                    raise DomainError(bi("安装包超过大小限制", "installer exceeds size limit"), 413)
                sha256.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if size == 0:
            raise DomainError(bi("安装包为空", "installer is empty"))
        final_path = directory / stored_filename
        os.replace(temporary_name, final_path)
        with transaction(connection, immediate=True):
            connection.execute(
                """INSERT INTO installers(
                     id, platform, architecture, version, original_filename,
                     stored_filename, sha256, size_bytes, media_type,
                     license_name, license_source_url, redistribution_confirmed, uploaded_by
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    installer_id,
                    platform,
                    architecture,
                    version.strip(),
                    filename,
                    stored_filename,
                    sha256.hexdigest(),
                    size,
                    media_type,
                    license_name.strip(),
                    license_source_url,
                    actor_user_id,
                ),
            )
            audit(
                connection,
                action="installer.upload",
                object_type="installer",
                object_id=installer_id,
                actor_user_id=actor_user_id,
                actor_kind=actor_kind,
                details={"platform": platform, "sha256": sha256.hexdigest(), "size_bytes": size},
            )
    except BaseException:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass
        final_candidate = directory / stored_filename
        if final_candidate.exists() and connection.execute(
            "SELECT 1 FROM installers WHERE id = ?", (installer_id,)
        ).fetchone() is None:
            final_candidate.unlink()
        raise
    return connection.execute("SELECT * FROM installers WHERE id = ?", (installer_id,)).fetchone()


def verified_installer_path(connection, config, installer_id: str):
    row = connection.execute("SELECT * FROM installers WHERE id = ?", (installer_id,)).fetchone()
    if row is None:
        raise DomainError(bi("安装包不存在", "installer not found"), 404)
    path = Path(config["INSTALLER_DIR"]) / row["stored_filename"]
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(Path(config["INSTALLER_DIR"]).resolve(strict=True))
    except (FileNotFoundError, ValueError) as error:
        raise DomainError(bi("安装包文件不可用", "installer file is unavailable"), 410) from error
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != row["sha256"]:
        raise DomainError(bi("安装包完整性校验失败", "installer integrity check failed"), 409)
    return row, resolved


def reconcile_desired_state(connection, config) -> str:
    with transaction(connection, immediate=True):
        state_hash = _reconcile(connection, config)
        audit(
            connection,
            action="peer_state.reconcile",
            object_type="peer_state",
            object_id=None,
            actor_user_id=None,
            actor_kind="cli",
            details={"state_sha256": state_hash},
        )
    return state_hash


def _installer_suffix(filename: str, platform: str) -> str:
    lowered = filename.casefold()
    for suffix in ALLOWED_INSTALLER_SUFFIXES[platform]:
        if lowered.endswith(suffix.casefold()):
            return suffix
    raise DomainError(bi("该平台不允许此安装包类型", "installer file type is not allowed for this platform"))


def _reconcile(connection, config) -> str:
    adapter = make_adapter(
        config["WG_ADAPTER"], path=config["EXPECTED_PEERS_FILE"], interface=config["WG_INTERFACE"]
    )
    return adapter.reconcile(connection)


def _validate_wireguard_settings(config) -> None:
    values = (
        config["WG_SERVER_PUBLIC_KEY"],
        config["WG_ENDPOINT"],
        config["WG_DNS"],
        config["WG_ALLOWED_IPS"],
    )
    if any("\n" in value or "\r" in value for value in values):
        raise RuntimeError("WireGuard settings cannot contain newlines")
    try:
        raw_key = base64.b64decode(config["WG_SERVER_PUBLIC_KEY"], validate=True)
    except ValueError as error:
        raise RuntimeError("WG_SERVER_PUBLIC_KEY must be valid base64") from error
    if len(raw_key) != 32:
        raise RuntimeError("WG_SERVER_PUBLIC_KEY must decode to 32 bytes")
