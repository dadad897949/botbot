VERSION = "1.0.0"  # 2026-10-06: 结构化日志

"""结构化日志：借鉴 Douyin_TikTok_Download_API 的可观测性。

每个关键事件一条 JSON，方便 grep / 分析。
格式：{"ts": ..., "event": "...", ...fields}
"""

import json
import time


def log(event, **fields):
    """输出一条结构化日志。"""
    rec = {'ts': int(time.time()), 'event': event}
    rec.update(fields)
    try:
        print('JLOG ' + json.dumps(rec, ensure_ascii=False), flush=True)
    except Exception:
        pass


# 快捷函数
def task_start(task_type, **kw):
    log('task_start', type=task_type, **kw)


def task_done(task_type, ok, total, elapsed, **kw):
    log('task_done', type=task_type, ok=ok, total=total, elapsed_s=round(elapsed, 1), **kw)


def task_fail(task_type, error, **kw):
    log('task_fail', type=task_type, error=str(error)[:200], **kw)


def download_start(name, size, **kw):
    log('download_start', name=name, size=size, **kw)


def download_done(name, size, elapsed, speed, **kw):
    log('download_done', name=name, size=size, elapsed_s=round(elapsed, 1),
        speed_mbps=round(speed / 1048576, 2), **kw)


def upload_start(name, size, **kw):
    log('upload_start', name=name, size=size, **kw)


def upload_done(name, size, elapsed, **kw):
    log('upload_done', name=name, size=size, elapsed_s=round(elapsed, 1), **kw)


def cache_hit(key, **kw):
    log('cache_hit', key=key, **kw)


def cache_miss(key, **kw):
    log('cache_miss', key=key, **kw)
