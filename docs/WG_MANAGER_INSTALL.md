# WireGuard Manager 安装与启动 / Installation and Startup

> 中文为主，英文说明见文末。本指南用于在**已经运行 WireGuard Server** 的 Linux 主机上安装 Manager Web/CLI 和实时 Peer reconciler。它不会重新安装 WireGuard，也不会覆盖 `/etc/wireguard/wg0.conf`。

状态 / Status: `READY`（安装步骤与实现） · `NOT VERIFIED`（你的真实服务器验收）
最后核对 / Last verified: **2026-08-02**

## 1. 组件边界

现有 WireGuard Server 继续负责真实隧道；Manager 分成非 root 管理进程和仅接受本机请求的最小权限 reconciler：

```text
Web/CLI (wireguard-manager, non-root)
  -> SQLite transaction + atomic expected-peers.json
  -> /run/wireguard-manager/reconcile.sock
  -> reconciler (root, restricted systemd service)
  -> wg syncconf wg0 -> verify -> rollback on failure
```

实时模式必须使用：

```ini
WG_ADAPTER=reconciler
```

每台设备生成一条独立服务端 Peer，所有托管 Peer 都写入同一个期望状态集合。reconciler 读取在线配置、保留未托管 Peer、只差量同步变化并校验结果；新增、reset、delete、用户禁用/启用不会执行 `restart`，现有无关连接保持运行。Redis 不参与此链路。

Manager 不改写 `/etc/wireguard/wg0.conf`。机器重启时，`wg-quick@wg0` 先加载原配置，reconciler 随后重新应用 Manager 期望状态。已有未托管 Peer 会保留；其隧道 IP 必须写入 `WG_RESERVED_IPS`，避免 Manager 重新分配。若出现地址重叠，reconciler 会拒绝变更而不是覆盖现有 Peer。

reconciler 的 Peer 所有权清单和应用状态保存在 root 控制的 `/var/lib/wireguard-manager-reconciler`，与 Web 可写数据目录分离。Unix Socket 的父目录不可由 Web 用户改名或替换；客户端还会核对对端进程 UID，并使用一次性请求标识，避免把旧状态误认为本次成功。

## 2. 前置检查

确认现有 WireGuard 正常，不要把私钥或带真实公网端点的完整输出复制到日志、聊天或仓库：

```sh
sudo systemctl is-active wg-quick@wg0
sudo wg show wg0
sudo wg show wg0 public-key
sudo wg show wg0 listen-port
ip -o -4 address show dev wg0
python3.11 --version
```

Manager 需要 Python 3.11 或更高版本。

仅安装 Python 运行环境，不需要重新安装 `wireguard-tools`：

Amazon Linux 2023、CentOS Stream 9/10、Rocky Linux 9：

```sh
sudo dnf install -y python3.11 python3.11-pip git curl
```

Ubuntu 24.04 或其他已经提供 Python 3.11+ 的 Ubuntu：

```sh
sudo apt update
sudo apt install -y python3 python3-venv git curl
python3 --version
```

Ubuntu 22.04 的系统 Python 通常低于 3.11；必须先使用组织批准的方式安装 Python 3.11+，不要绕过项目版本门禁。

## 3. 获取并安装源码

### 方法 A：GitHub clone

```sh
git clone https://github.com/niugengtian/wireguard_manager.git /tmp/wireguard-manager-source
git -C /tmp/wireguard-manager-source rev-parse HEAD

sudo install -d -o root -g root -m 0755 /opt/wireguard-manager
git -C /tmp/wireguard-manager-source archive HEAD |
  sudo tar -x -C /opt/wireguard-manager
```

### 方法 B：GitHub 源码压缩包

```sh
curl --fail --location \
  https://github.com/niugengtian/wireguard_manager/archive/refs/heads/main.tar.gz \
  --output /tmp/wireguard-manager-source.tar.gz

sudo install -d -o root -g root -m 0755 /opt/wireguard-manager
sudo tar -xzf /tmp/wireguard-manager-source.tar.gz \
  -C /opt/wireguard-manager \
  --strip-components=1
```

在执行 `pip install` 前必须通过这个门禁：

```sh
sudo test -f /opt/wireguard-manager/pyproject.toml &&
  echo "Manager source directory OK"
```

如果没有输出 `Manager source directory OK`，不要继续安装，参见故障排查。

创建隔离 Python 环境并安装：

```sh
sudo python3.11 -m venv /opt/wireguard-manager/.venv
sudo /opt/wireguard-manager/.venv/bin/python -m pip install --upgrade pip
sudo /opt/wireguard-manager/.venv/bin/pip install \
  --no-cache-dir \
  /opt/wireguard-manager

/opt/wireguard-manager/.venv/bin/wg-manager --help
```

