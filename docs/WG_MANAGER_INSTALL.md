# WireGuard Manager 安装与启动 / Installation and Startup

> 中文为主，英文说明见文末。本指南用于在**已经运行 WireGuard Server** 的 Linux 主机上安装 Manager Web/CLI。它不会重新安装 WireGuard，也不会覆盖 `/etc/wireguard/wg0.conf`。

状态 / Status: `READY`（安装步骤） · `NOT VERIFIED`（真实 Peer 自动同步）
最后核对 / Last verified: **2026-08-02**

## 1. 组件边界

现有 WireGuard Server 继续负责真实隧道；Manager 是独立的 Python Web/CLI 与 SQLite 应用：

```text
WireGuard Server: wg0、握手、路由、真实 Peer
WireGuard Manager: 用户、设备、配额、密钥生成、静态 IP、期望 Peer 状态
```

当前必须使用：

```ini
WG_ADAPTER=file
```

该模式只原子写入 `$WG_MANAGER_DATA_DIR/expected-peers.json`，不会执行 `wg`、`wg-quick` 或 `syncconf`，因此安装 Manager 不会中断现有隧道。

已有 Peer 不会自动导入 Manager。正式创建设备前，必须先保留或导入现有静态 IP；否则空数据库可能重新分配正在使用的地址。

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
WG_ALLOWED_IPS=10.255.77.0/24,172.31.0.0/16
WG_DNS=
WG_INTERFACE=wg0
WG_ADAPTER=file

WG_WEB_HOST=10.255.77.1
WG_WEB_PORT=8081
WG_COOKIE_SECURE=0
WG_SESSION_MINUTES=30
```

`WG_COOKIE_SECURE=0` 只适用于通过加密 WireGuard 隧道访问的 HTTP 页面。若以后使用 Nginx/Caddy 提供 HTTPS，应改成 `WG_COOKIE_SECURE=1`。

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

`pip install` 不会自动注册系统服务。项目只提供经过加固的 unit 模板，必须安装一次：

```sh
sudo install \
  -o root \
  -g root \
  -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager.service.example \
  /etc/systemd/system/wireguard-manager.service

sudo systemctl daemon-reload
sudo systemctl enable --now wireguard-manager
```

验证：

```sh
sudo systemctl is-enabled wireguard-manager
sudo systemctl status wireguard-manager --no-pager
sudo ss -lntp | grep ':8081'
curl --fail http://10.255.77.1:8081/login
```

预期状态是 `enabled`、`active (running)`，并由 Waitress 监听配置的 WireGuard 地址。

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

Python 包安装不会自动注册 systemd unit。重新执行第 7 节的 `sudo install ...service.example` 和 `sudo systemctl daemon-reload`。

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
sudo cp -a /var/lib/wireguard-manager /var/lib/wireguard-manager.rollback
```

安装新源码后重新运行 pip 并启动：

```sh
sudo /opt/wireguard-manager/.venv/bin/pip install \
  --no-cache-dir \
  /opt/wireguard-manager
sudo systemctl start wireguard-manager
sudo systemctl status wireguard-manager --no-pager
```

回滚时恢复已记录的旧 commit 和数据库备份。不要回滚或覆盖 `/etc/wireguard/wg0.conf`，因为 Manager 当前不拥有该文件。

---

## English summary

This guide installs WireGuard Manager next to an already running WireGuard server. It does not reinstall WireGuard or modify `wg0.conf`.

1. Confirm `wg-quick@wg0` is active and record only the existing server public key, listen port, tunnel network, endpoint, and client routes.
2. Install Python 3.11+, extract the repository so `/opt/wireguard-manager/pyproject.toml` exists, create a virtual environment, and install the package.
3. Run the application as the dedicated `wireguard-manager` system user with data under `/var/lib/wireguard-manager`.
4. Configure `WG_WEB_HOST` with the existing server tunnel address and choose an unused port. Tunnel-only HTTP uses `WG_COOKIE_SECURE=0`; HTTPS requires `WG_COOKIE_SECURE=1`.
5. Install the provided systemd template manually; Python package installation does not register the service.
6. Ensure the client `AllowedIPs` contains the server tunnel network, then browse directly to the tunnel address. No SSM, reverse proxy, or public cloud firewall rule is required for tunnel-only access.
7. Keep `WG_ADAPTER=file`. Existing peers are not imported automatically, so reserve/import their IP addresses before issuing new devices.

Troubleshooting rules:

- Missing `pyproject.toml`: the source was not extracted at the expected directory level.
- Missing systemd unit: install `deploy/wireguard-manager.service.example` into `/etc/systemd/system/` and run `daemon-reload`.
- Generic Python `File not found` 404: another process owns the port; inspect it and choose an unused port.
- Local curl works but the client cannot connect: check client routes and the host firewall on `wg0`.

`NOT VERIFIED`: automatic live peer reconciliation. The current file adapter deliberately leaves the existing WireGuard interface unchanged.
