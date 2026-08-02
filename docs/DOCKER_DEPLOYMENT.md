# 无 Compose 的容器部署 / Docker Run Deployment without Compose

> `READY`：本方案只使用两个环境文件和三条 `docker run` 命令，不需要 Docker Compose。WireGuard Server 使用现成镜像；Manager Web/CLI 与 reconciler 使用同一个自建镜像、不同启动角色。

> `NOT VERIFIED`：尚未在你的真实服务器和真实公网链路上执行。以下命令不会由项目自动操作 AWS、安全组或现有 WireGuard 服务。

## 1. 三个运行角色 / Three runtime roles

| 容器 | 镜像 | 权限 | 作用 |
| --- | --- | --- | --- |
| `wireguard-server` | `lscr.io/linuxserver/wireguard:1.0.20260223-r0-ls118` | `NET_ADMIN` | 建立唯一的 `wg0` 和 UDP `51820` |
| `wireguard-manager-reconciler` | `wireguard-manager:0.3.0` | `root:10001`、`NET_ADMIN`、`DAC_OVERRIDE` | 通过 `wg syncconf` 热增删 Peer、校验、失败回滚 |
| `wireguard-manager` | `wireguard-manager:0.3.0` | 非 root `10001:10001`、无 Linux capability | Web、SQLite、配置一次性交付和 CLI |

后两个容器是**同一个镜像**，只是最后一个参数分别为 `reconciler` 和 `manager`。三个容器共享 `wireguard-server` 的网络命名空间，因此只有一个 `wg0`、一个 WireGuard UDP 端口；Peer 再多也不会增加监听端口。Manager 只绑定隧道地址 `10.44.0.1:8081`，不向宿主机公网发布 Web 端口。

The Manager image is reused for both application and reconciler roles. All three containers share the server container's network namespace, so the host exposes one WireGuard UDP port rather than one port per peer.

## 2. 适用边界 / Deployment boundary

以下步骤是**全新容器化 WireGuard Server**。不要让宿主机的原生 `wg-quick@wg0` 与容器同时使用同名接口或同一 UDP 端口。

如果当前服务器已经有正式 Peer：先保留原生服务和 `/etc/wireguard` 备份，不要直接执行本章替换。把既有私钥、PostUp/PostDown、防火墙和地址规划迁入 LinuxServer `/config` 需要单独维护窗口；本项目当前没有宣称自动迁移已经运行的 `wg0.conf`。

This is a fresh containerized deployment. Migration from an existing native `wg0.conf` is deliberately not automatic.

## 3. 本机构建 Manager 镜像 / Build locally

先在目标服务器运行 `uname -m`。普通 Intel/AMD EC2 的 `x86_64` 对应 `linux/amd64`；Graviton 的 `aarch64` 对应 `linux/arm64`。以下示例为常见的 `linux/amd64`：

```sh
docker buildx build --pull --platform linux/amd64 --load \
  --build-arg WG_MANAGER_VERSION=0.3.0 \
  --build-arg SOURCE_REVISION=local \
  -t wireguard-manager:0.3.0 .

docker image inspect wireguard-manager:0.3.0 \
  --format '{{.Id}} {{.Architecture}} {{.Config.User}} {{index .Config.Labels "org.opencontainers.image.version"}}'
```

预期架构是 `amd64`、用户是 `10001:10001`、版本是 `0.3.0`。Graviton 服务器把平台改为 `linux/arm64`。私有仓库无法从服务器拉取时，导出镜像：

```sh
docker image save wireguard-manager:0.3.0 | gzip -9 > wireguard-manager-0.3.0-linux-amd64.tar.gz
shasum -a 256 wireguard-manager-0.3.0-linux-amd64.tar.gz > wireguard-manager-0.3.0-linux-amd64.tar.gz.sha256
```

上传并在服务器校验、导入：

```sh
scp wireguard-manager-0.3.0-linux-amd64.tar.gz* YOUR_SERVER:/tmp/
ssh YOUR_SERVER
cd /tmp
sha256sum -c wireguard-manager-0.3.0-linux-amd64.tar.gz.sha256
gunzip -c wireguard-manager-0.3.0-linux-amd64.tar.gz | sudo docker image load
sudo docker image inspect wireguard-manager:0.3.0 --format '{{.Id}} {{.Architecture}} {{.Config.User}}'
```