在 Ubuntu 24.04 上可将 `python3.11` 替换为已经确认版本不低于 3.11 的 `python3`。

## 4. 创建非 root 运行身份

```sh
id wireguard-manager >/dev/null 2>&1 ||
  sudo useradd \
    --system \
    --home-dir /var/lib/wireguard-manager \
    --create-home \
    --shell /sbin/nologin \
    wireguard-manager

sudo install -d \
  -o wireguard-manager \
  -g wireguard-manager \
  -m 0700 \
  /var/lib/wireguard-manager
```

Web 和 CLI 都拒绝以 root 运行。SQLite、会话密钥、安装包和期望状态只写入 `/var/lib/wireguard-manager`，不写入源码目录。

## 5. 连接现有 WireGuard 参数

先读取现有**公钥**、监听端口和隧道地址：

```sh
sudo wg show wg0 public-key
sudo wg show wg0 listen-port
ip -o -4 address show dev wg0
```

如果接口地址是 `10.255.77.1/24`，Manager 的池必须写成网络地址 `10.255.77.0/24`，不能写 `10.255.77.1/24`。

选择一个没有被其他进程使用的 Web 端口：

```sh
sudo ss -lntp | grep -E ':(8080|8081)\b' || true
```

创建配置：

```sh
sudoedit /etc/wireguard-manager.env
```

下面是通过 WireGuard 隧道地址直接访问的示例。必须替换公钥、Endpoint、网段和路由；不要把真实 Endpoint 提交到 Git：

```ini
WG_MANAGER_DATA_DIR=/var/lib/wireguard-manager
WG_SERVER_PUBLIC_KEY=REPLACE_WITH_EXISTING_WG0_PUBLIC_KEY
WG_ENDPOINT=REPLACE_WITH_EXISTING_ENDPOINT:51820

WG_TUNNEL_CIDR=10.255.77.0/24
WG_RESERVED_IPS=10.255.77.2
WG_ALLOWED_IPS=10.255.77.0/24,172.31.0.0/16
WG_DNS=
WG_INTERFACE=wg0
WG_ADAPTER=reconciler
WG_RECONCILE_SOCKET=/run/wireguard-manager/reconcile.sock
WG_RECONCILE_STATE_DIR=/var/lib/wireguard-manager-reconciler

WG_WEB_HOST=10.255.77.1
WG_WEB_PORT=8081
WG_COOKIE_SECURE=0
WG_SESSION_MINUTES=30
```

`WG_COOKIE_SECURE=0` 只适用于通过加密 WireGuard 隧道访问的 HTTP 页面。若以后使用 Nginx/Caddy 提供 HTTPS，应改成 `WG_COOKIE_SECURE=1`。

`WG_RESERVED_IPS` 只写既有或由其他系统管理的客户端隧道 IP，多个地址用逗号分隔。`WG_ALLOWED_IPS` 是新客户端配置的默认目标路由，不是服务端 Peer 的地址：服务端始终为每台设备使用唯一 `<static_ip>/32`。客户端路由最多 32 条 IPv4 CIDR；`0.0.0.0/0` 表示全隧道，分流时可写 `10.255.77.0/24,172.31.0.0/16`。

Web 如果仅能经当前 WireGuard 隧道访问，桌面端用户只需点一次“重置并下载”。浏览器完整接收文件后，服务端默认等待 2 秒再热切换公钥；旧隧道随后断开是预期行为，用户直接导入已下载文件。可用 `WG_RESET_ACTIVATION_DELAY_SECONDS=2` 调整 1-30 秒宽限期。二维码 reset 仍需在扫描完成后显式确认。不要在未验证 NAT、转发、DNS 与恢复通道时使用 `0.0.0.0/0`。

锁定配置权限：

```sh
sudo chown root:wireguard-manager /etc/wireguard-manager.env
sudo chmod 0640 /etc/wireguard-manager.env
```

## 6. 创建管理员

```sh
sudo -u wireguard-manager -H /bin/sh -c \
  'set -a; . /etc/wireguard-manager.env; set +a; exec /opt/wireguard-manager/.venv/bin/wg-manager user create admin --role admin --quota 0'
```

密码通过无回显交互输入，不要把密码写进命令行、环境文件或日志。

## 7. 安装 systemd 服务

`pip install` 不会自动注册 systemd 服务。必须同时安装 reconciler 与 Web unit：

