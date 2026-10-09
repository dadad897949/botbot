#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记录集合 —— 同步历史记录、面板状态快照。

VERSION = "1.0.2"  # 2026-10-05: snapshot 返回前端期望的完整格式
"""
VERSION = "1.0.2"

import json
import os
import threading
import time

_history_lock = threading.Lock()

# 由 runner.init() 注入
_HISTORY_FILE = None
_tasks_mod = None
_transfer_mod = None


def init(history_file, tasks_mod, transfer_mod, transfer_status=None):
    """初始化记录模块。"""
    global _HISTORY_FILE, _tasks_mod, _transfer_mod, _TRANSFER_STATUS
    _HISTORY_FILE = history_file
    _tasks_mod = tasks_mod
    _transfer_mod = transfer_mod
    _TRANSFER_STATUS = transfer_status


def _mb(n):
    return '%.1fMB' % (n / 1048576)


def _fmt_elapsed(sec):
    sec = int(sec)
    if sec < 60:
        return '%d秒' % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return '%d分%d秒' % (m, s)
    h, m = divmod(m, 60)
    return '%d小时%d分' % (h, m)


def log_history(entry):
    with _history_lock:
        try:
            with open(_HISTORY_FILE, 'a') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            try:
                with open(_HISTORY_FILE) as f:
                    lines = f.readlines()
                if len(lines) > 1000:
                    tmp = _HISTORY_FILE + '.tmp'
                    with open(tmp, 'w') as f:
                        f.writelines(lines[-500:])
                    os.replace(tmp, _HISTORY_FILE)
            except OSError:
                pass
        except OSError as e:
            print('HISTORY_WRITE_FAIL %s' % e, flush=True)


def read_history(n=10):
    with _history_lock:
        try:
            with open(_HISTORY_FILE) as f:
                lines = f.readlines()
            out = []
            for line in lines[-n:]:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
            return out
        except OSError:
            return []


def delete_history_entry(entry_id):
    """按 id 删除一条同步记录（原子替换）。返回是否删掉。"""
    with _history_lock:
        try:
            with open(_HISTORY_FILE) as f:
                lines = f.readlines()
        except OSError:
            return False
        kept = []
        removed = False
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                kept.append(line)
                continue
            if not removed and str(r.get('id', '')) == str(entry_id):
                removed = True
                continue
            kept.append(line)
        if not removed:
            return False
        try:
            tmp = _HISTORY_FILE + '.tmp'
            with open(tmp, 'w') as f:
                f.writelines(kept)
            os.replace(tmp, _HISTORY_FILE)
            return True
        except OSError:
            return False


def clear_history():
    """一键清空所有同步记录。"""
    with _history_lock:
        try:
            tmp = _HISTORY_FILE + '.tmp'
            with open(tmp, 'w') as f:
                pass
            os.replace(tmp, _HISTORY_FILE)
            return True
        except OSError:
            return False


def snapshot():
    """面板状态快照：活动任务 + 最近历史。返回前端期望的格式。"""
    items = []
    queued = []
    now = time.time()
    # 用 list() 复制，避免 dashboard 线程遍历时事件循环线程修改 dict
    active = _tasks_mod.active
    # 加上 getbot pipeline 任务（不在 tasks.active 里）
    try:
        _gb = (_TRANSFER_STATUS or {}).get('getbot', {})
        if _gb and _gb.get('phase') not in (None, 'done', ''):
            _total = _gb.get('total', 0) or 1
            _done = _gb.get('downloaded', 0)
            items.append({
                'key': 'getbot',
                'label': '取文件 %s' % _gb.get('bot_username', ''),
                'phase': 'downloading',
                'total': _total,
                'done': _done,
                'pct': min(100, int(_done * 100 / _total)),
            })
    except Exception:
        pass
    for key in list(active):
        st = active.get(key)
        if st is None:
            continue
        total = st.get('total') or 1
        done = st.get('done', 0)
        bar = st.get('_bar')
        if bar is not None and getattr(bar, 'total', None):
            done = st.get('base', 0) + bar.n
        pct = min(100, int(done * 100 / total))
        elapsed = now - st.get('t0', now)
        # 实时速度：用最近 60 秒采样算；ETA 据此推
        samples = st.get('_samples') or []
        cur_speed = 0
        if len(samples) >= 2:
            dt = samples[-1][0] - samples[0][0]
            dd = samples[-1][1] - samples[0][1]
            if dt > 1 and dd > 0:
                cur_speed = dd / dt
        if cur_speed > 0:
            speed = '%.1fMB/s' % (cur_speed / 1048576)
        elif elapsed > 5 and done > 0:
            speed = '%.1fMB/s' % (done / elapsed / 1048576)
        else:
            speed = ''
        eta = ''
        if cur_speed > 0 and total > done:
            eta = _fmt_elapsed((total - done) / cur_speed)
        item = {
            'key': key,
            'label': st.get('label', key),
            'phase': st.get('phase', '传输中'),
            'pct': pct,
            'done': _mb(done),
            'total': _mb(total),
            'speed': speed,
            'elapsed': _fmt_elapsed(elapsed),
            'eta': eta,
        }
        # 排队中的任务单独放，不跟正在运行的混在一起显示
        if st.get('placeholder'):
            queued.append(item)
        else:
            items.append(item)
    hist = []
    for r in read_history(20):
        hist.append({
            'id': r.get('id', ''),
            'ts': r.get('ts', ''),
            'result': r.get('result', ''),
            'folder': r.get('folder', ''),
            'size': _mb(r.get('size', 0)),
            'parts': r.get('parts', 0),
            'duration': r.get('duration', ''),
            'name': r.get('name', ''),
        })
    return {'active': items, 'queued': queued, 'history': hist, 'time': int(now)}
