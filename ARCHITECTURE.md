# 架构与逻辑设计

## 设计思路

### 1. 为什么用用户号而不用 Bot Token

2026-10-02 起改用用户身份（`user_session`）运行，不再用 Bot Token。原因：
- Bot API 有 20MB 下载限制，用户号无此限制
- 用户号能读私密频道、收藏夹
- t.me 链接的媒体，bot 号可能解析成功但读不到内容（2026-10-09 souyunso 案例）

2026-10-09 确定：**所有下载走用户号，删除所有 bot 下载路径**（v2.7.0）。

### 2. 双入口命令对齐

痛点：收藏夹（用户号）和 Bot 号是两个独立的 TelegramClient，命令要两边都注册。

铁律（2026-10-07 `/clearchat` 教训）：加新命令时，两端必须一起加。用 `grep -n "命令名" tg_bot/runner.py` 确认两端都有。

### 3. 通知策略

- 收藏夹：完全静默，不打扰
- Bot 号：编辑模式，只留最新一条完成通知，24h 自动删
- **只发开始+完成，不要实时进度**（2026-10-05 确定）
- 范围下载：只发开头"📦 范围…"和结尾"✅ 完成"两条，中间 22 条的"收到相册"全部屏蔽（quiet 模式，v2.6.8）

### 4. 台账模式（防重启丢任务）

2026-10-07 从 Jack 的 1.9.3 学到：
- 先建云端资源、再做耗时操作时，必须先记账：`record_task(key, type, cloud)`
- 记账点：资源创建成功后、耗时操作前
- 销账点：所有退出路径（try/finally），`complete_task(key)`
- Key 带类型前缀（`tme:`/`cmt:`/`getbot`），重启清理时知道删哪个目录
- 只有"进程崩了"才留账，正常完成（成功/失败）都销账

### 5. 去重策略的变迁

- 2026-10-08：按文件名去重
- 2026-10-09：按 msg_id 去重（v2.6.1）
- 2026-10-09：Jack 要求删除所有去重（"不需要去重逻辑"），重复发链接会重复下载
- 保留：`_tme_processing` 并发 guard（防同一链接并发重复处理，不是业务去重）
- 保留：`is_ad_video` 广告过滤（白名单机制）

## 核心流程

### t.me 单条链接

```
handle_tme_link(event, text)
  ↓ 解析 t.me/频道/msg_id
_handle_tme_link_inner(event, text, entity_id, msg_id, chan_key, quiet=False)
  ↓ 用户号 get_entity → get_messages(ids=[msg_id])
  ↓ 相册检测：grouped_id 相同的归为一组
  ↓ 原子认领：_tasks.active 占位，防重复派发
  ↓ _prepare_cloud_link：建 TG链接/<频道>_<时间戳>/
  ↓ 逐个 handle_media → fastdl 下载 → transfer 上传
  ↓ notify 完成通知
```

### t.me 范围链接

```
_handle_tme_range(event, channel, start_id, end_id)
  ↓ 创建共享 cloud：TG链接/<频道>_<起始>-<结束>_<时间戳>/
  ↓ _done_ids = set()  # range 层按 ID 去重
  ↓ for mid in range(start, end+1):
  │     if mid in _done_ids: continue
  │     取消息，查 grouped_id
  │     如果是相册：把同组 ID 全加入 _done_ids
  │     _handle_tme_link_inner(..., quiet=True, cloud=_shared_cloud)
  ↓ 只发两条通知：开始和完成
```

**范围去重逻辑**（v2.6.8）：同一相册的 10 条只触发一次下载，不会重复找那 10 个。

### 收藏夹同步

```
@client.on(NewMessage)  # 监听 Saved Messages
  ↓ 相册：等 3 秒收齐（grouped_id）
  ↓ handle_media(event, msg)
  ↓ _dl = _client（用户号）
  ↓ fastdl 并行下载
  ↓ transfer.upload 到 TG收藏/<时间戳>/
  ↓ 静默（不通知）
```

### /getbot 流程

```
用户：/getbot @botname 口令
  ↓ 导航器识别 bot 类型（A/B/C 类）
  ↓ StartBotRequest 模拟点击
  ↓ 分页翻页收集媒体
  ↓ 广告过滤（is_ad_video）
  ↓ 下载→上传流水线（pipeline.py）
```

## 并发控制

- 转存任务级并发：`Semaphore(2)`（2026-10-08 从 1 提到 2）
- 同一链接并发 guard：`_tme_processing` dict，30 分钟超时自动清理
- 相册原子认领：`_tasks.active` 占位，防重复派发

## 已知问题

1. **重启孤儿文件**（2026-10-09 发现）：下载完成但服务重启，上传任务丢失，文件在 bot_inbox 躺着。需启动时扫描孤儿文件重传。
2. **getbot 完成通知缺口**（2026-10-06 未解决）：开始通知到了，但之后无后续通知。
3. **云盘上传进度不显示**（待解决）。
