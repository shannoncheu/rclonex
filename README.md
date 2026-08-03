# rclonex

个人 VPS 用的链接下载和云盘转存工具，只会写入 `gd:inbox/` 与 `dropbox:inbox/`。

## 架构

`Telegram Bot / React(MUI)+Nginx -> FastAPI+SQLite 队列 -> rclone copyurl（直链）或 yt-dlp/aria2 -> rclone move -> inbox/`。

三个容器分别为 `api`（认证、队列与工作器）、`bot`（白名单命令）和 `web`（React 静态站点与 API 反代）。Docker volumes 保存数据库、临时下载及日志；宿主机 `rclone/` 目录只读挂载配置。HTTP 直链优先直接上传；YouTube/Vimeo/Bilibili 使用 yt-dlp；magnet/torrent 使用 aria2 且不做种；已知网盘分享页明确拒绝。rclone 上传成功后才删除临时文件，服务重启会将未完成任务标为可重试。

## 目录

```text
backend/app/main.py     FastAPI、SQLite、队列
backend/app/bot.py      Telegram 机器人
frontend/src/           React + Material UI
nginx/                  Web Nginx 与反代示例
systemd/ logrotate/     运维示例
docker-compose.yml      服务与持久化卷
```

## 首次部署

在 Ubuntu 22.04 / Debian 12 安装 Docker Engine 与 Compose 插件后：

```bash
sudo mkdir -p /opt/rclonex
sudo chown "$USER":"$USER" /opt/rclonex
git clone https://github.com/YOUR_NAME/rclonex.git /opt/rclonex
cd /opt/rclonex
cp .env.example .env
chmod 600 .env
mkdir -p rclone
nano .env
./deploy.sh
```

`.env` 必须修改 `SESSION_SECRET`（可用 `openssl rand -hex 32` 生成）、管理员密码、Bot Token 和自己的 Telegram 数字 ID。默认只监听 `127.0.0.1:8080`；从本机安全访问：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-vps
```

打开 `http://localhost:8080`。若改为公网监听，必须配合 HTTPS、VPN 或 IP 白名单；`nginx/reverse-proxy.example.conf` 是宿主机 Nginx 示例。

## 无浏览器 VPS 配置 rclone

推荐在有浏览器的电脑执行 `rclone config`，创建名称必须为 `gd`（Google Drive）和 `dropbox`（Dropbox）的 remote，完成 OAuth 后验证 `rclone lsd gd:` 和 `rclone lsd dropbox:`。安全上传配置：

```bash
scp /path/to/rclone.conf user@your-vps:/opt/rclonex/rclone/rclone.conf
ssh user@your-vps 'chmod 600 /opt/rclonex/rclone/rclone.conf; cd /opt/rclonex; docker compose restart api bot'
```

也可在 VPS 执行 `rclone config`，自动授权时选择 `n`，在本机执行 rclone 输出的授权命令，并将返回 token 粘回 VPS。容器内验证：`docker compose exec api rclone lsd gd: --config /config/rclone.conf`。

## 使用与维护

Telegram：`/gd 电影/新片 <链接>`、`/dropbox 资料 <链接>`、`/status`、`/cancel <ID>`、`/retry <ID>`、`/help`。网页支持任务创建、状态轮询、重试/删除与受限 inbox 浏览。

更新：`cd /opt/rclonex && ./update.sh`。日志：`docker compose logs -f api bot web`。停止：`docker compose down`。启动：`docker compose up -d`。

可选开机服务与日志轮转：

```bash
sudo cp systemd/rclonex.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now rclonex
sudo cp logrotate/rclonex /etc/logrotate.d/rclonex
```

备份前先停服务。数据库备份：`docker run --rm -v rclonex_app-data:/data -v "$PWD":/backup alpine tar czf /backup/app-data-backup.tgz -C /data .`；另行加密备份 `rclone/`。恢复则停服务、将数据库解压回同一 volume、恢复 `rclone/` 后启动。

## 安全检查

- `.env`、`rclone/rclone.conf` 均 `chmod 600`，且绝不提交 Git。
- 使用强随机 session secret、管理员密码；Bot 白名单只保留个人 ID。
- 保持本机绑定，公网访问加 VPN/IP 白名单/HTTPS。
- 不在网页、Telegram 或日志显示 OAuth token、rclone.conf 或敏感链接参数。
- 仅授权两个指定 remote，并定期备份 SQLite。

目录输入会拒绝 `..`、绝对路径、反斜杠、控制符与危险字符；所有命令通过参数数组和 `shell=False` 执行。第一版没有不安全的通用网盘解析器。
