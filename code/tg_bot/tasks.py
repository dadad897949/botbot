#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务集合 —— 任务创建、取消、断点续传、进度。

VERSION = "1.1.0"  # 2026-10-06: 学 Paperclip wake reason，加 trigger 追踪  # 2026-10-05: 从 runner.py 抽离
"""
VERSION = "1.1.0"  # 2026-10-06: 学 Paperclip wake reason，加 trigger 追踪

import asyncio
import json
import os

# 由 runner.init() 注入
_INBOX = None


def init(inbox_dir):
    """初始化任务模块。"""
    global _INBOX
    _INBOX = inbox_dir


# key -> asyncio Task，用于面板取消任务
task_by_key = {}
# 2026-10-08：取消标志位，下载循环定期检查（asyncio取消可能传不进阻塞的I/O）
_cancel_flags = {}

# 任务并发队列：最多同时跑 2 个转存任务（2026-10-08: 1→2；单任务 8 下载连接，
# 2 任务=16 正好等于 fastdl per-DC 上限，不触发 429；上传是瓶颈，2 任务可下载/上传重叠）
_task_sem = asyncio.Semaphore(2)  # 2026-10-08: 用户要求2
# 文件间延迟（秒）：降低连续下载触发 TG 限流的概率
_INTER_FILE_DELAY = 3

# 后台任务引用：防止被 GC，异常未取回时打日志
_bg_tasks = set()

# 活动任务状态：key -> {progress, speed, ...}，供面板读取
active = {}


_task_triggers = {}  # {task_id: trigger_reason}，学 Paperclip 的 wake reason

def spawn(coro, name='task', trigger='manual'):
    """spawn 后台任务。trigger 记录触发原因（manual/auto_sync/retry/getcomments 等），便于排查。"""
    t = asyncio.create_task(coro)
    _task_triggers[id(t)] = trigger

    def _done(t):
        _bg_tasks.discard(t)
        _task_triggers.pop(id(t), None)
        try:
            exc = t.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            print('BG_TASK_FAIL %s: %r' % (name, exc), flush=True)

    t.add_done_callback(_done)
    _bg_tasks.add(t)
    return t


def get_trigger(task):
    """查任务的触发原因。"""
    return _task_triggers.get(id(task), 'unknown')


def cancel_task(key):
    """取消一个进行中的任务。返回是否找到并取消。"""
    # 2026-10-08：先设置标志位，下载循环会检查
    _cancel_flags[key] = True
    t = task_by_key.get(key)
    if t is None or t.done():
        # 即使 task 找不到，标志位已设，返回 True 让面板有反馈
        # （task 可能在 active 但不在 task_by_key）
        return key in active
    # dashboard 跑在独立线程，必须用 call_soon_threadsafe 跨线程取消
    try:
        loop = t.get_loop()
        if loop.is_closed():
            return False
        loop.call_soon_threadsafe(t.cancel)
    except RuntimeError:
        # loop 已关闭，任务事实上已不会继续
        return False
    except Exception:
        return False
    return True


def is_cancelled(key):
    """检查任务是否被取消（下载循环调用）。"""
    return _cancel_flags.get(key, False)

def clear_cancel_flag(key):
    """清理取消标志（任务结束时）。"""
    _cancel_flags.pop(key, None)

def _resume_path(key):
    return os.path.join(_INBOX, key + '.resume.json')


def save_resume_state(key, state):
    """保存断点续传状态到磁盘。"""
    try:
        from .fsutil import atomic_write_json
        atomic_write_json(_resume_path(key), state)
    except (OSError, TypeError, ValueError):
        pass


def load_resume_state(key):
    """加载断点续传状态，不存在返回 None。"""
    try:
        with open(_resume_path(key)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def clear_resume_state(key):
    """清除断点续传状态。"""
    try:
        os.remove(_resume_path(key))
    except OSError:
        pass


def progress_of(key):
    """获取任务进度，供面板读取。"""
    st = active.get(key)
    if not st:
        return None
    return {
        'progress': st.get('progress', 0),
        'speed': st.get('speed', 0),
        'eta': st.get('eta', 0),
    }