```sh
sudo install \
  -o root \
  -g root \
  -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager-reconciler.service.example \
  /etc/systemd/system/wireguard-manager-reconciler.service

sudo install \
  -o root \
  -g root \
  -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager.service.example \
  /etc/systemd/system/wireguard-manager.service

sudo systemctl daemon-reload
sudo systemctl enable --now wireguard-manager-reconciler
sudo systemctl enable --now wireguard-manager
```

验证：

```sh
sudo systemctl is-enabled wireguard-manager-reconciler
sudo systemctl status wireguard-manager-reconciler --no-pager
sudo systemctl is-enabled wireguard-manager
sudo systemctl status wireguard-manager --no-pager
sudo test -S /run/wireguard-manager/reconcile.sock
sudo ss -lntp | grep ':8081'
curl --fail http://10.255.77.1:8081/login
```

两个 unit 的预期状态都是 `enabled`、`active (running)`；Web 由 `wireguard-manager` 用户运行，只有 reconciler 为受限 root 服务。Web unit 依赖 reconciler，因此不会在热更新通道不可用时假装成功运行。

systemd 会创建 `/run/wireguard-manager`（root 控制、组可连接但不可替换 Socket）和 `/var/lib/wireguard-manager-reconciler`（root 控制、组只读状态）。Web 每次启动前会执行一次 `wg-manager reconcile`，用已提交 SQLite 状态修复异常退出留下的跨进程差异，不会重启 `wg0`。

安装完成后，在得到现网变更确认的前提下做最小在线验证：

```sh
sudo -u wireguard-manager -H /bin/sh -c \
  'set -a; . /etc/wireguard-manager.env; set +a; exec /opt/wireguard-manager/.venv/bin/wg-manager reconcile'
sudo wg show wg0 allowed-ips
sudo journalctl -u wireguard-manager-reconciler -n 20 --no-pager
```

实时模式下 `wg-manager reconcile` 返回 `VERIFIED`，代表 reconciler 已完成 `syncconf` 和在线校验；仍建议使用 `wg show` 做操作员复核。文件模式只返回 `READY`，表示期望文件已重建。命令不会重启 `wg0`。

## 8. 从客户端直接访问

客户端 WireGuard 配置的 `AllowedIPs` 必须包含服务端隧道地址，例如：

```ini
AllowedIPs = 10.255.77.0/24, 172.31.0.0/16
```

连接 WireGuard 后直接打开：

```text
http://10.255.77.1:8081/login
```

这种方式不需要 SSM、Nginx，也不需要在 AWS Security Group 中开放 TCP `8081`。应用只绑定 WireGuard 地址，公网网卡不监听该端口。

如果主机 INPUT 策略默认拒绝，需要按实际防火墙工具只允许来自 `wg0` 的 TCP 端口。先检查现状，不要同时混用 iptables、firewalld 和 UFW：

```sh
sudo systemctl is-active firewalld 2>/dev/null || true
sudo ufw status 2>/dev/null || true
sudo iptables -S INPUT
```

临时 iptables 验证规则：

```sh
sudo iptables -C INPUT -i wg0 -p tcp --dport 8081 -j ACCEPT ||
  sudo iptables -I INPUT -i wg0 -p tcp --dport 8081 -j ACCEPT
```

持久化方式取决于服务器现有防火墙管理方案。不要为了 Web 页面新增公网安全组规则。

## 9. 故障排查

### `pyproject.toml` 找不到

错误：

```text
Directory '/opt/wireguard-manager' is not installable.
Neither 'setup.py' nor 'pyproject.toml' found.
```

原因是源码仍在 `/tmp`、解压后多了一层目录，或 clone 后没有复制到 `/opt`。检查：

```sh
sudo find /opt/wireguard-manager -maxdepth 3 -name pyproject.toml -print
tar -tzf /tmp/wireguard-manager-source.tar.gz | head -20
```

重新使用第 3 节的 `--strip-components=1` 解压，并确认 `/opt/wireguard-manager/pyproject.toml` 存在后再运行 pip。

### `Unit wireguard-manager.service not found`

Python 包安装不会自动注册 systemd unit。重新执行第 7 节两个 `sudo install ...service.example` 命令和 `sudo systemctl daemon-reload`。

### 页面新增设备时报 reconciler 不可用

```sh
sudo systemctl status wireguard-manager-reconciler --no-pager
sudo test -S /run/wireguard-manager/reconcile.sock
sudo journalctl -u wireguard-manager-reconciler -n 20 --no-pager
```

重点检查 `wg-quick@wg0` 是否 active、环境文件的 `WG_INTERFACE`、数据目录权限，以及 Web 用户是否属于 `wireguard-manager` 组。请求失败时数据库事务和期望文件会回滚，不会交付一个未上线的设备配置。

### 新设备与已有 Peer 地址冲突

