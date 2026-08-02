# WireGuard + wg-manager 端到端部署与运维手册 / End-to-End Operations Runbook

> 中文为主，英文摘要随后。本手册覆盖 WireGuard Server、`wireguard-manager` Web/CLI 和 `wireguard-manager-reconciler` 三个组件。
> Chinese is authoritative; concise English notes follow. This runbook covers the WireGuard server, Manager Web/CLI, and the least-privilege reconciler.

状态 / Status: `READY` 实现与文档 · `NOT VERIFIED` 用户真实服务器当前运行状态

最后核对 / Last verified: **2026-08-03**

组件深入文档 / Component references:

- [WireGuard Server 部署与操作](WIREGUARD_SERVER_GUIDE.md)
- [WireGuard Manager 安装与启动](WG_MANAGER_INSTALL.md)
- [验收记录](../ACCEPTANCE.md)

## 1. 先理解三个组件 / Component ownership

| 组件 | systemd unit | 运行身份 | 网络入口 | 拥有的数据 | 停止影响 |
| --- | --- | --- | --- | --- | --- |
| WireGuard Server | `wg-quick@wg0` | root/kernel | UDP `51820` 示例 | `/etc/wireguard/wg0.conf` 和服务端私钥 | 停止/重启会中断所有隧道 |
| Manager Web + CLI | `wireguard-manager` | `wireguard-manager` 非 root | TCP `8081` 示例，绑定 `wg0` 地址 | SQLite、安装包、期望 Peer JSON | 已在线 WireGuard Peer 仍留在内核 |
| Reconciler | `wireguard-manager-reconciler` | 受限 root | 仅 Unix Socket，不监听 TCP/UDP | root 独占 Peer 所有权清单和回执 | 已在线 Peer 继续工作，但新变更会被拒绝 |

Manager 与 reconciler 分成两个服务，是为了让 Web 永远不拥有 root/CAP_NET_ADMIN，同时仍能通过受控 Unix Socket 热更新 `wg0`。Reconciler 不会为每个 Peer 打开新端口；所有 Peer 共用一个 WireGuard UDP 端口。

The split keeps the Web process unprivileged. All peers share one WireGuard interface and UDP listener; the reconciler exposes only a local Unix socket.

## 2. 版本基线与安全边界 / Version and security baseline

每次发布至少记录三个版本，不要只写“最新”：

```sh
wg --version
/opt/wireguard-manager/.venv/bin/wg-manager --version
/opt/wireguard-manager/.venv/bin/python -m pip show wireguard-manager
```

客户端版本在 Windows/macOS/Linux/iOS/Android 官方客户端内人工记录。发布台账建议保存：WireGuard Server 工具版本、客户端版本、Manager 版本、Git commit、源码 tar SHA-256 和操作日期。

不得写入日志、Git、测试快照或聊天的内容：

- WireGuard 私钥、完整客户端配置、明文密码；
- 未脱敏的真实公网 Endpoint/IP；
- `/etc/wireguard/wg0.key`、Manager session secret 和 SQLite 原始文件。

Private keys, passwords, complete configurations, and unredacted public endpoints must never enter logs, Git, tests, or chat.

## 3. 阶段 A：部署 WireGuard Server / Stage A: WireGuard server

### 3.1 规划网段

示例只用占位值：

- 服务端接口：`wg0`
- UDP 端口：`51820`
- 隧道网段：`10.44.0.0/24`
- 服务端隧道 IP：`10.44.0.1/24`
- 客户端起始 IP：`10.44.0.2/32`

必须避开 VPC、公司办公网、家庭网、Docker/Kubernetes 网段重叠。

### 3.2 安装 WireGuard

Amazon Linux 2023：

```sh
sudo dnf update -y
sudo dnf install -y wireguard-tools iptables-nft
```

Ubuntu 22.04/24.04：

```sh
sudo apt update
sudo apt install -y wireguard iptables
```

CentOS Stream 9/10 与 Rocky Linux 9：

