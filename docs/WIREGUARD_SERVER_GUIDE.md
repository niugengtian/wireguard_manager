# WireGuard Server 部署与操作指南 / Deployment and Operations Guide

> 中文在前，英文紧随每节。本文只提供人工操作指南，不会自动修改服务器、云防火墙或 AWS 资源。
> Chinese comes first, followed by English. This document is manual guidance only; it does not modify hosts, cloud firewalls, or AWS resources.

最后核对 / Last verified: **2026-08-02**

官方依据 / Primary references:

- [WireGuard installation](https://www.wireguard.com/install/)
- [WireGuard quick start](https://www.wireguard.com/quickstart/)
- [wg(8) manual: syncconf](https://git.zx2c4.com/wireguard-tools/about/src/man/wg.8)
- [Ubuntu Server WireGuard guide](https://ubuntu.com/server/docs/how-to/wireguard-vpn/)
- [Amazon Linux 2023 package list](https://docs.aws.amazon.com/linux/al2023/release-notes/all-packages.html)

## 1. 支持范围 / Supported baseline

| 系统 / OS | 建议版本 / Recommended release | 安装方式 / Installation path |
| --- | --- | --- |
| Amazon Linux | Amazon Linux 2023 | 官方仓库 `wireguard-tools`；内核已足够新 / official `wireguard-tools`, modern kernel |
| Ubuntu | 22.04 LTS、24.04 LTS 或更新的受支持 LTS | 官方仓库 `wireguard` |
| CentOS | CentOS Stream 9/10 | 先尝试系统仓库；缺包时启用匹配版本的 EPEL |
| Rocky Linux | Rocky 9；Rocky 8 见兼容路径 | Rocky 9 使用 EPEL 工具；Rocky 8 按 WireGuard 官方 EL8/ELRepo 路径 |

不要在已停止维护的 CentOS Linux 7/8 或 Amazon Linux 1 上新部署。Amazon Linux 2 是上一代版本；新部署优先 AL2023。
Do not start new deployments on EOL CentOS Linux 7/8 or Amazon Linux 1. Prefer AL2023 over the previous Amazon Linux 2 generation.

## 2. 部署前确认 / Preflight

先记录系统、默认出口网卡和现有防火墙。不要把包含公网端点的完整输出粘贴到工单或聊天中。
Record the OS, default egress interface, and current firewall first. Do not paste unredacted output containing public endpoints into tickets or chats.

```sh
cat /etc/os-release
uname -r
ip route show default
sudo ss -lunp
sudo systemctl is-active firewalld 2>/dev/null || true
sudo ufw status 2>/dev/null || true
```

规划示例 / Example plan:

- 接口 / interface: `wg0`
- UDP 端口 / UDP port: `51820`
- 隧道网段 / tunnel network: `10.44.0.0/24`
- 服务端地址 / server address: `10.44.0.1/24`
- 客户端起始地址 / first client address: `10.44.0.2/32`
- 出口网卡占位符 / egress interface placeholder: `PUBLIC_INTERFACE`

如果这个网段与 VPC、办公室、家庭或容器网络冲突，先换成不冲突的 RFC1918 网段。
If this network overlaps a VPC, office, home, or container network, choose a different non-overlapping RFC1918 range first.

## 3. 安装软件 / Install packages

### Amazon Linux 2023

AWS 当前 AL2023 包列表包含 `wireguard-tools` 和 `iptables-nft`。
The current AWS AL2023 package list includes `wireguard-tools` and `iptables-nft`.

```sh
sudo dnf update -y
sudo dnf install -y wireguard-tools iptables-nft
```

### Ubuntu

```sh
sudo apt update
sudo apt install -y wireguard iptables
```

### CentOS Stream 9/10 与 Rocky Linux 9

先直接查询；如果系统仓库没有 `wireguard-tools`，再启用与当前主版本匹配的 EPEL。不要混用不同 EL 主版本仓库。
Query first; if `wireguard-tools` is absent, enable EPEL matching the current EL major. Never mix repositories from different EL majors.

```sh
sudo dnf info wireguard-tools || true
sudo dnf install -y epel-release
sudo dnf install -y wireguard-tools iptables
```

CentOS Stream 可能还需要对应版本的 `epel-next-release`；只有在 `dnf info wireguard-tools` 仍找不到包时才添加。
CentOS Stream may also need the matching `epel-next-release`; add it only if the package is still unavailable.

### Rocky Linux 8 兼容路径 / Rocky Linux 8 compatibility path

WireGuard 官方 EL8 路径使用 EPEL + ELRepo 的 `kmod-wireguard`。先确认组织允许这些第三方仓库。
The official EL8 path uses EPEL + ELRepo and `kmod-wireguard`. Confirm that these repositories are allowed by your organization.

```sh
sudo dnf install -y \
  https://dl.fedoraproject.org/pub/epel/epel-release-latest-8.noarch.rpm \
  https://www.elrepo.org/elrepo-release-8.el8.elrepo.noarch.rpm
sudo dnf install -y kmod-wireguard wireguard-tools iptables
```

安装后检查 / Verify after installation:

```sh
wg --version
sudo modprobe wireguard
```

`modprobe` 失败时不要继续。先核对当前内核、内核模块包和第三方仓库是否与系统主版本一致。
Do not continue if `modprobe` fails. Verify the running kernel, module package, and repository major versions first.

## 4. 生成服务端密钥 / Generate server keys

密钥目录和私钥必须只允许 root 访问。下面的命令不向终端打印私钥。
The key directory and private key must be root-only. These commands do not print the private key.

```sh
sudo install -d -m 700 -o root -g root /etc/wireguard
sudo sh -c 'umask 077; wg genkey > /etc/wireguard/wg0.key; wg pubkey < /etc/wireguard/wg0.key > /etc/wireguard/wg0.pub'
sudo chmod 600 /etc/wireguard/wg0.key
sudo chmod 644 /etc/wireguard/wg0.pub
```

只有公钥可以安全地抄到 Manager 的 `WG_SERVER_PUBLIC_KEY`。不要把 `/etc/wireguard/wg0.key` 写进命令行参数、日志、Git 或聊天。
Only the public key belongs in `WG_SERVER_PUBLIC_KEY`. Never place `wg0.key` in command arguments, logs, Git, or chat.

```sh
sudo cat /etc/wireguard/wg0.pub
```

## 5. 开启 IPv4 转发 / Enable IPv4 forwarding

```sh
printf '%s\n' 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/90-wireguard-forward.conf
sudo sysctl --system
sudo sysctl net.ipv4.ip_forward
```

最后一条必须返回 `net.ipv4.ip_forward = 1`。
The final command must report `net.ipv4.ip_forward = 1`.

## 6. 创建 `wg0.conf` / Create `wg0.conf`

先从 `ip route show default` 确认实际出口网卡，然后把下面所有 `PUBLIC_INTERFACE` 替换掉。使用 `sudoedit`，不要把私钥复制进编辑器：`PostUp` 会从 root-only 密钥文件加载它。
Identify the real egress interface from `ip route show default`, replace every `PUBLIC_INTERFACE`, and use `sudoedit`. Do not paste the private key; `PostUp` loads it from the root-only key file.

```sh
sudoedit /etc/wireguard/wg0.conf
```

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

[Peer]
# 首台测试设备；继续重复 [Peer] 段即可支持更多设备
# First test device; repeat the [Peer] section for every additional device
PublicKey = CLIENT_PUBLIC_KEY_BASE64
AllowedIPs = 10.44.0.2/32

[Peer]
PublicKey = ANOTHER_CLIENT_PUBLIC_KEY_BASE64
AllowedIPs = 10.44.0.3/32
```

然后锁定权限并做离线解析检查 / Lock permissions and perform an offline parse check:

```sh
sudo chown root:root /etc/wireguard/wg0.conf
sudo chmod 600 /etc/wireguard/wg0.conf
sudo wg-quick strip wg0 >/dev/null
```

每个 Peer 的 `AllowedIPs` 必须是唯一的 `/32`。这里没有配置 IPv6，所以客户端不要使用 `::/0`。只有在服务器真的提供隧道 DNS 时才设置客户端 `DNS`。
Every peer must have a unique `/32` `AllowedIPs`. This guide does not configure IPv6, so clients must not use `::/0`. Set client `DNS` only when the server actually provides a tunnel DNS service.

## 7. 放行 UDP 与云侧规则 / Open UDP and cloud-side rules

主机防火墙二选一，按实际启用的工具执行。
Use the section matching the firewall actually enabled on the host.

Ubuntu/UFW:

```sh
sudo ufw allow 51820/udp
sudo ufw status
```

firewalld:

```sh
sudo firewall-cmd --permanent --add-port=51820/udp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports
```

如果服务器位于 AWS、其他云或上游硬件防火墙后，还必须人工允许到实例的 UDP `51820`。来源范围已知时应限制来源 CIDR，不要无条件向全网开放管理端口。
If the host is behind AWS, another cloud, or an upstream firewall, manually allow inbound UDP `51820`. Restrict source CIDRs when known; never expose management ports broadly.

本文不会创建或修改 Security Group、NACL、路由表、EIP 或任何 AWS 资源。
This guide does not create or modify Security Groups, NACLs, routes, EIPs, or any AWS resource.

## 8. 启动与开机自启 / Start and enable

```sh
sudo systemctl enable --now wg-quick@wg0
sudo systemctl status wg-quick@wg0 --no-pager
sudo wg show wg0
sudo ss -lunp | grep 51820
```

`wg show` 中没有 handshake 并不一定代表服务端故障；客户端必须先发起流量。不要把包含真实端点的完整输出提交到公共问题单。
No handshake does not necessarily mean a server fault; a client must initiate traffic first. Do not publish unredacted output containing real endpoints.

## 9. 客户端最小模板 / Minimal client template

客户端私钥只能在客户端生成或由 Manager 一次性交付。下面只有占位符，不要把真实配置提交到 Git。
Generate the client private key on the client or deliver it once through Manager. The template contains placeholders only; never commit a real configuration.

```ini
[Interface]
PrivateKey = CLIENT_PRIVATE_KEY_BASE64
Address = 10.44.0.2/24

[Peer]
PublicKey = SERVER_PUBLIC_KEY_BASE64
Endpoint = VPN_ENDPOINT_HOST:51820
AllowedIPs = 0.0.0.0/0
PersistentKeepalive = 25
```

只访问内部网段时，把 `AllowedIPs` 改成明确的内部 CIDR，而不是默认路由。
For split tunnel access, replace the default route with explicit internal CIDRs.

## 10. 日常操作 / Routine operations

### 查看状态 / Status

```sh
sudo wg show wg0
ip address show dev wg0
ip route show
sudo systemctl status wg-quick@wg0 --no-pager
sudo journalctl -u wg-quick@wg0 -n 20 --no-pager
```

日志默认只看最后 20 行；证据不足时再按时间或错误签名扩大范围。
Start with the latest 20 log lines; expand by time range or error signature only when necessary.

### 不重启的手工热更新 / Manual hot apply without restart

每个设备使用独立的 `[Peer]` 段和唯一 `/32`。编辑并验证 `wg0.conf` 后，用 WireGuard 官方 `syncconf` 差量应用；它只修改差异，不销毁接口：

```sh
sudo cp -a /etc/wireguard/wg0.conf /etc/wireguard/wg0.conf.rollback
sudoedit /etc/wireguard/wg0.conf
sudo wg-quick strip wg0 >/dev/null
sudo wg syncconf wg0 <(sudo wg-quick strip wg0)
sudo wg show wg0 allowed-ips
```

最后两条使用 Bash/Zsh 的进程替换语法；不要使用 `systemctl restart wg-quick@wg0`。For each device, add an independent `[Peer]` with a unique `/32`, validate it, and use `wg syncconf` to apply only the differences without destroying the interface.

### Reset 设备 / Reset a device

从配置中删除旧公钥、加入新公钥；静态 IP 可以保持不变。验证后执行上述 `syncconf`。旧公钥必须不再出现在 `wg show`。
Remove the old public key, add the new one with the same static IP, validate, then run the `syncconf` command above. The old key must disappear from `wg show`.

### 删除设备 / Delete a device

先删除 Peer 并执行 `syncconf`，确认 `wg show` 已没有该公钥，然后才能重新分配其 IP。
Remove and hot-apply the peer change first; only reuse the IP after confirming that the public key is absent from `wg show`.

### 回滚 / Roll back

```sh
sudo install -o root -g root -m 600 /etc/wireguard/wg0.conf.rollback /etc/wireguard/wg0.conf
sudo wg-quick strip wg0 >/dev/null
sudo wg syncconf wg0 <(sudo wg-quick strip wg0)
sudo wg show wg0
```

只有接口本身已经不可用且进入维护窗口时才考虑 `restart`；它会中断现有隧道。
Consider `restart` only if the interface itself is already unusable and a maintenance window is active; restart interrupts tunnels.

## 11. 故障排查 / Troubleshooting

| 现象 / Symptom | 检查 / Check |
| --- | --- |
| 服务启动失败 | `wg-quick strip wg0`、文件权限、`PUBLIC_INTERFACE` 是否已替换、密钥文件是否存在 |
| 没有 handshake | UDP 端口、云/上游防火墙、服务端/客户端公钥是否配反、客户端 Endpoint |
| 有 handshake 但不能访问 | `AllowedIPs`、唯一 IP、`net.ipv4.ip_forward`、FORWARD/NAT 规则和回程路由 |
| DNS 不工作 | 未运行隧道 DNS 时删除客户端 `DNS`；不要假定 `10.44.0.1` 自动提供 DNS |
| 热更新后 Peer 不一致 | 对比 `wg show` 与 `wg-quick strip wg0`；必要时用 `syncconf` 应用已验证 rollback 文件 |
| MTU 问题 | 先观察路径与丢包；只有得到证据后再在两端逐步降低 MTU |

## 12. 与 WireGuard Manager 的边界 / Manager integration boundary

在已经运行本指南服务端的主机上安装 Web/CLI，请使用 [WireGuard Manager 安装与启动指南](WG_MANAGER_INSTALL.md)。该指南不会重装 WireGuard 或覆盖 `wg0.conf`。

生产实时模式设置：

```ini
WG_ADAPTER=reconciler
WG_INTERFACE=wg0
```

Web 与 CLI 共用以下链路，不需要 Redis：

```text
SQLite transaction
  -> atomic expected-peers.json (all managed devices, one peer per device)
  -> local Unix socket
  -> restricted root reconciler
  -> merge with unmanaged live peers
  -> wg syncconf
  -> verify public keys and unique /32 AllowedIPs
  -> commit result or rollback live state
```

服务端与客户端的 `AllowedIPs` 含义不同：

1. 服务端每个 Peer 的 `AllowedIPs` 是该设备的唯一隧道地址，例如 `10.44.0.27/32`。多个 PublicKey 对应多个独立 `[Peer]`，不能共享相同 `/32`。
2. 客户端配置的 `AllowedIPs` 是该设备送入隧道的目标路由，可以按设备不同；多条 CIDR 使用英文逗号分隔，例如 `10.255.77.0/24, 172.31.0.0/16`。`0.0.0.0/0` 表示所有 IPv4 流量走隧道。修改后必须 reset 才能一次性交付新配置。
3. 服务端 Peer `AllowedIPs` 不是目标访问控制。若要限制某设备访问特定 VPC/内部网段，应通过 nftables/防火墙按来源隧道 IP 实施。

自动化测试已覆盖 300 个托管 Peer、未托管 Peer 保留、reset/delete 公钥撤销、失败回滚以及无 `restart` 命令。`NOT VERIFIED`：在你的真实服务器上安装并执行在线验收；本文没有操作 AWS 或服务器资源。

Automated tests cover 300 managed peers, unmanaged-peer preservation, key revocation on reset/delete, rollback, and the absence of restart commands. `NOT VERIFIED`: installation and live acceptance on your server; this guide has not modified any server or AWS resource.
