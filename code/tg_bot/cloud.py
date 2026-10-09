#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多网盘调度层 —— 阿里云盘。

对上层只暴露一个入口：

    prepare(route, name, duration) -> ([Target, ...], [(key, 错误说明), ...])

route 决定云端目录放哪：
    'media'  收藏夹同步 —— 建「TG收藏/中文名_时间戳」（2026-10-08：短视频共享目录已移除）
    'link'   t.me 链接   —— 进「TG链接/链接_时间戳」
    'album'  相册        —— 进「TG收藏/相册_时间戳」

多块盘用**同一个目录名**，看起来才整齐。返回值里的 Target 带着已经建好的连接和
目录 ID，直接喂给 transfer.transfer_video 就能传（两边的接口是一样的）。

配置文件 cloud_config.json：
    {"targets": ["ali"]}       # 顺序就是上传顺序；只写一个就是单传
"""
from __future__ import annotations
VERSION = "1.2.0"  # 2026-10-05: 取消时删非空独立目录

import json
import os
import re
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'cloud_config.json')

KEYS = ('ali',)
LABEL = {'ali': '阿里云盘'}

_lock = threading.Lock()
_alert_at = [0.0]




class Target:
    """一块盘 + 一个已经建好的目录。"""

    __slots__ = ('key', 'label', 'drive', 'fid', 'folder', 'split_limit', 'reuse')

    def __init__(self, key, drive, fid, folder, reuse=False):
        self.key = key
        self.label = LABEL.get(key, key)
        self.drive = drive
        self.fid = fid
        self.reuse = reuse
        self.folder = folder


# ------------------------------------------------------------------ 配置

def load_config():
    cfg = {'targets': ['ali']}
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get('targets'), list):
            t = [k for k in d['targets'] if k in KEYS]
            if t:
                cfg['targets'] = t
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    # 原子写：写到一半被杀时，load_config 读到截断 JSON 会静默回退成默认网盘，丢掉用户的选择
    from .fsutil import atomic_write_json
    atomic_write_json(CONFIG_FILE, cfg, ensure_ascii=False, indent=2)
    return cfg


def targets():
    return load_config()['targets']


def set_targets(keys):
    keys = [k for k in keys if k in KEYS]
    if not keys:
        raise ValueError('至少要留一块网盘')
    cfg = load_config()
    cfg['targets'] = keys
    save_config(cfg)
    return keys


def labels(keys=None):
    return [LABEL.get(k, k) for k in (keys or targets())]


# --------------------------------------------------------------- 连接与目录

def sanitize(name):
    """云端文件名/文件夹名安全化。"""
    name = (name or '').replace('/', '_').replace('\\', '_').strip()
    name = ''.join(c for c in name if ord(c) >= 32)
    return name[:120] or '未命名'


def _cn(text, max_len=20):
    cn = ''.join(re.findall(r'[\u4e00-\u9fff]+', text or ''))
    return cn[:max_len]


def connect(key):
    """建立指定网盘的连接；连不上直接抛异常，由上层汇报给用户。"""
    if key == 'ali':
        from tg_bot.alipan import get_ali
        return get_ali()
    raise ValueError('未知网盘: %s' % key)


def _ensure_folder(drive, name):
    """根目录下的文件夹，有就复用，没有就建。"""
    name = sanitize(name)
    for f in drive.get_file_list(parent_file_id='root'):
        if f.type == 'folder' and f.name == name:
            return f.file_id
    return drive.create_folder(name=name, parent_file_id='root').file_id


def _ensure_subfolder(drive, parent_fid, name):
    name = sanitize(name)
    for f in drive.get_file_list(parent_file_id=parent_fid):
        if f.type == 'folder' and f.name == name:
            return f.file_id, f.name
    return drive.create_folder(name=name, parent_file_id=parent_fid).file_id, name


def _unique_name(drive, parent_fid, base):
    """同一秒并发建目录时防撞车：撞了就加 _2 / _3。"""
    existing = {f.name for f in drive.get_file_list(parent_file_id=parent_fid)
                if f.type == 'folder'}
    if base not in existing:
        return base
    n = 2
    while '%s_%d' % (base, n) in existing:
        n += 1
    return '%s_%d' % (base, n)


# --------------------------------------------------------------- 主入口

def _common_prefix(names):
    """找文件名公共前缀：去扩展名、去尾部数字/序号，返回有意义的前缀。
    2026-10-05: 用 jieba 分词提取关键词，比纯正则更准。"""
    import re, os
    if not names:
        return ''
    # 去扩展名
    bases = [os.path.splitext(n)[0] for n in names]
    # 去尾部序号：_part01, _01, -1, (1) 等
    cleaned = []
    for b in bases:
        c = re.sub(r'[\s_\-]*\(?\d+\)?$', '', b)  # 尾部数字
        c = re.sub(r'[\s_\-]*part\d*$', '', c, flags=re.I)  # part序号
        cleaned.append(c.strip())
    # 2026-10-05: 用 jieba 提取关键词（取第一个有意义的名词短语）
    try:
        import jieba
        # 用第一个文件名提取关键词
        if cleaned and cleaned[0]:
            words = list(jieba.cut(cleaned[0]))
            # 取前2-3个有意义的词（长度>=2，非数字）
            keywords = [w for w in words if len(w) >= 2 and not w.isdigit()][:3]
            if keywords:
                # 如果关键词能覆盖原意，直接用
                kw = ''.join(keywords)
                if len(kw) >= 2 and len(kw) <= 12:
                    return kw
    except Exception:
        pass
    if not cleaned or not cleaned[0]:
        return ''
    # 找最长公共前缀
    prefix = cleaned[0]
    for c in cleaned[1:]:
        while not c.startswith(prefix) and prefix:
            prefix = prefix[:-1]
        if not prefix:
            break
    prefix = prefix.strip(' _-')
    # 太短无意义（<2字符）则返回空
    return prefix if len(prefix) >= 2 else ''


def prepare(route='media', name='', duration=0, names=None, folder_override=''):
    """建连接 + 在每块启用的盘上准备好同名目录。

    返回 (targets, errors)：
        targets —— 建好目录的 Target 列表（可能为空）
        errors  —— [(盘名, 出错原因)]，单块盘挂了不影响其它盘
    names —— 相册文件名列表，用于推断公共前缀命名目录
    folder_override —— 指定目录名（重试时复用原目录，不生成新的）
    """
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    if folder_override:
        # 重试复用原目录
        if route == 'link':
            base_name = 'TG链接'
        elif route == 'getbot':
            base_name = 'TGBot'
        else:
            base_name = 'TG收藏'
        # 2026-10-08 fix: 入口先清洗，防全非法字符撞进同一个「未命名」目录
        folder, reuse = sanitize(folder_override), True
    elif route == 'link':
        base_name, folder = 'TG链接', '链接_%s' % stamp
        reuse = False
    elif route == 'album':
        # 文件名推断（2026-10-04）：用公共前缀+时间戳命名，防重名
        # 2026-10-05 Jack 要求：加上时间
        prefix = _common_prefix(names) if names else ''
        if prefix:
            base_name, folder = 'TG收藏', '%s_%s' % (prefix, stamp)
        else:
            base_name, folder = 'TG收藏', '相册_%s' % stamp
        reuse = False
    elif route == 'getbot':
        # 2026-10-08 Jack 要求：getbot 上传到 TGBot 目录
        prefix = _common_prefix(names) if names else ''
        if prefix:
            base_name, folder = 'TGBot', '%s_%s' % (prefix, stamp)
        else:
            base_name, folder = 'TGBot', 'getbot_%s' % stamp
        reuse = False
    else:                                   # media，长视频
        cn = _cn(name)
        base_name = 'TG收藏'
        folder = '%s_%s' % (cn, stamp) if cn else stamp
        reuse = False

    out, errors = [], []
    with _lock:
        live = []
        for key in targets():
            try:
                live.append((key, connect(key)))
            except Exception as e:          # 单块盘失败不影响另一块
                errors.append((LABEL.get(key, key), str(e)[:160]))

        # 目录名以第一块能用的盘为准算一次唯一名，其余盘照搬，保证两边一致
        if live:
            first_key, first = live[0]
            first_base = _ensure_folder(first, base_name)
            if not reuse:
                folder = _unique_name(first, first_base, folder)

        for key, drive in live:
            try:
                base_fid = _ensure_folder(drive, base_name)
                fid, real = _ensure_subfolder(drive, base_fid, folder)
                out.append(Target(key, drive, fid, real, reuse))
            except Exception as e:
                errors.append((LABEL.get(key, key), '建目录失败: %s' % str(e)[:140]))
    return out, errors


def cleanup(targets_):
    """任务失败/取消时清掉云端目录，best-effort。

    2026-10-05 改：独立目录（非共享）直接删，含未传完的文件；
    共享目录（短视频复用）只删空目录，避免误删别人的文件。
    """
    for t in targets_ or []:
        try:
            # 共享目录：只删空的
            if t.reuse:
                if t.drive.get_file_list(parent_file_id=t.fid):
                    print('CLOUD_CLEANUP %s skip shared non-empty %s' % (t.label, t.fid),
                          flush=True)
                    continue
            # 独立目录：直接丢回收站（含未传完的文件）
            t.drive.move_file_to_trash(t.fid)
            print('CLOUD_CLEANUP %s removed %s (reuse=%s)' % (t.label, t.fid, t.reuse),
                  flush=True)
        except Exception as e:
            print('CLOUD_CLEANUP %s failed %s: %r' % (t.label, t.fid, e), flush=True)


def health():
    """给 /drive 命令用：逐块盘探一次登录态。"""
    result = []
    for key in KEYS:
        try:
            connect(key)
            result.append((key, LABEL[key], True, 'ok'))
        except Exception as e:
            result.append((key, LABEL[key], False, str(e)[:120]))
    return result