镜像归档中包含应用代码和 Python 依赖，不包含数据库、密码、私钥、客户端配置或真实公网地址。

## 4. 服务器目录和配置 / Host directories and configuration

```sh
sudo install -d -o root  -g root  -m 0750 /etc/wireguard-manager
sudo install -d -o root  -g root  -m 0700 /opt/wireguard-manager-data/wireguard
sudo install -d -o 10001 -g 10001 -m 0700 /opt/wireguard-manager-data/manager
sudo install -d -o root  -g 10001 -m 0750 /opt/wireguard-manager-data/reconciler
sudo install -d -o root  -g 10001 -m 0750 /opt/wireguard-manager-data/run
```

把仓库中的示例复制为服务器配置：

```sh
sudo cp docker/wireguard-server.env.example /etc/wireguard-manager/wireguard-server.env
sudo cp docker/manager.env.example /etc/wireguard-manager/manager.env
sudo chmod 0600 /etc/wireguard-manager/*.env
sudoedit /etc/wireguard-manager/wireguard-server.env
sudoedit /etc/wireguard-manager/manager.env
```

至少替换：

- `SERVERURL`：真实公网域名或 IP，不带协议、不带端口；
- `WG_ENDPOINT`：同一公网入口加 UDP 端口，例如 `vpn.example.net:51820`；
- `WG_COOKIE_SECURE=0`：仅适用于通过 WireGuard 隧道访问 `http://10.44.0.1:8081`；以后由 Nginx 提供 HTTPS 时改为 `1`；
- `WG_TUNNEL_CIDR`、`INTERNAL_SUBNET` 和 Web 地址必须保持同一规划；默认服务端 `.1`、bootstrap `.2`，因此 `WG_RESERVED_IPS=10.44.0.2`。

默认客户端路由是 `10.44.0.0/24`，不会接管本机全部流量。未验证服务端转发、NAT、DNS 和恢复通道前，不要改成 `0.0.0.0/0`。

## 5. 启动 WireGuard Server / Start the server

```sh
sudo docker pull lscr.io/linuxserver/wireguard:1.0.20260223-r0-ls118

sudo docker run -d \
  --name wireguard-server \
  --restart unless-stopped \
  --cap-add NET_ADMIN \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --sysctl net.ipv4.ip_forward=1 \
  --env-file /etc/wireguard-manager/wireguard-server.env \
  -p 51820:51820/udp \
  -v /opt/wireguard-manager-data/wireguard:/config \
  lscr.io/linuxserver/wireguard:1.0.20260223-r0-ls118
```

`SERVERPORT` 和 `-p` 左右两侧的端口必须一致。若宿主机内核没有加载 WireGuard 模块，先在宿主机安装/加载模块；只有确有需要时才增加 LinuxServer 文档中的 `SYS_MODULE` 和 `/lib/modules` 挂载。

等待 `wg0`：

```sh
sudo docker exec wireguard-server wg show wg0
sudo docker logs --tail 20 wireguard-server
```

因为示例设置了 `PEERS=bootstrap`，首次启动会生成一个只用于进入管理隧道的 bootstrap 客户端。它位于：

```text
/opt/wireguard-manager-data/wireguard/peer_bootstrap/peer_bootstrap.conf
```

这是一次性敏感配置；通过安全通道取走并限制为 `0600`。`LOG_CONFS=false` 用于避免把完整配置或二维码写入普通容器日志。

## 6. 启动 reconciler / Start the reconciler

```sh
sudo docker run -d \
  --name wireguard-manager-reconciler \
  --restart unless-stopped \
  --network container:wireguard-server \
  --user 0:10001 \
  --cap-drop ALL \
  --cap-add NET_ADMIN \
  --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  --env-file /etc/wireguard-manager/manager.env \
  -v /opt/wireguard-manager-data/manager:/var/lib/wireguard-manager:ro \
  -v /opt/wireguard-manager-data/reconciler:/var/lib/wireguard-manager-reconciler \
  -v /opt/wireguard-manager-data/run:/run/wireguard-manager \
  wireguard-manager:0.3.0 reconciler
```