```sh
sudo dnf info wireguard-tools || true
sudo dnf install -y epel-release
sudo dnf install -y wireguard-tools iptables
```

Rocky Linux 8 需 EPEL + ELRepo 兼容路径，只在组织允许第三方仓库时使用，详见 [WireGuard Server 指南](WIREGUARD_SERVER_GUIDE.md#rocky-linux-8-兼容路径--rocky-linux-8-compatibility-path)。

```sh
wg --version
sudo modprobe wireguard
```

### 3.3 生成服务端密钥

```sh
sudo install -d -m 700 -o root -g root /etc/wireguard
sudo sh -c 'umask 077; wg genkey > /etc/wireguard/wg0.key; wg pubkey < /etc/wireguard/wg0.key > /etc/wireguard/wg0.pub'
sudo chmod 600 /etc/wireguard/wg0.key
sudo chmod 644 /etc/wireguard/wg0.pub
```

只有下面的公钥可写入 Manager 环境文件：

```sh
sudo cat /etc/wireguard/wg0.pub
```

### 3.4 转发、NAT 与 `wg0.conf`

```sh
printf '%s\n' 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/90-wireguard-forward.conf
sudo sysctl --system
sudo sysctl net.ipv4.ip_forward
ip route show default
```

使用 `sudoedit /etc/wireguard/wg0.conf`，将 `PUBLIC_INTERFACE` 替换为真实出口网卡：

```ini
[Interface]
Address = 10.44.0.1/24
ListenPort = 51820
SaveConfig = false
PostUp = wg set %i private-key /etc/wireguard/%i.key
PostUp = iptables -A FORWARD -i %i -j ACCEPT
PostUp = iptables -A FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -s 10.44.0.0/24 -o PUBLIC_INTERFACE -j MASQUERADE
PreDown = iptables -D FORWARD -i %i -j ACCEPT
PreDown = iptables -D FORWARD -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PreDown = iptables -t nat -D POSTROUTING -s 10.44.0.0/24 -o PUBLIC_INTERFACE -j MASQUERADE
```

Manager 接管动态 Peer 后，`wg0.conf` 可保留已有未托管 Peer；它们的隧道 IP 必须加入 `WG_RESERVED_IPS`。Manager 不会改写 `wg0.conf`。

```sh
sudo chown root:root /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo wg-quick strip wg0 >/dev/null
sudo systemctl enable --now wg-quick@wg0
sudo systemctl status wg-quick@wg0 --no-pager
sudo wg show wg0
```

主机防火墙和云/上游防火墙需人工允许 UDP `51820`。本项目不创建或修改 AWS Security Group、NACL、路由表、EIP 或任何 AWS 资源。

## 4. 阶段 B：从本地上传并安装 Manager / Stage B: Install Manager

### 4.1 本地上传私有仓库发布包

在 Mac/管理机：

```sh
cd "/path/to/release-directory"
shasum -a 256 wireguard-manager-RELEASE.tar.gz \
  > wireguard-manager-RELEASE.tar.gz.sha256
cat wireguard-manager-RELEASE.tar.gz.sha256
scp wireguard-manager-RELEASE.tar.gz \
    wireguard-manager-RELEASE.tar.gz.sha256 \
    SERVER_USER@SERVER_ADDRESS:/tmp/
```

在服务器：

```sh
cd /tmp
sha256sum -c wireguard-manager-RELEASE.tar.gz.sha256
sudo tar --no-same-owner -xzf wireguard-manager-RELEASE.tar.gz -C /opt
sudo test -f /opt/wireguard-manager/pyproject.toml
```

源码 tar 固定使用 `wireguard-manager/` 前缀，因此解压到 `/opt` 后的正确路径是 `/opt/wireguard-manager/pyproject.toml`。

### 4.2 建立运行身份与数据目录

```sh
getent group wireguard-manager >/dev/null || sudo groupadd --system wireguard-manager
id wireguard-manager >/dev/null 2>&1 || sudo useradd \
  --system \
  --gid wireguard-manager \
  --home-dir /var/lib/wireguard-manager \
  --create-home \
  --shell /sbin/nologin \
  wireguard-manager

sudo install -d -o wireguard-manager -g wireguard-manager -m 0700 \
  /var/lib/wireguard-manager
sudo install -d -o root -g wireguard-manager -m 0750 \
  /var/lib/wireguard-manager-reconciler
```

### 4.3 创建 venv 并以 root 安装代码

```sh
sudo python3.11 -m venv /opt/wireguard-manager/.venv
sudo /opt/wireguard-manager/.venv/bin/python -m pip install --upgrade pip
sudo /opt/wireguard-manager/.venv/bin/python -m pip install \
  --no-cache-dir \
  /opt/wireguard-manager

/opt/wireguard-manager/.venv/bin/wg-manager --version
```

`/opt/wireguard-manager` 和 venv 保持 root 所有；Web 进程只读代码。安装时使用 root 不等于 Web 以 root 运行。不要执行 `chown -R wireguard-manager /opt/wireguard-manager`。

## 5. 配置 Manager / Configure Manager

```sh
sudoedit /etc/wireguard-manager.env
```

```ini
WG_MANAGER_DATA_DIR=/var/lib/wireguard-manager
WG_SERVER_PUBLIC_KEY=REPLACE_WITH_WG0_PUBLIC_KEY
WG_ENDPOINT=REPLACE_WITH_ENDPOINT:51820

WG_TUNNEL_CIDR=10.44.0.0/24
WG_RESERVED_IPS=10.44.0.2
WG_ALLOWED_IPS=10.44.0.0/24,172.31.0.0/16
WG_DNS=

WG_INTERFACE=wg0
WG_ADAPTER=reconciler
WG_RECONCILE_SOCKET=/run/wireguard-manager/reconcile.sock
WG_RECONCILE_STATE_DIR=/var/lib/wireguard-manager-reconciler
WG_RECONCILE_TIMEOUT_SECONDS=5
WG_RESET_ACTIVATION_DELAY_SECONDS=2

WG_WEB_HOST=10.44.0.1
WG_WEB_PORT=8081
WG_COOKIE_SECURE=0
WG_SESSION_MINUTES=30
```

```sh
sudo chown root:wireguard-manager /etc/wireguard-manager.env
sudo chmod 0640 /etc/wireguard-manager.env
```

关键区分：

- `WG_TUNNEL_CIDR`：Manager 静态 IP 地址池；
- `WG_RESERVED_IPS`：已有/未托管 Peer 占用的隧道 IP；
- `WG_ALLOWED_IPS`：新客户端默认目标路由，不是服务端 Peer 的地址；
- 服务端每个 Peer 永远是该设备唯一 `<static_ip>/32`；
- `WG_WEB_HOST` 可直接绑定服务端 `wg0` IP，此时只有进入隧道后才能访问。

`WG_COOKIE_SECURE=0` 只表示浏览器通过 WireGuard 加密隧道内的 HTTP 访问 Manager。以后在 Nginx/Caddy 终止 HTTPS 时改为 `1`。它不影响 WireGuard UDP 加密。

## 6. 创建管理员 / Create the first admin

```sh
sudo -u wireguard-manager -H /bin/bash -c '
set -a
source /etc/wireguard-manager.env
set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager \
  user create admin --role admin --quota 0
'
```

密码使用无回显交互输入。不要把密码放进 shell 参数或环境文件。

## 7. 阶段 C：安装 reconciler 与 Web systemd / Stage C: systemd services

```sh
sudo install -o root -g root -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager-reconciler.service.example \
  /etc/systemd/system/wireguard-manager-reconciler.service

sudo install -o root -g root -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager.service.example \
  /etc/systemd/system/wireguard-manager.service

sudo systemctl daemon-reload
sudo systemctl enable wireguard-manager-reconciler wireguard-manager
sudo systemctl start wireguard-manager-reconciler
sudo systemctl start wireguard-manager
```

启动顺序必须是：

```text
wg-quick@wg0
  -> wireguard-manager-reconciler
  -> wireguard-manager Web
```

验证：

```sh
sudo systemctl status wg-quick@wg0 --no-pager
sudo systemctl status wireguard-manager-reconciler --no-pager
sudo systemctl status wireguard-manager --no-pager
sudo ss -xlpn | grep reconcile.sock || true
sudo wg show wg0
/opt/wireguard-manager/.venv/bin/wg-manager --version
```

Web 服务由 `wireguard-manager` 非 root 用户运行。Reconciler 是受 systemd sandbox 限制的 root 服务，只允许 AF_UNIX/AF_NETLINK 和必要的 `CAP_NET_ADMIN`/`CAP_DAC_OVERRIDE`。

### 7.1 可选 Nginx HTTPS / Optional TLS proxy

仅在 WireGuard 隧道内访问 `http://10.44.0.1:8081` 时不必部署 Nginx。如果要用域名/HTTPS，把 Web 改为只监听 `127.0.0.1:8081`，并设置 `WG_COOKIE_SECURE=1`。Nginx 最小代理段如下：

```nginx
location / {
    proxy_pass http://127.0.0.1:8081;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
}
```

TLS 证书、`listen 443 ssl` 和防火墙由现有证书/网络方案管理。不要让 Nginx 记录请求体，也不要缓存配置下载或二维码响应。

## 8. 日常操作 / Routine operations

### 8.1 WireGuard Server

```sh
sudo wg show wg0
ip address show dev wg0
sudo systemctl status wg-quick@wg0 --no-pager
sudo journalctl -u wg-quick@wg0 -n 20 --no-pager
```

尚未启用 Manager 时，可手工热应用已验证的 `wg0.conf`：

```sh
sudo wg-quick strip wg0 >/dev/null
sudo bash -c 'wg syncconf wg0 <(wg-quick strip wg0)'
sudo wg show wg0
```

启用 Manager 后，不要直接用 `wg-quick strip wg0` 的结果覆盖在线接口，因为基础 `wg0.conf` 不包含 Manager 动态 Peer，会临时撤销它们。动态 Peer 变更只用 `wg-manager reconcile`。

日常 Peer 变更不要执行 `systemctl restart wg-quick@wg0`；restart 会中断全部现有隧道。

### 8.2 Manager Web/CLI

```sh
sudo -u wireguard-manager -H /bin/bash -c '
set -a; source /etc/wireguard-manager.env; set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager user list
'

sudo -u wireguard-manager -H /bin/bash -c '
set -a; source /etc/wireguard-manager.env; set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager device list
'
```

创建设备（输出文件必须不存在）：

```sh
sudo -u wireguard-manager -H /bin/bash -c '
set -a; source /etc/wireguard-manager.env; set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager \
  device create alice laptop --type linux \
  --allowed-ips "10.44.0.0/24,172.31.0.0/16" \
  --output /var/lib/wireguard-manager/alice-laptop.conf
'
```

CLI 生成的配置权限是 `0600`，不会打印到终端。交付后删除服务器上的临时副本。

修改客户端路由范围：

```sh
sudo -u wireguard-manager -H /bin/bash -c '
set -a; source /etc/wireguard-manager.env; set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager \
  device allowed-ips DEVICE_ID \
  --set "10.44.0.0/24,172.31.0.0/16"
'
```

Web 桌面端 reset 只需点一次“重置并下载”：浏览器先完整接收新配置，然后自动确认；服务端再热切换新公钥并撤销旧 Peer。二维码 reset 在扫描完成后显式确认。

CLI reset 会立即撤销旧 Peer，只应在服务器本机或另有 SSH/控制台/其他 Peer 恢复通道时使用：

```sh
sudo -u wireguard-manager -H /bin/bash -c '
set -a; source /etc/wireguard-manager.env; set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager \
  device reset DEVICE_ID \
  --output /var/lib/wireguard-manager/device-recovery.conf
'
```

### 8.3 Reconciler

手工重建期望状态、热应用并校验：

```sh
sudo -u wireguard-manager -H /bin/bash -c '
set -a; source /etc/wireguard-manager.env; set +a
exec /opt/wireguard-manager/.venv/bin/wg-manager reconcile
'
```

成功时 CLI 输出 `VERIFIED` 和状态 SHA-256。失败时不要重启 `wg0`，先查看最后 20 行 reconciler 日志。

```sh
sudo journalctl -u wireguard-manager-reconciler -n 20 --no-pager -o cat
```

### 8.4 客户端下载 / Client downloads

官方统一入口是 [WireGuard Installation](https://www.wireguard.com/install/)：Windows 使用官方安装程序，macOS/iOS 使用 App Store，Android 使用 Play Store 或官方 APK，Linux 使用发行版包管理器。

Manager 的“客户端安装包”页只显示管理员已上传且记录 SHA-256 的文件。上传前必须先核对再分发许可；没有明确权利时只给官方下载链接。Clash Verge/Mihomo 共存仅作为 Windows/macOS/Linux 桌面端策略边界，不承诺 iOS/Android 双 VPN/TUN 并行。

### 8.5 备份

备份 Manager 时只停两个 Manager 服务，不停 `wg0`：

```sh
sudo test ! -e /var/backups/wireguard-manager/pre-RELEASE
sudo systemctl stop wireguard-manager
sudo systemctl stop wireguard-manager-reconciler
sudo install -d -o root -g root -m 0700 \
  /var/backups/wireguard-manager/pre-RELEASE
sudo cp -a /var/lib/wireguard-manager \
  /var/backups/wireguard-manager/pre-RELEASE/manager-data
sudo cp -a /var/lib/wireguard-manager-reconciler \
  /var/backups/wireguard-manager/pre-RELEASE/reconciler-state
sudo cp -a /etc/wireguard-manager.env \
  /var/backups/wireguard-manager/pre-RELEASE/environment
sudo systemctl start wireguard-manager-reconciler
sudo systemctl start wireguard-manager
```

先把上述路径中的 `RELEASE` 换成发布版本或日期；已存在的备份目录不要直接覆盖。

备份包含会话密钥、密码哈希和安装包，必须按敏感数据保护，不得进入 Git。

## 9. 升级、验证与回滚 / Upgrade and rollback

### 9.1 升级

```sh
cd /tmp
sha256sum -c wireguard-manager-RELEASE.tar.gz.sha256

sudo test ! -e /var/backups/wireguard-manager/pre-RELEASE
sudo systemctl stop wireguard-manager
sudo systemctl stop wireguard-manager-reconciler
sudo install -d -o root -g root -m 0700 \
  /var/backups/wireguard-manager/pre-RELEASE
sudo cp -a /var/lib/wireguard-manager \
  /var/backups/wireguard-manager/pre-RELEASE/manager-data
sudo cp -a /var/lib/wireguard-manager-reconciler \
  /var/backups/wireguard-manager/pre-RELEASE/reconciler-state
sudo cp -a /etc/wireguard-manager.env \
  /var/backups/wireguard-manager/pre-RELEASE/environment

sudo tar --no-same-owner -xzf wireguard-manager-RELEASE.tar.gz -C /opt
sudo test -f /opt/wireguard-manager/pyproject.toml

sudo /opt/wireguard-manager/.venv/bin/python -m pip uninstall -y wireguard-manager
sudo rm -rf /opt/wireguard-manager/wireguard_manager.egg-info
sudo /opt/wireguard-manager/.venv/bin/python -m pip install \
  --no-cache-dir --no-deps \
  /opt/wireguard-manager
```

先把 `RELEASE` 替换为本次目标版本。这里的 `--no-deps` 只用于依赖已齐全的原有 venv；如果重建了 venv，必须像 4.3 节那样不带 `--no-deps` 安装。

启动前先验证包版本和实际导入路径：

```sh
/opt/wireguard-manager/.venv/bin/wg-manager --version
/opt/wireguard-manager/.venv/bin/python -m pip show wireguard-manager
sudo /opt/wireguard-manager/.venv/bin/python -c \
  "import wg_manager.routes as r; print(r.__file__); print(hasattr(r, 'activate_reset_device_route'))"
```

然后：

```sh
sudo systemctl start wireguard-manager-reconciler
sudo systemctl start wireguard-manager
sudo systemctl status wireguard-manager-reconciler wireguard-manager --no-pager
```

这个过程不重启 `wg0`，已在线的无关 Peer 继续工作。

### 9.2 回滚

1. 停止 Manager Web 和 reconciler，不停 `wg0`。
2. 重新解压已保留的上一个发布 tar，按上述干净安装流程重装。
3. 只在数据库迁移不兼容时恢复对应数据备份；不要盲目覆盖更新的用户数据。备份位于 `/var/backups/wireguard-manager/pre-RELEASE/`。
4. 先启动 reconciler，再启动 Web，最后运行验收清单。

## 10. 常见问题和处理 / Troubleshooting matrix

### 10.1 `Directory ... is not installable` / 缺少 `pyproject.toml`

原因：tar 解压层级错误，或只上传了子目录。

```sh
sudo test -f /opt/wireguard-manager/pyproject.toml
tar -tzf /tmp/wireguard-manager-RELEASE.tar.gz | head -20
```

必须看到 `wireguard-manager/pyproject.toml`。不要在错误目录手工伪造 `setup.py`。

### 10.2 `Cannot update time stamp ... wireguard_manager.egg-info`

原因：非 root 用户在 root 所有的 `/opt` 源码树中构建。

```sh
sudo rm -rf /opt/wireguard-manager/wireguard_manager.egg-info
sudo /opt/wireguard-manager/.venv/bin/python -m pip install \
  --no-cache-dir --no-deps --force-reinstall \
  /opt/wireguard-manager
```

只删除这个可再生元数据，不要把整个源码目录 `chown` 给 Web 用户。

### 10.3 页面 500，Jinja `BuildError` 说 endpoint 不存在

原因：模板与 Python 模块混合了不同版本，常见于同版本号覆盖安装或残留 site-packages。

```sh
sudo journalctl -u wireguard-manager -n 20 --no-pager -o cat
sudo grep -n "def activate_reset_device_route" \
  /opt/wireguard-manager/wg_manager/routes.py

sudo systemctl stop wireguard-manager
sudo /opt/wireguard-manager/.venv/bin/python -m pip uninstall -y wireguard-manager
sudo rm -rf /opt/wireguard-manager/wireguard_manager.egg-info
sudo /opt/wireguard-manager/.venv/bin/python -m pip install \
  --no-cache-dir --no-deps /opt/wireguard-manager

sudo /opt/wireguard-manager/.venv/bin/python -c \
  "import wg_manager.routes as r; print(r.__file__); print(hasattr(r, 'activate_reset_device_route'))"
```

最后一行必须是 `True`。这种渲染阶段 500 没有执行 reset，不要重启 `wg0`。

### 10.4 `Unit wireguard-manager.service not found`

```sh
sudo install -o root -g root -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager.service.example \
  /etc/systemd/system/wireguard-manager.service
sudo install -o root -g root -m 0644 \
  /opt/wireguard-manager/deploy/wireguard-manager-reconciler.service.example \
  /etc/systemd/system/wireguard-manager-reconciler.service
sudo systemctl daemon-reload
```

### 10.5 页面出现 Python `HTTPStatus.NOT_FOUND` 404

原因通常是端口上跑着 `python -m http.server` 或其他进程，不是 Manager。

```sh
sudo ss -lntp | grep -E ':(8080|8081)\b' || true
sudo systemctl status wireguard-manager --no-pager
```

停止错误进程或更换 `WG_WEB_PORT`，然后重启仅 Manager Web。

### 10.6 `WG_WEB_HOST=10.x.x.1` 为什么客户端访问不到

Manager 只绑定 WireGuard 隧道 IP，客户端必须先连入 WireGuard，且客户端 `AllowedIPs` 包含服务端隧道网段。不需要 SSM，也不必须使用 Nginx。Nginx/Caddy 只在需要 HTTPS、域名或反向代理时添加。

```sh
ip -o -4 address show dev wg0
sudo ss -lntp | grep 8081
sudo firewall-cmd --list-all 2>/dev/null || true
sudo ufw status 2>/dev/null || true
```

### 10.7 `WG_COOKIE_SECURE=0` 的含义

`0` 只是允许浏览器在 HTTP 上发送 Manager session Cookie。HTTP 数据仍在 WireGuard 隧道内加密。使用 Nginx/Caddy HTTPS 后改为 `1`。它不改变 SSH、UDP `51820` 或其他协议。

### 10.8 Web/CLI 变更没有立即进入 `wg0`

```sh
sudo grep '^WG_ADAPTER=' /etc/wireguard-manager.env
sudo systemctl status wireguard-manager-reconciler --no-pager
sudo ss -xlpn | grep reconcile.sock || true
sudo journalctl -u wireguard-manager-reconciler -n 20 --no-pager -o cat
```

`WG_ADAPTER=file` 和 `dry-run` 只重建文件，不修改在线接口。生产热应用必须是 `reconciler`。

### 10.9 用户 reset 时下载未完成就断线

先核对：

```sh
/opt/wireguard-manager/.venv/bin/wg-manager --version
```

Manager `0.2.1` 桌面端流程会先在浏览器中完整接收 Blob，然后自动提交切换；服务端响应关闭回调再提供宽限期兜底。旧版本或混合安装必须按 10.3 干净重装。

如果旧公钥已撤销且没有完整新配置，使用服务器本机/公网 SSH/控制台/另一 Peer 执行 CLI reset，一次性写出新恢复配置。

### 10.10 一连接 WireGuard，本地、SSH 或远程桌面全断

客户端配置通常包含：

```ini
AllowedIPs = 0.0.0.0/0, ::/0
```

Windows 可把它视为全隧道/kill-switch。立即断开 WireGuard，改为明确分流：

```ini
AllowedIPs = 10.44.0.0/24, 172.31.0.0/16, 10.0.0.0/24
```

未配置 IPv6 时删除 `::/0`。只在服务端 NAT、FORWARD、DNS、回程路由和独立恢复通道均已验证时才使用 `0.0.0.0/0`。

WireGuard `AllowedIPs` 只有正向匹配，没有原生 `ExcludedIPs`。`0.0.0.0/1,128.0.0.0/1` 在 Manager 0.2.1 不会被合并成 `/0`，但 GeoIP/非大陆分流仍应交给 Clash/Mihomo 或 OS 策略路由，不是 WireGuard 自身功能。

### 10.11 多个 PublicKey、多条 AllowedIPs 和 Peer 数量

- 每个设备一个独立 PublicKey 和一个独立 `[Peer]`；
- 服务端每个 Peer 只用唯一静态 `/32`，不得重复；
- 客户端 AllowedIPs 可有最多 32 条逗号分隔 IPv4 CIDR，每设备可不同；
- 全部 Peer 共用一个 `wg0` 和一个 UDP 监听端口，不会每 Peer 启端口；
- 目标访问控制应在 nftables/防火墙中按来源隧道 IP 实施，不要混同服务端 `/32` 与客户端目标路由。

### 10.12 有 handshake 但不能访问，或只发送不接收

```sh
sudo wg show wg0
sudo sysctl net.ipv4.ip_forward
sudo iptables -S FORWARD
sudo iptables -t nat -S POSTROUTING
ip route show
```

核对：服务端和客户端公钥方向、Endpoint/UDP 防火墙、唯一 `/32`、转发/NAT、目标网回程路由。没有 handshake 时先触发客户端流量，再判断服务器故障。

### 10.13 SQLite 只读、无法创建用户或待重置表

```sh
sudo namei -l /var/lib/wireguard-manager/manager.sqlite3
sudo ls -ld /var/lib/wireguard-manager
sudo ls -l /var/lib/wireguard-manager/manager.sqlite3*
```

`/var/lib/wireguard-manager` 应归 `wireguard-manager:wireguard-manager` 且权限 `0700`。不要修改表或删除 SQLite/WAL 文件；先停 Web，备份后再处理权限。

### 10.14 安装包上传被拒绝

核对平台、架构、扩展名、媒体类型、大小上限、HTTPS 许可来源和“允许再分发”确认。普通用户无权上传。优先提供官方商店/官方下载链接，不要默认镜像权利不明的客户端二进制。

## 11. 安全的诊断证据 / Safe diagnostic evidence

先取最小证据：

```sh
sudo systemctl status wg-quick@wg0 --no-pager
sudo systemctl status wireguard-manager-reconciler --no-pager
sudo systemctl status wireguard-manager --no-pager
sudo journalctl -u wireguard-manager -n 20 --no-pager -o cat
sudo journalctl -u wireguard-manager-reconciler -n 20 --no-pager -o cat
/opt/wireguard-manager/.venv/bin/wg-manager --version
```

只在证据不足时才按时间范围或错误签名扩大日志。粘贴前删除/遮盖任何 `PrivateKey`、密码、完整配置和真实公网 Endpoint。不要默认输出全部 journal 或数据库。

## 12. 上线验收清单 / Live acceptance checklist

1. `wg-quick@wg0` active，UDP 监听存在，测试客户端有 handshake。
2. `wg-manager --version` 与发布台账一致，Web 页脚版本一致。
3. Reconciler 只有 Unix Socket，不监听 TCP/UDP；Web 进程用户是非 root。
4. 管理员创建 quota=2 用户。
5. 用户创建第一、第二设备成功，静态 IP 唯一；第三台被拒绝。
6. 管理员修改该设备多条 AllowedIPs；用户一键 reset 下载，文件到达后旧 Peer 才撤销，新配置能重连，静态 IP 保留。
7. Delete 后 Peer 从 `wg show` 和期望状态移除，IP 可按策略重用。
8. 普通用户不能读他人设备或上传客户端；安装包上传/下载 SHA-256 一致。
9. 检查最后 20 行日志，不含密码、私钥或完整配置。
10. 上述是真实客户端与真实 `wg0` 验收，不能用单元测试或 `curl` 代替。

## English quick runbook

1. Install WireGuard from the supported OS repositories, create root-only keys, enable IPv4 forwarding, configure firewall/NAT, and start `wg-quick@wg0`.
2. Upload the private-repository source tar and checksum from the administrator workstation. Verify SHA-256 and ensure `/opt/wireguard-manager/pyproject.toml` exists.
3. Keep `/opt/wireguard-manager` and its venv root-owned. Run package installation as root; run Web/CLI as the dedicated unprivileged user.
4. Configure the existing server public key, endpoint, tunnel pool, reserved unmanaged IPs, split client routes, reconciler socket, and tunnel-only Web bind address.
5. Start `wg0`, then the restricted reconciler, then the Web service. Peer changes use `wg syncconf`; do not restart the WireGuard interface for routine changes.
6. Desktop Web reset fully downloads the replacement before automatic peer activation. QR reset requires post-scan confirmation; CLI reset is immediate and requires out-of-band recovery access.
7. For upgrades, stop only the two Manager services, back up Manager data, cleanly reinstall the package, verify `wg-manager --version` and the loaded module path, then start reconciler before Web.
8. Start troubleshooting with service status and the latest 20 journal lines. Never publish private keys, passwords, full configurations, or real public endpoints.
