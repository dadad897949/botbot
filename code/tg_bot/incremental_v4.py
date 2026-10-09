"""增量同步 v4：高效版。

只做 3 次 API 调用：
1. 列 TG收藏 下的所有文件夹
2. 取最新的 2 个，分别列出文件
3. 本地比对，不再逐个搜索

比 v3 的 99 次全局搜索快得多。
"""

VERSION = "4.0.0"

import re


def _extract_episode(msg):
    """从配文提取'第X期'标识。"""
    _caption = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
    if _caption:
        _m = re.search(r'第\s*\d+\s*期', _caption)
        if _m:
            return _m.group(0).replace(' ', '')
    return ''


def _expected_photo_name(msg):
    """生成照片的期望文件名（带期数前缀）。"""
    _ep = _extract_episode(msg)
    _prefix = (_ep + '_') if _ep else ''
    _file = getattr(msg, 'file', None)
    _raw = getattr(_file, 'name', None) if _file else None
    if _raw:
        import os
        _n = os.path.basename(_raw.replace('/', '_').replace('\\', '_'))
        if _prefix and not _n.startswith(_prefix):
            _n = _prefix + _n
        return _n
    return '%sphoto_%d.jpg' % (_prefix, getattr(msg, 'id', 0))


def _is_photo_msg(msg):
    is_photo = bool(getattr(msg, 'photo', None))
    _doc = getattr(msg, 'document', None)
    if not is_photo and _doc:
        _mt = getattr(_doc, 'mime_type', '') or ''
        if _mt.startswith('image'):
            is_photo = True
    return is_photo


async def incremental_sync_v4(cloud_drive, collected, base_dir='TG收藏'):
    """v4 主流程。
    
    1. 只列 2 个目录，本地比对
    2. 找到旧命名文件时，记录下来用于重命名为新命名
    
    Returns:
        (missing_msgs, existing_count, reuse_dir_name, rename_list)
        rename_list: [(file_id, old_name, new_name), ...] 需要重命名的
    """
    import asyncio
    from collections import Counter
    loop = asyncio.get_event_loop()
    
    # 1. 找 base_dir fid（2026-10-08：支持 TGBot）
    from tg_bot import cloud as _cm
    _tg_fid = await loop.run_in_executor(
        None, _cm._ensure_folder, cloud_drive, base_dir)
    
    # 2. 列出所有文件夹，取最新的 2 个
    from .incremental import find_last_getbot_dirs_sync
    _dirs = await loop.run_in_executor(
        None, find_last_getbot_dirs_sync, cloud_drive, _tg_fid, 2)
    
    if not _dirs:
        print('INCREMENTAL_V4 no dirs, full download', flush=True)
        return collected, 0, None, []
    
    # 3. 列出 2 个目录的文件
    # _name_to_info: 文件名 -> (file_id, dir_name)
    # _pid_to_info: photo msg_id -> (file_id, filename, dir_name)
    _existing_names = set()
    _name_to_size = {}  # 2026-10-08：文件名 -> 大小，防同名广告视频误判
    _pid_to_info = {}
    
    _dir_file_count = Counter()
    
    for _fid, _fname in _dirs:
        try:
            _files = await loop.run_in_executor(
                None, cloud_drive.get_file_list, _fid)
            _cnt = 0
            for _f in _files:
                _n = getattr(_f, 'name', '')
                _id = getattr(_f, 'file_id', '')
                if not _n:
                    continue
                _existing_names.add(_n)
                _sz = getattr(_f, 'size', 0) or 0
                # 同名文件保留最大的（或第一个），用于大小比对
                if _n not in _name_to_size or _sz > _name_to_size[_n]:
                    _name_to_size[_n] = _sz
                _m = re.search(r'photo_(\d+)\.jpg', _n)
                if _m:
                    _pid = int(_m.group(1))
                    # 只保留第一个（避免重复）
                    if _pid not in _pid_to_info:
                        _pid_to_info[_pid] = (_id, _n, _fname)
                _cnt += 1
            _dir_file_count[_fname] = _cnt
            print('INCREMENTAL_V4 %s: %d files' % (_fname, _cnt), flush=True)
        except Exception as e:
            print('INCREMENTAL_V4 list fail %s: %r' % (_fname, e), flush=True)
    
    # 4. 本地比对，收集需要重命名的
    _missing = []
    _rename_list = []  # [(file_id, old_name, new_name)]
    
    for _msg in collected:
        _found = False
        if _is_photo_msg(_msg):
            _exp = _expected_photo_name(_msg)  # 如：第20期_photo_24951.jpg
            if _exp in _existing_names:
                _found = True
            else:
                _mid = getattr(_msg, 'id', 0)
                if _mid in _pid_to_info:
                    _found = True
                    _fid_old, _old_name, _dir = _pid_to_info[_mid]
                    # 如果旧名和新名不同，加入重命名列表
                    if _old_name != _exp:
                        _rename_list.append((_fid_old, _old_name, _exp))
        else:
            # 视频：精确匹配 + 切分 part 文件检查
            _file = getattr(_msg, 'file', None)
            _raw = getattr(_file, 'name', None) if _file else None
            if _raw:
                import os
                _vn = os.path.basename(_raw.replace('/', '_').replace('\\', '_'))
                if _vn in _existing_names:
                    # 2026-10-08：加大小校验，防同名广告视频误判
                    _msg_sz = getattr(_file, 'size', 0) or 0
                    _cloud_sz = _name_to_size.get(_vn, 0)
                    # 大小都为0或相差小于1KB才算同一个文件
                    if _msg_sz == 0 or _cloud_sz == 0 or abs(_msg_sz - _cloud_sz) < 1024:
                        _found = True
                    else:
                        print('INCREMENTAL_V4 size mismatch %s: msg=%d cloud=%d' % (
                            _vn, _msg_sz, _cloud_sz), flush=True)
                else:
                    # 检查切分文件：xxx.mp4 → xxx_part01of03.mp4
                    if _vn.lower().endswith('.mp4'):
                        _base = _vn[:-4]
                        for _en in _existing_names:
                            if _en.startswith(_base + '_part') and _en.endswith('.mp4'):
                                _found = True
                                print('INCREMENTAL_V4 found split: %s' % _en, flush=True)
                                break
        
        if not _found:
            _missing.append(_msg)
    
    _existing = len(collected) - len(_missing)
    
    # 5. 选文件最多的目录作为复用目标
    _reuse_name = None
    if _dir_file_count:
        _reuse_name = _dir_file_count.most_common(1)[0][0]
    
    print('INCREMENTAL_V4: %d collected, %d existing, %d missing, %d to rename, reuse=%s' % (
        len(collected), _existing, len(_missing), len(_rename_list), _reuse_name), flush=True)
    return _missing, _existing, _reuse_name, _rename_list


async def rename_old_files(cloud_drive, rename_list):
    """批量重命名旧文件。
    
    Args:
        rename_list: [(file_id, old_name, new_name), ...]
    Returns:
        成功数量
    """
    import asyncio
    loop = asyncio.get_event_loop()
    _ok = 0
    for _fid, _old, _new in rename_list:
        try:
            await loop.run_in_executor(None, cloud_drive.rename_file, _fid, _new)
            print('INCREMENTAL_V4 renamed: %s -> %s' % (_old, _new), flush=True)
            _ok += 1
        except Exception as e:
            print('INCREMENTAL_V4 rename fail %s: %r' % (_old, e), flush=True)
    return _ok