它等待 `wg0`，从在线接口读取服务端**公钥**到共享只读状态目录，然后监听 Unix Socket。它不会读取或复制服务端私钥，也不会执行 `wg-quick restart`。

```sh
sudo docker logs --tail 20 wireguard-manager-reconciler
sudo docker exec wireguard-manager-reconciler \
  /usr/local/bin/wg-manager-container healthcheck-reconciler
```

## 7. 启动 Manager / Start Manager

```sh
sudo docker run -d \
  --name wireguard-manager \
  --restart unless-stopped \
  --network container:wireguard-server \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
  --env-file /etc/wireguard-manager/manager.env \
  -v /opt/wireguard-manager-data/manager:/var/lib/wireguard-manager \
  -v /opt/wireguard-manager-data/reconciler:/var/lib/wireguard-manager-reconciler:ro \
  -v /opt/wireguard-manager-data/run:/run/wireguard-manager:ro \
  wireguard-manager:0.3.0 manager
```

入口会等待公钥和 reconciler Socket，先同步数据库的期望 Peer，再以非 root 身份启动 Web。没有 Compose 也不依赖固定的启动延时。

```sh
sudo docker logs --tail 20 wireguard-manager
sudo docker inspect wireguard-manager --format '{{.Config.User}}'
sudo docker exec wireguard-manager id
sudo docker exec wireguard-manager \
  /usr/local/bin/wg-manager-container healthcheck-manager
```

预期 `id` 为 `uid=10001 gid=10001`。导入 bootstrap 配置并连接后，在客户端浏览器打开：

```text
http://10.44.0.1:8081
```

这里的 HTTP/HTTPS 指的是**管理网页协议**，不是 WireGuard 隧道协议。WireGuard 数据仍通过 UDP `51820` 加密；HTTP 页面位于已建立的隧道内。若要从公网直接暴露管理页，应先用 Nginx/其他反向代理终止 TLS、限制来源，并把 `WG_COOKIE_SECURE` 改为 `1`。

## 8. 创建管理员和使用 CLI / Admin and CLI

```sh
sudo docker exec -it wireguard-manager \
  /usr/local/bin/wg-manager-container cli \
  user create admin --role admin --quota 0
```

密码使用无回显交互输入。日常 CLI 也通过同一个运行中的 Manager 容器：

```sh
sudo docker exec -it wireguard-manager \
  /usr/local/bin/wg-manager-container cli user list

sudo docker exec -it wireguard-manager \
  /usr/local/bin/wg-manager-container cli \
  device create alice laptop --type linux \
  --allowed-ips '10.44.0.0/24' --output /tmp/laptop.conf

docker cp wireguard-manager:/tmp/laptop.conf ./laptop.conf
chmod 0600 ./laptop.conf
sudo docker exec wireguard-manager rm -f /tmp/laptop.conf
```

不要在多人可读的日志、Shell 历史或工单中放密码、私钥、完整配置和真实公网 IP。CLI 输出配置时还要把容器内文件安全复制出来并立即删除；普通用户更适合使用 Web 一次性下载。

## 9. 验证即时生效 / Verify live peer changes

Web 或 CLI 新增、reset、delete 后，Manager 写入期望状态并请求 reconciler 执行 `wg syncconf`。`wg0` 不重启，现有其他 Peer 的握手和端口不变。

```sh
sudo docker exec wireguard-server wg show wg0 peers
sudo docker exec wireguard-server wg show wg0 allowed-ips
sudo docker logs --tail 20 wireguard-manager-reconciler
```

同一个 PublicKey 只能属于一个 Peer；每台设备使用独立 PublicKey 和唯一服务端 `AllowedIPs=<隧道IP>/32`。管理页的多条客户端 `AllowedIPs` 是该设备的**目的路由列表**，不会在服务端一个 Peer 下复制多个用户公钥。

## 10. 重启、升级和回滚 / Restart, upgrade, rollback

