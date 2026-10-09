# TG 转存机器人

Telegram 媒体自动转存到阿里云盘的机器人。

## 功能

- **收藏夹同步**：自动同步 Telegram Saved Messages 的新视频/图片到阿里云盘
- **t.me 链接**：发送 t.me 频道链接，直接下载媒体（支持单条和范围 `起始-结束`）
- **范围下载**：`https://t.me/频道/1013661-https://t.me/频道/1013682` 批量下载
- **/getbot**：从防转发/bot 取文件（导航器模式）
- **91porn 同步**：定时同步到阿里云盘 `91/` 目录
- **网页面板**：8899 端口，实时传输进度、历史记录、任务管理

## 架构

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ Telegram    │────▶│  tg-bot      │────▶│ 阿里云盘    │
│ (用户号+Bot) │     │  (Python)    │     │ (aligo)     │
└─────────────┘     └──────────────┘     └─────────────┘
                           │
                    ┌──────┴──────┐
                    │ 8899 面板   │
                    │ (dashboard) │
                    └─────────────┘
```

### 双入口

- **收藏夹**（用户号 `@client.on(NewMessage)`）：完全静默，监听 Saved Messages
- **Bot 号**（`@_bot_client.on(NewMessage)`）：@duola2meng_transfer_bot，可交互命令

两个入口命令对齐：/start、/menu、/stats、/history、/drive、/clean、/getbot、直接发文件、发 t.me 链接。

### 核心模块

| 模块 | 版本 | 说明 |
|------|------|------|
| runner.py | 2.11.2 | 主入口，双客户端初始化、命令路由、面板启动 |
| handlers.py | 2.7.3 | 业务逻辑：收藏夹同步、t.me、getbot、相册处理 |
| transfer.py | 2.0.2 | 上传到云盘（阿里云盘） |
| cloud.py | - | 多网盘调度层，目录命名规范 |
| notify.py | 1.5.6 | 通知管理（编辑模式、自动删除） |
| dashboard.py | 1.6.4 | 8899 网页面板（进度、历史、任务管理） |
| fastdl.py | 2.0.0 | 并行多连接下载 |
| tasks.py | - | 任务状态机、取消、台账模式 |
| history.py | - | 同步历史记录 |

### 下载链路（2026-10-09 起全部走用户号）

```
用户发送 t.me 链接 / 转发文件到收藏夹
  ↓
handlers.handle_tme_link / handle_media
  ↓ (全部用 _client 用户号，不走 bot)
fastdl 并行下载 → bot_inbox/
  ↓
transfer.upload → 阿里云盘
  ↓
notify 通知（开始+完成，编辑模式）
```

**关键设计**：
- 所有下载走用户号 `_client`，bot 号仅做 fallback 已删除（v2.7.0）
- t.me 优先用户号：bot 可能解析成功但读不到媒体
- 台账模式：先记账 `record_task`，finally 销账，防重启丢任务

### 云端目录规范

- 收藏夹视频/图片：`TG收藏/<时间戳>/`
- t.me 单条：`TG链接/<频道>_<时间戳>/`
- t.me 范围：`TG链接/<频道>_<起始>-<结束>_<时间戳>/`（22 条共用一个目录）
- 相册：`TG收藏/<时间戳>/`（带时间戳防重名）
- 91porn：`91/` 根目录

## 部署

```bash
# 服务器：/root/TG转存机器人/
# 服务：tg-bot.service (systemd)
# 面板：http://服务器IP:8899 (token 鉴权)

systemctl restart tg-bot.service
journalctl -u tg-bot.service -f  # 看日志
```

## 配置

- `config/tg_config.json`：API_ID、API_HASH
- `config/proxy.json`：代理配置（可选）
- `~/.aligo/tg_sync.json`：阿里云盘登录态
- `user_session.session`：Telegram 用户号登录态

## 版本铁律

改代码 → 改 VERSION → 写 CHANGELOG → 编译 → 部署 → 汇报版本。
