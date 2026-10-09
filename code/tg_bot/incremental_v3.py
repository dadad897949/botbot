"""增量同步 v3：云端全局搜索版。

不找目录，不依赖路径。对每个 msg_id，直接在云端全局搜索文件名。
- 找到 → 已存在，跳过
- 找不到 → 需要下载

彻底避开 /备份文件 vs / 的路径坑。
"""

VERSION = "3.0.0"

import re


def parse_msg_id_from_msg(msg):
    """从 Telegram 消息对象获取 msg_id。"""
    return getattr(msg, 'id', 0)


def expected_filename(msg):
    """根据消息生成期望的文件名（与 _extract_msg_name 逻辑一致）。
    
    照片会带"第X期"前缀，如：第20期_photo_24951.jpg
    搜索时用模糊匹配，只要包含 photo_24951.jpg 就算找到。
    """
    import re
    is_photo = bool(getattr(msg, 'photo', None))
    _doc = getattr(msg, 'document', None)
    if not is_photo and _doc:
        _mt = getattr(_doc, 'mime_type', '') or ''
        if _mt.startswith('image'):
            is_photo = True
    
    # 提取期数
    _episode = ''
    _caption = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
    if _caption:
        _m = re.search(r'第\s*\d+\s*期', _caption)
        if _m:
            _episode = _m.group(0).replace(' ', '') + '_'
    
    if is_photo:
        _file = getattr(msg, 'file', None)
        _raw = getattr(_file, 'name', None) if _file else None
        if _raw:
            import os
            _name = os.path.basename(_raw.replace('/', '_').replace('\\', '_'))
            if _episode and not _name.startswith(_episode):
                _name = _episode + _name
            return _name
        return '%sphoto_%d.jpg' % (_episode, getattr(msg, 'id', 0))
    
    # 视频：尝试从 file.name 获取，否则用 video_id.mp4
    _file = getattr(msg, 'file', None)
    _raw = getattr(_file, 'name', None) if _file else None
    if _raw:
        import os
        return os.path.basename(_raw.replace('/', '_').replace('\\', '_'))
    return 'video_%d.mp4' % getattr(msg, 'id', 0)


def file_exists_in_cloud(cloud_drive, filename):
    """在云端全局搜索文件，返回 (是否存在, 所在目录fid, 所在目录名)。
    
    处理：
    1. 精确匹配文件名
    2. 照片：提取 msg_id，模糊搜 photo_{id}.jpg（兼容新旧命名）
    3. 视频切分：如果 filename 是视频，同时检查 xxx_part01of03.mp4
    
    Args:
        cloud_drive: aligo 对象
        filename: 如 '第20期_photo_24951.jpg' 或 'video_123.mp4'
    
    Returns:
        (exists: bool, dir_fid: str|None, dir_name: str|None)
    """
    import re
    try:
        # 1. 精确搜原文件名
        _results = cloud_drive.search_files(filename)
        for _r in _results:
            if getattr(_r, 'name', '') == filename:
                _parent = getattr(_r, 'parent_file_id', None)
                return True, _parent, None
        
        # 2. 照片：提 msg_id 模糊搜（兼容 第20期_photo_24951.jpg 和 photo_24951.jpg）
        _m = re.search(r'photo_(\d+)\.jpg', filename)
        if _m:
            _mid = _m.group(1)
            _pattern = 'photo_%s.jpg' % _mid
            _results2 = cloud_drive.search_files(_pattern)
            for _r2 in _results2:
                _n2 = getattr(_r2, 'name', '')
                if _pattern in _n2:  # 包含即可，兼容前缀
                    _parent2 = getattr(_r2, 'parent_file_id', None)
                    return True, _parent2, None
        
        # 3. 视频切分：xxx.mp4 → 搜 xxx_part*of*.mp4
        if filename.lower().endswith('.mp4'):
            _base = filename[:-4]
            _results3 = cloud_drive.search_files(_base)
            for _r3 in _results3:
                _n3 = getattr(_r3, 'name', '')
                if _n3.startswith(_base + '_part') and _n3.endswith('.mp4'):
                    _parent3 = getattr(_r3, 'parent_file_id', None)
                    print('INCREMENTAL_V3 found split: %s' % _n3, flush=True)
                    return True, _parent3, None
        
        return False, None, None
    except Exception as e:
        print('INCREMENTAL_SEARCH_FAIL %s: %r' % (filename, e), flush=True)
        return False, None, None


def get_folder_name_by_fid(cloud_drive, fid):
    """通过 fid 获取文件夹名称。"""
    try:
        _info = cloud_drive.get_file(fid)
        return getattr(_info, 'name', None)
    except Exception:
        return None


async def incremental_sync_v3(cloud_drive, collected):
    """v3 主流程：云端全局搜索去重，找到旧目录并复用。
    
    1. 对每个文件全局搜索是否存在
    2. 统计找到文件的目录，选出现最多的那个作为"旧目录"
    3. 缺失文件将上传到旧目录（由调用方通过 folder_override 实现）
    
    Args:
        cloud_drive: aligo 对象
        collected: Telegram 消息对象列表
    
    Returns:
        (missing_msgs, existing_count, reuse_dir_fid, reuse_dir_name)
    """
    import asyncio
    from collections import Counter
    loop = asyncio.get_event_loop()
    
    _missing = []
    _existing = 0
    _dir_counter = Counter()
    _dir_names = {}  # fid -> name
    
    for _msg in collected:
        _fname = expected_filename(_msg)
        _exists, _dir_fid, _ = await loop.run_in_executor(
            None, file_exists_in_cloud, cloud_drive, _fname)
        if _exists:
            _existing += 1
            if _dir_fid:
                _dir_counter[_dir_fid] += 1
        else:
            _missing.append(_msg)
    
    # 找出出现最多的目录作为复用目标
    _reuse_fid = None
    _reuse_name = None
    if _dir_counter:
        _reuse_fid, _count = _dir_counter.most_common(1)[0]
        print('INCREMENTAL_V3 reuse dir %s (%d files)' % (_reuse_fid[:8], _count), flush=True)
        # 尝试获取目录名
        try:
            # 这里简化，调用方会通过 folder_override 传名称
            # 暂时不获取名称，用 fid 即可
            pass
        except Exception:
            pass
    
    print('INCREMENTAL_V3: %d collected, %d existing (global), %d missing' % (
        len(collected), _existing, len(_missing)), flush=True)
    return _missing, _existing, _reuse_fid, _reuse_name
