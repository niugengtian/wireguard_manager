from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from . import create_app
from .db import get_db
from .services import (
    ARCHITECTURES,
    CLIENT_TYPES,
    DomainError,
    bi,
    create_device,
    create_user,
    delete_device,
    reconcile_desired_state,
    reset_device,
    set_user_password,
    store_installer,
    update_device_allowed_ips,
    update_user,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wg-manager",
        description=bi("本机 WireGuard 用户与设备管理命令行", "Local WireGuard user and device manager"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""常用示例 / Examples:
  wg-manager user create alice --quota 2
  wg-manager device create alice laptop --type linux --allowed-ips 10.0.0.0/8 --output ./laptop.conf
  wg-manager device allowed-ips DEVICE_ID --set "10.0.0.0/8, 172.31.0.0/16"
  wg-manager device list --username alice
  wg-manager reconcile

安全提示 / Security:
  密码使用无回显交互输入；配置不会打印到终端。
  Passwords are prompted without echo; configurations are never printed.""",
    )
    groups = parser.add_subparsers(dest="group", required=True, title=bi("命令组", "command groups"))

    user = groups.add_parser("user", help=bi("管理用户", "Manage users"))
    user_commands = user.add_subparsers(dest="command", required=True)
    user_create = user_commands.add_parser("create", help=bi("创建用户", "Create a user"))
    user_create.add_argument("username", help=bi("用户名", "Username"))
    user_create.add_argument("--role", choices=("admin", "user"), default="user", help=bi("角色", "Role"))
    user_create.add_argument("--quota", type=int, default=2, help=bi("设备配额", "Device quota"))
    user_commands.add_parser("list", help=bi("列出用户", "List users"))
    user_update = user_commands.add_parser("update", help=bi("启停用户并修改配额", "Enable/disable a user and change quota"))
    user_update.add_argument("username")
    user_update.add_argument("--quota", type=int, required=True)
    enabled = user_update.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enable", action="store_true")
    enabled.add_argument("--disable", action="store_true")
    user_password = user_commands.add_parser("password", help=bi("重置密码并撤销会话", "Reset password and revoke sessions"))
    user_password.add_argument("username")

    device = groups.add_parser("device", help=bi("管理设备", "Manage devices"))
    device_commands = device.add_subparsers(dest="command", required=True)
    device_list = device_commands.add_parser("list", help=bi("列出设备", "List devices"))
    device_list.add_argument("--username", help=bi("按用户名筛选", "Filter by username"))
    device_create = device_commands.add_parser("create", help=bi("新增设备并一次性写出配置", "Create a device and write its one-time config"))
    device_create.add_argument("username")
    device_create.add_argument("name")
    device_create.add_argument("--type", choices=CLIENT_TYPES, required=True, help=bi("客户端类型", "Client type"))
    device_create.add_argument(
        "--allowed-ips",
        help=bi("该设备客户端路由范围；默认使用 WG_ALLOWED_IPS", "Client routes for this device; defaults to WG_ALLOWED_IPS"),
    )
    device_create.add_argument("--output", type=Path, required=True, help=bi("必须不存在的配置输出路径", "New configuration output path"))
    device_reset = device_commands.add_parser("reset", help=bi("轮换密钥并保留 IP", "Rotate keys and preserve IP"))
    device_reset.add_argument("device_id")
    device_reset.add_argument("--output", type=Path, required=True, help=bi("必须不存在的配置输出路径", "New configuration output path"))
    device_delete = device_commands.add_parser("delete", help=bi("删除设备并释放 IP", "Delete a device and release IP"))
    device_delete.add_argument("device_id")
    device_allowed_ips = device_commands.add_parser(
        "allowed-ips",
        help=bi("修改设备客户端路由范围（需 reset 交付）", "Change client routes (reset required for delivery)"),
    )
    device_allowed_ips.add_argument("device_id")
    device_allowed_ips.add_argument("--set", required=True, dest="allowed_ips", help=bi("逗号分隔 IPv4 CIDR", "Comma-separated IPv4 CIDRs"))

    installer = groups.add_parser("installer", help=bi("管理客户端安装包", "Manage downloadable client packages"))
    installer_commands = installer.add_subparsers(dest="command", required=True)
    installer_commands.add_parser("list", help=bi("列出安装包", "List installers"))
    installer_add = installer_commands.add_parser("add", help=bi("校验并保存安装包", "Validate and store an installer"))
    installer_add.add_argument("path", type=Path)
    installer_add.add_argument("--platform", choices=CLIENT_TYPES, required=True)
    installer_add.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    installer_add.add_argument("--version", required=True)
    installer_add.add_argument("--license-name", required=True)
    installer_add.add_argument("--license-source-url", required=True)
    installer_add.add_argument("--confirm-redistribution", action="store_true", required=True)

    groups.add_parser("reconcile", help=bi("原子重建期望 Peer 状态文件", "Atomically rebuild desired peer state"))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        app = create_app()
        with app.app_context():
            _dispatch(args, app.config)
    except (DomainError, RuntimeError, OSError) as error:
        print(f"BLOCKED: 操作失败 / operation failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


def _dispatch(args, config) -> None:
    connection = get_db()
    if args.group == "user":
        if args.command == "create":
            password = _new_password()
            row = create_user(
                connection,
                username=args.username,
                password=password,
                quota=args.quota,
                role=args.role,
                actor_kind="cli",
            )
            print(f"READY: 用户已创建 / user created: {row['username']} ({row['role']}, quota={row['device_quota']})")
        elif args.command == "list":
            rows = connection.execute(
                "SELECT username, role, enabled, device_quota FROM users ORDER BY username"
            ).fetchall()
            _table(("用户名/USERNAME", "角色/ROLE", "启用/ENABLED", "配额/QUOTA"), rows)
        elif args.command == "update":
            user = _user_by_name(connection, args.username)
            update_user(
                connection,
                config,
                user_id=user["id"],
                enabled=args.enable,
                quota=args.quota,
                actor_user_id=None,
                actor_kind="cli",
            )
            print(f"READY: 用户已更新 / user updated: {user['username']}")
        elif args.command == "password":
            user = _user_by_name(connection, args.username)
            set_user_password(
                connection,
                user_id=user["id"],
                password=_new_password(),
                actor_user_id=None,
                actor_kind="cli",
            )
            print(f"READY: 密码已重置 / password reset: {user['username']}")
        return

    if args.group == "device":
        if args.command == "list":
            parameters: tuple = ()
            where = ""
            if args.username:
                where = " WHERE users.username = ?"
                parameters = (args.username,)
            rows = connection.execute(
                """SELECT devices.id, users.username, devices.name, devices.client_type,
                          devices.static_ip, devices.client_allowed_ips,
                          CASE WHEN devices.policy_revision = devices.delivered_policy_revision
                               THEN 'no' ELSE 'yes' END AS reset_required,
                          devices.key_generation
                   FROM devices JOIN users ON users.id = devices.user_id"""
                + where
                + " ORDER BY users.username, devices.name",
                parameters,
            ).fetchall()
            _table(
                (
                    "ID",
                    "用户名/USERNAME",
                    "名称/NAME",
                    "类型/TYPE",
                    "隧道IP/TUNNEL_IP",
                    "客户端路由/CLIENT_ROUTES",
                    "需重置/RESET_REQUIRED",
                    "代次/GEN",
                ),
                rows,
            )
        elif args.command == "create":
            user = _user_by_name(connection, args.username)
            descriptor = _reserve_secret_output(args.output)
            try:
                device, configuration = create_device(
                    connection,
                    config,
                    user_id=user["id"],
                    name=args.name,
                    client_type=args.type,
                    client_allowed_ips=args.allowed_ips,
                    actor_user_id=None,
                    actor_kind="cli",
                )
                _finish_secret_output(descriptor, args.output, configuration)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                args.output.unlink(missing_ok=True)
                raise
            print(f"READY: 设备 {device['id']} 已创建；一次性配置已按 0600 写入 / device created; one-time config written with mode 0600")
        elif args.command == "reset":
            descriptor = _reserve_secret_output(args.output)
            try:
                device, configuration = reset_device(
                    connection,
                    config,
                    device_id=args.device_id,
                    owner_user_id=None,
                    actor_user_id=None,
                    actor_kind="cli",
                )
                _finish_secret_output(descriptor, args.output, configuration)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                args.output.unlink(missing_ok=True)
                raise
            print(f"READY: 设备 {device['id']} 已重置，静态 IP 保留，配置按 0600 写入 / device reset; static IP preserved; config written with mode 0600")
        elif args.command == "delete":
            released = delete_device(
                connection,
                config,
                device_id=args.device_id,
                owner_user_id=None,
                actor_user_id=None,
                actor_kind="cli",
            )
            print(f"READY: 设备已删除；Peer 移除后释放隧道 IP {released} / device deleted; tunnel IP released after peer removal")
        elif args.command == "allowed-ips":
            device = update_device_allowed_ips(
                connection,
                device_id=args.device_id,
                client_allowed_ips=args.allowed_ips,
                actor_user_id=None,
                actor_kind="cli",
            )
            reset_required = device["policy_revision"] != device["delivered_policy_revision"]
            suffix = (
                bi("；请执行 device reset 生成并交付新配置", "; run device reset to generate and deliver the new configuration")
                if reset_required
                else bi("；范围未变化", "; routes unchanged")
            )
            print(f"READY: 客户端 AllowedIPs 已保存 / client AllowedIPs saved: {device['client_allowed_ips']}{suffix}")
        return

    if args.group == "installer":
        if args.command == "list":
            rows = connection.execute(
                "SELECT id, platform, architecture, version, size_bytes, sha256 FROM installers ORDER BY platform, version"
            ).fetchall()
            _table(("ID", "平台/PLATFORM", "架构/ARCH", "版本/VERSION", "字节/BYTES", "SHA256"), rows)
        elif args.command == "add":
            if not args.path.is_file():
                raise DomainError("installer path is not a regular file")
            with args.path.open("rb") as stream:
                row = store_installer(
                    connection,
                    config,
                    stream=stream,
                    filename=args.path.name,
                    platform=args.platform,
                    architecture=args.architecture,
                    version=args.version,
                    media_type="application/octet-stream",
                    license_name=args.license_name,
                    license_source_url=args.license_source_url,
                    redistribution_confirmed=args.confirm_redistribution,
                    actor_user_id=None,
                    actor_kind="cli",
                )
            print(f"READY: 安装包已保存 / installer stored: {row['id']} sha256={row['sha256']} bytes={row['size_bytes']}")
        return

    if args.group == "reconcile":
        digest = reconcile_desired_state(connection, config)
        if config["WG_ADAPTER"] == "reconciler":
            print(f"VERIFIED: 在线 Peer 已热同步并校验 / live peers hot-applied and verified; sha256={digest}")
        else:
            print(f"READY: 期望 Peer 状态已重建 / desired peer state rebuilt; sha256={digest}")


def _new_password() -> str:
    first = getpass.getpass("新密码 / New password: ")
    second = getpass.getpass("再次输入 / Repeat password: ")
    if first != second:
        raise DomainError(bi("两次输入的密码不一致", "passwords do not match"))
    return first


def _user_by_name(connection, username: str):
    row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        raise DomainError(bi("用户不存在", "user not found"), 404)
    return row


def _reserve_secret_output(path: Path) -> int:
    path = path.expanduser().resolve()
    if not path.parent.is_dir():
        raise DomainError(bi("输出文件的父目录不存在", "output parent directory does not exist"))
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _finish_secret_output(descriptor: int, path: Path, configuration: str) -> None:
    try:
        payload = configuration.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _table(headers, rows) -> None:
    print("\t".join(headers))
    for row in rows:
        print("\t".join(str(value) for value in row))


if __name__ == "__main__":
    main()
