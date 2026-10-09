# 目录树

```
tgbot/
├── README.md
├── TREE.md
├── .gitignore
├── code/
│   ├── tg_bot/
│   │   ├── __init__.py
│   │   ├── runner.py          # 主入口，双入口命令
│   │   ├── handlers.py        # 消息处理：下载、t.me、getbot、评论区
│   │   ├── transfer.py        # 上传到阿里云盘
│   │   ├── cloud.py           # 网盘抽象层
│   │   ├── dashboard.py       # 网页面板
│   │   ├── history.py         # 历史记录
│   │   ├── notify.py          # Telegram 通知
│   │   ├── tasks.py           # 任务管理
│   │   ├── alipan.py          # 阿里云盘封装
│   │   ├── alipan_login.py    # 阿里云盘登录
│   │   ├── hash_dedup.py      # 去重 + 广告过滤
│   │   ├── disc_cache.py      # 评论区缓存
│   │   ├── getbot_cache.py    # getbot 任务缓存
│   │   ├── fastdl.py          # 快速下载
│   │   ├── pipeline.py        # 下载流水线
│   │   ├── navigator.py       # getbot 导航器
│   │   ├── nav_core.py        # 导航核心
│   │   ├── nav_v2.py          # 导航 v2
│   │   ├── nav_group.py       # 分组导航
│   │   ├── nav_utils.py       # 导航工具
│   │   ├── incremental.py     # 增量同步
│   │   ├── incremental_v3.py
│   │   ├── incremental_v4.py
│   │   ├── health.py          # 健康检查
│   │   ├── fsutil.py          # 文件工具
│   │   ├── jlog.py            # 日志
│   │   └── learnings.py       # 学习记录
│   └── config/
│       ├── tg_config.json.example
│       ├── bot_config.json.example
│       └── proxy.json.example
├── ARCHITECTURE.md
└── tg-bot.service.example
```
