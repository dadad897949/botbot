# -*- coding: utf-8 -*-
"""
getbot 内容哈希去重 + 广告白名单（2026-10-08）
方案1：用 Telegram document.id 做内容哈希，跨任务去重
方案2：广告视频白名单，直接过滤
"""
import json
import os

# 本地索引文件
_HASH_INDEX = '/root/TG转存机器人/getbot_hash_index.json'
_AD_WHITELIST = '/root/TG转存机器人/getbot_ad_whitelist.json'


def _load_json(path, default):
    # 2026-10-08 fix: 索引损坏时打日志并保留坏文件，不再静默丢历史
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except FileNotFoundError:
        pass
    except Exception as e:
        print('HASH_DEDUP load fail %s: %r' % (path, e), flush=True)
        try:
            os.replace(path, path + '.bak')
        except Exception:
            pass
    return default


def _save_json(path, data):
    # 2026-10-08 fix: 原子写入（临时文件+os.replace），崩溃中途不留半截文件
    try:
        _tmp = path + '.tmp'
        with open(_tmp, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp, path)
    except Exception as e:
        print('HASH_INDEX_SAVE_FAIL: %r' % e, flush=True)
        try:
            os.remove(path + '.tmp')
        except Exception:
            pass


def get_file_uid(msg):
    """取文件的内容唯一 ID（Telegram document.id，同一文件跨 bot 不变）"""
    try:
        # 优先用 document.id
        doc = getattr(msg, 'document', None)
        if doc and getattr(doc, 'id', None):
            return str(doc.id)
        # 照片用 photo.id
        photo = getattr(msg, 'photo', None)
        if photo and getattr(photo, 'id', None):
            return 'photo_%s' % photo.id
        # 兜底：file.id
        f = getattr(msg, 'file', None)
        if f and getattr(f, 'id', None):
            return str(f.id)
    except Exception:
        pass
    return None


# 2026-10-09：去重函数已删除（用户要求去掉所有去重）

def is_ad_video(msg):
    """查广告白名单（方案2）"""
    wl = _load_json(_AD_WHITELIST, {'uids': [], 'keywords': []})
    # 1. 按 uid 匹配
    uid = get_file_uid(msg)
    if uid and uid in wl.get('uids', []):
        return True
    # 2. 按文件名关键词匹配
    try:
        f = getattr(msg, 'file', None)
        fname = (getattr(f, 'name', '') or '').lower()
        for kw in wl.get('keywords', []):
            if kw.lower() in fname:
                return True
    except Exception:
        pass
    return False


def add_ad_uid(uid):
    """手动加广告 uid 到白名单"""
    wl = _load_json(_AD_WHITELIST, {'uids': [], 'keywords': []})
    if uid and uid not in wl['uids']:
        wl['uids'].append(uid)
        _save_json(_AD_WHITELIST, wl)
        return True
    return False


def file_md5(path, max_mb=100):
    """算文件 md5（大学校只读前 max_mb MB，防慢）
    从 telegram_media_downloader 学到：同名文件用 md5 定胜负"""
    import hashlib
    h = hashlib.md5()
    try:
        _limit = max_mb * 1024 * 1024
        _read = 0
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                h.update(chunk)
                _read += len(chunk)
                if _read >= _limit:
                    # 大文件只算前 100MB + 文件大小，保证速度的同时有区分度
                    h.update(str(__import__('os').path.getsize(path)).encode())
                    break
        return h.hexdigest()
    except Exception:
        return None


def is_duplicate_content(path1, path2):
    """两个文件内容是否相同（md5 比对）
    用于：同名但大小接近的文件，二次确认是否为同一内容"""
    if not path1 or not path2:
        return False
    try:
        import os
        # 大小差超过 1KB 直接判不同，不用算 md5
        if abs(os.path.getsize(path1) - os.path.getsize(path2)) > 1024:
            return False
        _m1, _m2 = file_md5(path1), file_md5(path2)
        # 2026-10-08 fix: 双 None 时返回 False，不误判为相同
        return bool(_m1 and _m1 == _m2)
    except Exception:
        return False
