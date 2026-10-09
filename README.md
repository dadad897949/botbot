# TG 转存机器人

Telegram 媒体自动转存到阿里云盘。

## 功能

| 功能 | 说明 |
|------|------|
| 收藏夹同步 | 自动同步 Telegram Saved Messages 的新视频/图片到阿里云盘 |
| t.me 链接 | 发送 t.me 频道链接直接下载，支持单条和范围 `起始-结束` |
| /getbot | 从防转发 bot 取文件（导航器模式，支持 A/B/C 类 bot） |
| 评论区转存 | 转存频道帖子的评论区媒体 |
| 91porn 同步 | 定时同步到阿里云盘 `91/` 目录 |
| 网页面板 | 实时传输进度、历史记录、任务管理 |
| 广告过滤 | 自动过滤广告视频（白名单机制） |

## 快速开始

### 1. 环境要求

- Python 3.8+
- Telegram API 账号（https://my.telegram.org）
- 阿里云盘账号

### 2. 安装依赖

```bash
pip install telethon aligo
```

### 3. 配置

复制示例配置并填写：

```bash
cp config/tg_config.json.example config/tg_config.json
cp config/bot_config.json.example config/bot_config.json
# 可选：代理
cp config/proxy.json.example config/proxy.json
```

`tg_config.json`：
```json
{
  "api_id": 12345678,
  "api_hash": "your_api_hash_here"
}
```

`bot_config.json`：
```json
{
  "bot_token": "your_bot_token_here",
  "owner_id": 123456789,
  "dashboard_token": "your_dashboard_token_here"
}
```

### 4. 登录

```bash
# 用户号登录（首次需输入验证码）
python -m tg_bot.runner

# 阿里云盘登录（扫码）
python -c "from tg_bot.alipan import get_ali; get_ali()"
```

### 5. 启动服务

```bash
sudo cp tg-bot.service /etc/systemd/system/
sudo systemctl enable --now tg-bot.service
```

## 使用

### 收藏夹同步
直接往 Telegram Saved Messages 发视频/图片，自动转存到 `TG收藏/`。

### t.me 链接
发送频道链接：
```
https://t.me/channel/12345
https://t.me/channel/12345-https://t.me/channel/12380  # 范围下载
```

### /getbot
从 bot 取文件：
```
/getbot @example_bot 口令
```

### 网页面板
`http://服务器IP:8900`（nginx 反代到 127.0.0.1:8899）
- 实时进度/速度/ETA
- 历史记录（最近 500 条）
- 任务取消、记录删除

## 目录结构

```
tg_bot/
├── runner.py       # 主入口，双入口命令（收藏夹 + bot）
├── handlers.py     # 消息处理：下载、t.me、getbot、评论区
├── transfer.py     # 上传到阿里云盘（带重试）
├── cloud.py        # 网盘抽象层（Aligo 单例缓存）
├── dashboard.py    # 网页面板（127.0.0.1:8899）
├── history.py      # 历史记录
├── notify.py       # Telegram 通知
├── hash_dedup.py   # 内容哈希去重 + 广告过滤
├── disc_cache.py   # 评论区 SQLite 缓存
├── getbot_cache.py # getbot 任务缓存
└── alipan.py       # 阿里云盘封装

config/
├── tg_config.json.example
├── bot_config.json.example
└── proxy.json.example
```

## 云端目录布局

```
TG收藏/          # 收藏夹同步
TG链接/          # t.me 链接下载
91/              # 91porn 同步
```

## 安全

- 面板默认只绑 `127.0.0.1`，公网访问需 nginx 反代
- Token 比较用 `hmac.compare_digest`（防时序攻击）
- 敏感配置不进代码库（见 `.gitignore`）

## 版本

各模块独立版本号，见文件头 `VERSION` 常量。

## License

MIT