Docker 在宿主机启动时可能并行恢复容器，但两个 Manager 角色都带就绪等待。若手工重启或重建 WireGuard Server，按以下顺序恢复，确保运行时 Peer 重新应用：

```sh
sudo docker restart wireguard-server
sudo docker restart wireguard-manager-reconciler
sudo docker restart wireguard-manager
```

升级 Manager 时不要删除 `/opt/wireguard-manager-data`：

```sh
sudo docker stop wireguard-manager wireguard-manager-reconciler
sudo docker rm wireguard-manager wireguard-manager-reconciler
```

导入新版本镜像后，使用第 6、7 节命令和新标签重建两个容器。回滚同理切回旧标签；数据库升级前先做一致性备份：

```sh
sudo docker exec wireguard-manager \
  python -c 'import sqlite3; s=sqlite3.connect("/var/lib/wireguard-manager/manager.sqlite3"); d=sqlite3.connect("/var/lib/wireguard-manager/manager-backup.sqlite3"); s.backup(d); d.close(); s.close()'
```

备份目录必须受到与数据库相同的访问控制。

## 11. 常见问题 / Troubleshooting

### 页面 404 或连不上

- 不要运行 `python -m http.server`；它只是静态文件服务器，会返回 404。
- 先确认客户端已连接 bootstrap 隧道，再访问 `http://10.44.0.1:8081`。
- 检查 `WG_WEB_HOST=10.44.0.1`、`WG_WEB_PORT=8081`，以及 Manager 是否共享 `wireguard-server` 网络命名空间。

### `BLOCKED: timed out waiting for WireGuard interface wg0`

- `wireguard-server` 未正常建立 `wg0`；先检查最后 20 行日志和 `wg show wg0`。
- 不要把 Manager/reconciler 放到普通 bridge 网络；必须使用 `--network container:wireguard-server`。

### reconciler 目录所有权错误

重新执行第 4 节的 `install -d -o root -g 10001`。不要把运行目录改成 `0777`。

### reset/delete 后 Peer 没变化

```sh
sudo docker logs --tail 20 wireguard-manager
sudo docker logs --tail 20 wireguard-manager-reconciler
sudo docker exec wireguard-server wg show wg0 allowed-ips
```

确认 `WG_ADAPTER=reconciler`、Socket 挂载路径一致、reconciler 有 `NET_ADMIN`，并检查状态文件：

```sh
sudo sed -n '1,80p' /opt/wireguard-manager-data/reconciler/reconcile-status.json
```

状态文件只有请求 ID、摘要和数量，不应包含私钥或完整客户端配置。

### `0.0.0.0/0` 后本地/远程连接中断

这是全隧道路由结果，不是 Peer 数量限制。先断开该客户端隧道恢复连接，再把设备客户端范围改回实际内网 CIDR，执行 reset 并导入新配置。WireGuard 没有原生排除列表或 GeoIP 路由；复杂分流应由操作系统策略路由或 Clash/Mihomo 负责。

### 为什么环境文件不能替代全部命令行参数

`--env-file` 只负责应用和镜像环境变量。端口发布、capability、网络命名空间、只读文件系统和宿主机挂载是 Docker 的隔离边界，必须由 `docker run` 声明；把它们写进 Compose 不是必需条件。

## 12. English quick reference

1. Copy and edit `docker/wireguard-server.env.example` and `docker/manager.env.example`.
2. Create the five host directories with the exact numeric ownership in section 4.
3. Run the existing LinuxServer WireGuard image once, then the same Manager image with `reconciler` and `manager` roles.
4. Import the generated bootstrap client and open `http://10.44.0.1:8081` through the tunnel.
5. Use `/usr/local/bin/wg-manager-container cli ...` inside the Manager container for local administration.
6. Restart in server → reconciler → manager order after a server recreation so desired peers are reapplied.

官方镜像与变量参考 / Official image and environment reference:

- [LinuxServer WireGuard image](https://github.com/linuxserver/docker-wireguard)
- [LinuxServer WireGuard tags](https://hub.docker.com/r/linuxserver/wireguard/tags)
- [Official Python image tags](https://hub.docker.com/_/python/tags)
