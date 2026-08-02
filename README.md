# WireGuard Manager｜WireGuard 用户与设备配置管理

[中文](#中文说明) · [English](#english) · [Manager 安装指南 / Manager install](docs/WG_MANAGER_INSTALL.md) · [服务端部署指南 / Server guide](docs/WIREGUARD_SERVER_GUIDE.md) · [验收记录 / Acceptance](ACCEPTANCE.md)

## 中文说明

`READY`：一个小型、安全、可运行的 WireGuard 用户与设备管理站点。技术栈只有 Python、Flask、服务端模板和本地 SQLite，同时提供使用同一套业务规则的本机 CLI。

它只生成确定性的期望 Peer 状态文件，**不会**执行 `wg`、`wg-quick` 或 `syncconf`。真实 WireGuard 同步必须等待人工确认后，通过独立的最小权限 reconciler 接入。

### 能做什么

- 管理员创建、启停用户，设置设备配额，查看或删除设备和审计记录。
- 用户登录后按 Windows、macOS、Linux、iOS、Android 类型自助新增设备。
- 每台设备使用独立密钥和全局唯一静态隧道 IP；超过配额会被拒绝。
- 配置不可编辑，只能新增、`reset` 或 `delete`。
- `reset` 撤销旧公钥、生成新密钥，默认保留静态 IP。
- `delete` 先从期望 Peer 状态移除设备，再立即释放 IP；旧公钥仍然无效。
- 桌面端一次性下载 `.conf`；移动端可显示一次性二维码。
- 管理员核对许可后可上传客户端安装包，系统记录平台、架构、版本、SHA-256 和大小。
- Web 和 CLI 共用 SQLite、配额、IP 分配、密钥生命周期、RBAC 和审计规则。

### 安全模型

| 数据 | 保存内容 | 安全约束 |
| --- | --- | --- |
| 用户 | 角色、启停、配额、scrypt 密码哈希、会话版本 | 不保存明文密码；密码重置会撤销旧会话 |
| 设备 | 所属用户、客户端类型、静态 IP、公钥、密钥代次 | 不存在私钥字段；静态 IP 和公钥均有唯一约束 |
| 安装包 | 元数据、许可证依据、SHA-256、仓库外文件名 | 限制扩展名、媒体类型、大小和路径；下载时再次校验哈希 |
| 审计 | 非敏感动作、结果和对象 ID | 不写入密码、私钥、完整配置或原始来源 IP |

每台设备的私钥只会存在于当前请求/CLI 内存，以及下面三种一次性交付位置之一：

1. 带 `Cache-Control: no-store` 的配置下载；
2. 带 `no-store` 的一次性二维码页面；
3. CLI 调用者指定的新文件，权限固定为 `0600`。

SQLite 与期望 Peer 文件只保存公钥。配置丢失后无法找回，只能执行 `reset`。

### 安装与启动

在已经运行 WireGuard 的服务器上部署时，请直接使用 [WireGuard Manager 安装与启动指南](docs/WG_MANAGER_INSTALL.md)。其中记录了源码解压、非 root 安装、现有 `wg0` 参数、systemd、通过隧道地址访问和常见错误排查。

需要 Python 3.11 或更高版本，并使用专用的**非 root** 系统用户运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .

export WG_MANAGER_DATA_DIR=/var/lib/wireguard-manager
export WG_SERVER_PUBLIC_KEY='替换为服务端公钥'
export WG_ENDPOINT='vpn.example.invalid:51820'
export WG_COOKIE_SECURE=1

.venv/bin/wg-manager user create admin --role admin --quota 0
.venv/bin/wg-manager-web
```

密码通过无回显交互输入。Web 默认只监听 `127.0.0.1:8080`，建议由同机反向代理提供 HTTPS。程序拒绝以 root 运行，也拒绝把数据目录放进源码目录。

仅在本机 HTTP 演示时可使用 `WG_COOKIE_SECURE=0`，不得用于跨网络访问。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `WG_MANAGER_DATA_DIR` | 用户数据目录 | SQLite、会话密钥、安装包、期望 Peer 文件；必须位于源码外 |
| `WG_TUNNEL_CIDR` | `10.44.0.0/24` | 客户端静态 IP 池；网络地址、首个主机地址和广播地址保留 |
| `WG_SERVER_PUBLIC_KEY` | 非生产占位值 | 写入客户端配置的服务端公钥 |
| `WG_ENDPOINT` | `vpn.example.invalid:51820` | 客户端连接端点 |
| `WG_DNS` | 空 | 可选客户端 DNS；仅在确实提供 DNS 服务时设置 |
| `WG_ALLOWED_IPS` | `0.0.0.0/0` | 进入隧道的路由；未配置 IPv6 时不要加入 `::/0` |
| `WG_ADAPTER` | `file` | 只允许 `file` 或 `dry-run` |
| `WG_MAX_INSTALLER_BYTES` | 200 MiB | 安装包最大大小 |

生成可用配置前，必须替换所有占位 WireGuard 参数。

### CLI

```sh
# 查看中英双语帮助
.venv/bin/wg-manager --help
.venv/bin/wg-manager user --help
.venv/bin/wg-manager device --help

# 用户；密码始终交互输入
.venv/bin/wg-manager user create alice --quota 2
.venv/bin/wg-manager user update alice --quota 3 --enable
.venv/bin/wg-manager user password alice
.venv/bin/wg-manager user list

# 配置输出路径必须不存在；CLI 不会把私钥或配置打印到终端
.venv/bin/wg-manager device create alice workstation --type linux --output /secure/path/workstation.conf
.venv/bin/wg-manager device list --username alice
.venv/bin/wg-manager device reset DEVICE_ID --output /secure/path/workstation-reset.conf
.venv/bin/wg-manager device delete DEVICE_ID

# 只重建期望 Peer 文件，不接触现网接口
.venv/bin/wg-manager reconcile
```

### 安装包再分发边界

优先向用户提供官方商店、官方下载安装页或系统包管理器。此项目不会自动抓取或镜像厂商文件。管理员上传前必须记录具体文件的许可证/条款名称、HTTPS 来源并确认允许再分发。

源码许可证不能自动证明 App Store 包或厂商构建的二进制允许镜像。iOS/macOS 商店包或权利不清晰的文件，没有单独授权时不要上传。这是工程防护，不是法律意见。

官方参考：[WireGuard 安装](https://www.wireguard.com/install/)、[官方仓库](https://www.wireguard.com/repositories/)。

### 验证

```sh
.venv/bin/pip install -e '.[test,audit]'
.venv/bin/pytest
.venv/bin/pip-audit --cache-dir /tmp/wg-manager-pip-audit-cache
```

自动化验收覆盖 quota=2、一次性配置、超额拒绝、reset 撤销旧公钥并保留 IP、delete 移除 Peer 并复用 IP、并发唯一 IP、对象授权、安装包 SHA-256、CSRF、登录限速、CLI `0600` 文件以及敏感值不落盘。真实浏览器验收见 [ACCEPTANCE.md](ACCEPTANCE.md)。

`NOT VERIFIED`：真实 WireGuard reconciler、AWS 部署与 AWS 资源。它们均未连接或操作。

---

## English

`READY`: a small, secure WireGuard user/device configuration manager built with Flask, server-rendered templates, and local SQLite. A local CLI shares exactly the same business rules.

The application only emits deterministic desired peer state. It **cannot** execute `wg`, `wg-quick`, or `syncconf`; live reconciliation requires separate approval and a least-privilege reconciler.

### Features

- Admin user enable/disable, quota, device, installer, and audit management.
- Self-service Windows, macOS, Linux, iOS, and Android device creation.
- Independent keys and a globally unique static tunnel IP per device.
- Immutable configurations: create, reset, or delete only.
- Reset revokes the old public key, creates a new key, and preserves the IP.
- Delete removes the desired peer before immediately releasing the IP.
- One-time `.conf` downloads and one-time mobile QR delivery.
- Installer validation, external binary storage, license attestation, SHA-256 metadata, and verified downloads.

### Security invariants

- Passwords use scrypt hashes; password resets revoke existing sessions.
- Private device keys are never stored in SQLite, logs, snapshots, or desired peer state.
- SQLite enables WAL, foreign keys, full synchronous writes, explicit transactions, and uniqueness constraints.
- POST routes use CSRF protection; login attempts are persistently rate-limited.
- Ordinary users can query only their own devices; installer upload requires admin role.
- Dynamic pages, configuration downloads, installer downloads, and QR responses use `no-store`.
- The file adapter uses canonical JSON, `fsync`, atomic rename, and one `.previous` rollback file.

### Install and run

For deployment next to an existing WireGuard server, follow the [WireGuard Manager installation guide](docs/WG_MANAGER_INSTALL.md). It covers source extraction, non-root operation, existing `wg0` parameters, systemd, tunnel-address access, and observed installation failures.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .

export WG_MANAGER_DATA_DIR=/var/lib/wireguard-manager
export WG_SERVER_PUBLIC_KEY='replace-with-the-server-public-key'
export WG_ENDPOINT='vpn.example.invalid:51820'
export WG_COOKIE_SECURE=1

.venv/bin/wg-manager user create admin --role admin --quota 0
.venv/bin/wg-manager-web
```

Run as a dedicated non-root account. The default bind is `127.0.0.1:8080`; terminate TLS at a local reverse proxy. Replace every placeholder before issuing usable configurations.

### CLI

```sh
.venv/bin/wg-manager --help
.venv/bin/wg-manager user create alice --quota 2
.venv/bin/wg-manager device create alice workstation --type linux --output /secure/path/workstation.conf
.venv/bin/wg-manager device reset DEVICE_ID --output /secure/path/workstation-reset.conf
.venv/bin/wg-manager device delete DEVICE_ID
.venv/bin/wg-manager reconcile
```

Passwords are prompted without echo. Configurations are never printed and can only be written to a caller-selected new file with mode `0600`.

### Validation and boundaries

```sh
.venv/bin/pip install -e '.[test,audit]'
.venv/bin/pytest
.venv/bin/pip-audit --cache-dir /tmp/wg-manager-pip-audit-cache
```

See [ACCEPTANCE.md](ACCEPTANCE.md) for automated and real-browser evidence.

`NOT VERIFIED`: live WireGuard reconciliation and AWS deployment/resources. No live interface or AWS resource has been touched.
