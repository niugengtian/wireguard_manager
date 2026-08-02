# WireGuard Manager｜WireGuard 用户与设备配置管理

[中文](#中文说明) · [English](#english) · [无 Compose 容器部署 / Docker run](docs/DOCKER_DEPLOYMENT.md) · [端到端运维手册 / Operations runbook](docs/OPERATIONS_RUNBOOK.md) · [Manager 安装指南 / Manager install](docs/WG_MANAGER_INSTALL.md) · [服务端部署指南 / Server guide](docs/WIREGUARD_SERVER_GUIDE.md) · [验收记录 / Acceptance](ACCEPTANCE.md)

## 中文说明

`READY`：一个小型、安全、可运行的 WireGuard 用户与设备管理站点。技术栈只有 Python、Flask、服务端模板和本地 SQLite，同时提供使用同一套业务规则的本机 CLI。

Web 与 CLI 的设备新增、reset、delete，以及用户禁用/启用，都先原子重建期望状态，再通过独立 Unix Socket 请求最小权限 reconciler 执行 `wg syncconf`、在线校验并在失败时回滚。它不会执行 `wg-quick restart`，不会改写 `/etc/wireguard/wg0.conf`，也不需要 Redis；系统重启时 reconciler 会在 `wg0` 启动后重新应用期望 Peer。

`AllowedIPs` 可以有多条，使用英文逗号分隔，例如 `10.255.77.0/24, 172.31.0.0/16`。`0.0.0.0/0` 只代表全 IPv4 隧道；它不是数量限制。系统最多接受 32 条严格 IPv4 CIDR，并自动去重和合并被更大网段覆盖的条目。

### 能做什么

- 管理员创建、启停用户，设置设备配额，查看或删除设备和审计记录；禁用用户会立即从在线接口撤销其全部托管 Peer，重新启用会恢复。
- 用户登录后按 Windows、macOS、Linux、iOS、Android 类型自助新增设备。
- 每台设备使用独立密钥和全局唯一静态隧道 IP；超过配额会被拒绝。
- 配置不可编辑，只能新增、`reset` 或 `delete`。
- 桌面端 Web `reset` 先完整下载新配置，响应交付后再自动热切换公钥并撤销旧 Peer；静态 IP 默认保留。二维码 reset 在人工扫描完成后再显式确认。
- `delete` 先从期望状态和在线接口移除设备并验证，再释放 IP；旧公钥仍然无效。
- 每台设备对应一条独立服务端 Peer，服务端 `AllowedIPs` 固定为唯一隧道 IP `/32`；客户端 `AllowedIPs` 可按设备设置。
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

#### 容器方式（不使用 Compose）

WireGuard Server 直接使用固定版本的 LinuxServer 现成镜像；Web/CLI 和 reconciler 共用一个 `wireguard-manager:0.3.0` 镜像、分别以两个角色运行。部署只引用 `docker/wireguard-server.env.example`、`docker/manager.env.example` 两个配置文件，再执行三条 `docker run` 命令。详见 [无 Compose 的容器部署指南](docs/DOCKER_DEPLOYMENT.md)。

三个容器共享同一个 WireGuard 网络命名空间，因此只有一个 `wg0` 和一个 UDP 监听端口；Manager 页面仅监听隧道地址，不必向宿主机公网发布 `8081`。现有原生 WireGuard Server 的自动迁移尚未验证，不应直接与容器版本同时运行。

#### 原生 Python/systemd 方式

从空服务器开始时，按 [WireGuard + wg-manager 端到端运维手册](docs/OPERATIONS_RUNBOOK.md) 执行；已经运行 WireGuard 时，可直接使用 [WireGuard Manager 安装与启动指南](docs/WG_MANAGER_INSTALL.md)。手册记录了三个组件的安装顺序、源码包校验、非 root 运行、systemd、日常操作、升级回滚和本次真实排错经验。

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
| `WG_TUNNEL_CIDR` | `10.44.0.0/24` | 客户端静态 IP 池；可使用最大 `/16` 支持更多 Peer |
| `WG_RESERVED_IPS` | 空 | 逗号分隔的既有/外部 Peer 隧道 IP，Manager 永不分配 |
| `WG_SERVER_PUBLIC_KEY` | 非生产占位值 | 写入客户端配置的服务端公钥 |
| `WG_ENDPOINT` | `vpn.example.invalid:51820` | 客户端连接端点 |
| `WG_DNS` | 空 | 可选客户端 DNS；仅在确实提供 DNS 服务时设置 |
| `WG_ALLOWED_IPS` | 与 `WG_TUNNEL_CIDR` 相同 | 新设备默认客户端分流路由；可按设备覆盖 |
| `WG_ADAPTER` | `file` | 生产实时模式设为 `reconciler`；`file`/`dry-run` 只用于离线验证 |
| `WG_RECONCILE_SOCKET` | `/run/wireguard-manager/reconcile.sock` | Web/CLI 与 root reconciler 的本机 Unix Socket |
| `WG_RECONCILE_STATE_DIR` | `/var/lib/wireguard-manager-reconciler` | root reconciler 独占的 Peer 所有权清单和带请求标识的应用状态 |
| `WG_RESET_ACTIVATION_DELAY_SECONDS` | `2` | `.conf` 完整交付后自动撤销旧 Peer 前的宽限秒数，范围 1-30 |
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
.venv/bin/wg-manager device create alice workstation --type linux --allowed-ips '10.255.77.0/24,172.31.0.0/16' --output /secure/path/workstation.conf
.venv/bin/wg-manager device list --username alice
.venv/bin/wg-manager device allowed-ips DEVICE_ID --set '10.0.0.0/8,172.31.0.0/16'
.venv/bin/wg-manager device reset DEVICE_ID --output /secure/path/workstation-reset.conf
.venv/bin/wg-manager device delete DEVICE_ID

# 在 reconciler 模式下重建、热应用并校验全部托管 Peer
.venv/bin/wg-manager reconcile
```

客户端 `AllowedIPs` 决定该设备把哪些目标网段送入隧道。修改已保存策略后必须 reset 才能生成并一次性交付新客户端配置；服务器无法远程改写已经导入客户端的文件。服务端每个 Peer 的 `AllowedIPs = <该设备静态隧道 IP>/32` 用于身份与回程路由，不能拿它替代目的网段访问控制；如需限制某设备能访问的内部目标，应按来源隧道 IP 配置 nftables/防火墙策略。

Web 仅通过当前 WireGuard 隧道访问时，桌面端 reset 只需点一次“重置并下载”：请求先交付完整配置，响应关闭后等待短暂宽限期，再通过 reconciler 热切换 Peer。SQLite 期间只暂存新公钥，任何时候都不保存新私钥。CLI `device reset` 面向服务器本机或带独立恢复通道的管理员，仍会立即撤销旧 Peer。

`0.0.0.0/0` 会接管全部 IPv4 流量，Windows 客户端还可能应用 kill-switch 行为。只有在服务端转发、NAT、DNS 和独立恢复通道都已验证时才显式配置全隧道；否则使用 `10.255.77.0/24,172.31.0.0/16` 这类分流范围。

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

自动化验收覆盖 quota=2、一次性配置、超额拒绝、reset 撤销旧公钥并保留 IP、delete 移除 Peer 并复用 IP、并发唯一 IP、对象授权、安装包 SHA-256、CSRF、登录限速、CLI `0600` 文件、敏感值不落盘，以及 300 个托管 Peer 热应用、未托管 Peer 保留、失败回滚、Web/CLI 同步等待在线应用。真实浏览器验收见 [ACCEPTANCE.md](ACCEPTANCE.md)。

`NOT VERIFIED`：你的真实 WireGuard 服务器部署与 AWS 资源；本项目没有连接或操作它们。

---

## English

`READY`: a small, secure WireGuard user/device configuration manager built with Flask, server-rendered templates, and local SQLite. A local CLI shares exactly the same business rules.

Web and CLI mutations atomically rebuild desired state and ask a separate least-privilege reconciler over a local Unix socket to run `wg syncconf`, verify the live interface, and roll back on failure. No interface restart, Redis, or rewrite of `/etc/wireguard/wg0.conf` is required.

Client `AllowedIPs` accepts up to 32 comma-separated IPv4 CIDRs. `0.0.0.0/0` means full-tunnel IPv4; it does not mean only one entry is supported. Redundant routes are normalized and collapsed.

### Features

- Admin user enable/disable, quota, device, installer, and audit management.
- Self-service Windows, macOS, Linux, iOS, and Android device creation.
- Independent keys and a globally unique static tunnel IP per device.
- One independent server peer per device, with a unique tunnel `/32`; per-device client `AllowedIPs` policies.
- Immutable configurations: create, reset, or delete only.
- Desktop Web reset fully delivers the replacement and then automatically hot-swaps the key after a short grace period. QR reset keeps explicit post-scan confirmation. The static IP is preserved, while CLI reset remains immediate for local/out-of-band administration.
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

For containers, use the existing pinned LinuxServer WireGuard image and reuse one `wireguard-manager:0.3.0` image for separate Manager and reconciler roles. No Compose file is required; see [Docker run deployment without Compose](docs/DOCKER_DEPLOYMENT.md). The three containers share one network namespace, one `wg0`, and one UDP listening port.

For a native Python/systemd installation:

For a complete server-to-Manager deployment, follow the [end-to-end operations runbook](docs/OPERATIONS_RUNBOOK.md). For deployment next to an existing WireGuard server, follow the [WireGuard Manager installation guide](docs/WG_MANAGER_INSTALL.md). They cover source verification, non-root operation, systemd, upgrades, rollback, tunnel-address access, and the installation failures observed during testing.

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
.venv/bin/wg-manager device create alice workstation --type linux --allowed-ips '10.255.77.0/24,172.31.0.0/16' --output /secure/path/workstation.conf
.venv/bin/wg-manager device allowed-ips DEVICE_ID --set '10.0.0.0/8,172.31.0.0/16'
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

Changing client `AllowedIPs` requires a reset to deliver a new one-time client configuration; a server cannot remotely rewrite a configuration already imported by a client. Server peer `AllowedIPs` remains the device's unique tunnel `/32`. Destination authorization belongs in a firewall policy keyed by the source tunnel IP.

Desktop Web reset is one click: the response fully delivers the replacement, then a response-close hook waits a short grace period and automatically hot-swaps the peer. SQLite stores the pending public key only; the private key is still delivered once and never persisted. `0.0.0.0/0` is an explicit full-tunnel choice and may cut off local or remote-management traffic unless forwarding, NAT, DNS, and recovery access are already verified. The default is the configured tunnel CIDR.

`NOT VERIFIED`: deployment on your live WireGuard server and AWS resources. No live interface or AWS resource has been touched.