把所有已有未托管 Peer 的隧道 IP 写入 `/etc/wireguard-manager.env` 的 `WG_RESERVED_IPS`，然后重启两个 Manager unit。不要把一个现有私钥导入数据库；Manager 只保存其自己生成设备的公钥。

### 页面显示 Python `HTTPStatus.NOT_FOUND`

如果页面内容是：

```text
Error code: 404
Message: File not found.
```

这不是 Manager 的错误页面，通常表示该端口已经被 `python -m http.server` 或其他服务占用：

```sh
sudo ss -lntp | grep ':8080'
sudo systemctl status wireguard-manager --no-pager
sudo journalctl -u wireguard-manager -n 20 --no-pager
```

不要直接终止未知进程。选择空闲端口，例如 `8081`，修改环境文件并重启 Manager。

### 服务启动失败

```sh
sudo systemctl status wireguard-manager --no-pager
sudo journalctl -u wireguard-manager -n 20 --no-pager
```

重点检查：运行用户、环境文件权限、Python 可执行文件、严格的网络 CIDR、绑定地址是否存在以及端口是否被占用。日志只查看最近 20 行，避免无界输出。

### 服务器本机可访问，客户端不可访问

检查：

1. 客户端 WireGuard 已连接；
2. 客户端 `AllowedIPs` 包含服务端隧道地址；
3. Manager 监听的是 `wg0` 地址而不是公网地址；
4. 主机防火墙允许从 `wg0` 访问 Web 端口。

## 10. 更新与回滚

更新前先记录当前 commit，并备份数据库和期望状态；备份目录必须在源码外且权限为 `0700`：

```sh
git -C /tmp/wireguard-manager-source rev-parse HEAD
sudo systemctl stop wireguard-manager
sudo systemctl stop wireguard-manager-reconciler
sudo cp -a /var/lib/wireguard-manager /var/lib/wireguard-manager.rollback
sudo cp -a /var/lib/wireguard-manager-reconciler /var/lib/wireguard-manager-reconciler.rollback
```

安装新源码后重新运行 pip 并启动：

```sh
sudo /opt/wireguard-manager/.venv/bin/pip install \
  --no-cache-dir \
  /opt/wireguard-manager
sudo systemctl start wireguard-manager-reconciler
sudo systemctl start wireguard-manager
sudo systemctl status wireguard-manager-reconciler --no-pager
sudo systemctl status wireguard-manager --no-pager
```

停止或升级 Manager 服务不会停止 `wg0`，已有隧道继续运行。回滚时恢复已记录的旧 commit 和数据库备份，再依次启动 reconciler 和 Web。不要回滚或覆盖 `/etc/wireguard/wg0.conf`，因为 Manager 不拥有该文件。

---

## English summary

This guide installs WireGuard Manager and its live reconciler next to an already running WireGuard server. It does not reinstall WireGuard, restart the interface, or modify `wg0.conf`.

1. Confirm `wg-quick@wg0` is active and record only the existing server public key, listen port, tunnel network, endpoint, and client routes.
2. Install Python 3.11+, extract the repository so `/opt/wireguard-manager/pyproject.toml` exists, create a virtual environment, and install the package.
3. Reserve every existing unmanaged tunnel IP with `WG_RESERVED_IPS`, set `WG_ADAPTER=reconciler`, and keep client-route defaults in `WG_ALLOWED_IPS`.
4. Install both systemd units. Web/CLI run as non-root; the restricted reconciler alone calls `wg syncconf` through an authenticated local Unix socket. Root-owned reconciler metadata is separated from the Web-writable data directory.
5. Device create/reset/delete and user disable/enable synchronously apply and verify live peer state. Unrelated peers and sessions are preserved; failures roll back.
6. Configure `WG_WEB_HOST` with the existing server tunnel address and choose an unused port. Tunnel-only HTTP uses `WG_COOKIE_SECURE=0`; HTTPS requires `WG_COOKIE_SECURE=1`.
7. Ensure the client `AllowedIPs` contains the server tunnel network, then browse directly to the tunnel address. No SSM, reverse proxy, or public cloud firewall rule is required for tunnel-only access.

Troubleshooting rules:

- Missing `pyproject.toml`: the source was not extracted at the expected directory level.
- Missing systemd units: install both templates under `deploy/` into `/etc/systemd/system/` and run `daemon-reload`.
- Generic Python `File not found` 404: another process owns the port; inspect it and choose an unused port.
- Local curl works but the client cannot connect: check client routes and the host firewall on `wg0`.

`NOT VERIFIED`: deployment and live acceptance on your server. Automated tests verify the reconciler behavior without touching a real interface.
