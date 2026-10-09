"""增量同步：/getbot 复用上一个同 bot 的云端目录，只下载缺失文件。

设计文档：~/workspace/your_files/incremental_sync_design.md

核心：
1. 去云端找上一个同 bot 的目录（实时 API，不靠本地记录）
2. 去云端列该目录的实际文件，直接匹配 msg_id
3. 同 bot → 增量补齐；找不到 → 新建目录全量

版本：1.0.0 (2026-10-07)
"""

VERSION = "1.0.0"

import re


def get_tg收藏_fid(cloud_drive):
    """获取 TG收藏 的 fid，优先找 /备份文件/TG收藏，找不到再找 /TG收藏。
    
    用户实际使用的是 /备份文件/TG收藏，bot 之前误用了根目录下的。
    """
    # 先找 /备份文件/
    _backup_fid = None
    try:
        for _f in cloud_drive.get_file_list('root'):
            if getattr(_f, 'name', '') == '备份文件' and (
                getattr(_f, 'is_folder', False) or getattr(_f, 'type', '') == 'folder'):
                _backup_fid = getattr(_f, 'file_id', None) or getattr(_f, 'id', None)
                break
    except Exception:
        pass
    
    if _backup_fid:
        # 在备份文件下找 TG收藏
        try:
            for _f in cloud_drive.get_file_list(_backup_fid):
                if getattr(_f, 'name', '') == 'TG收藏' and (
                    getattr(_f, 'is_folder', False) or getattr(_f, 'type', '') == 'folder'):
                    _fid = getattr(_f, 'file_id', None) or getattr(_f, 'id', None)
                    print('INCREMENTAL using /备份文件/TG收藏', flush=True)
                    return _fid
        except Exception:
            pass
    
    # 回退：根目录下的 TG收藏（旧行为）
    print('INCREMENTAL fallback to /TG收藏', flush=True)
    return None  # 让调用方用 _ensure_folder


def parse_msg_id_from_name(filename):
    """从文件名解析 msg_id。
    photo_23571.jpg → 23571
    video_23572.mp4 → 23572
    第14期_...mp4 → None（无 msg_id）
    """
    _m = re.search(r'(?:photo|video)_(\d+)\.', filename or '')
    if _m:
        return int(_m.group(1))
    return None


def diff_files(collected_msgs, cloud_files):
    """去云端匹配：比对新收集的消息 vs 云端目录实际文件。
    
    Args:
        collected_msgs: 新收集的 Telegram 消息对象列表
        cloud_files: 云端目录的实际文件列表（需有 .name 属性）
    
    Returns:
        (missing_msgs, existing_count)
    """
    _cloud_ids = set()
    for _f in cloud_files:
        _mid = parse_msg_id_from_name(getattr(_f, 'name', ''))
        if _mid:
            _cloud_ids.add(_mid)
    
    _missing = [m for m in collected_msgs if getattr(m, 'id', 0) not in _cloud_ids]
    return _missing, len(collected_msgs) - len(_missing)


def find_last_getbot_dirs_sync(cloud_drive, root_fid, count=2):
    """同步版：去云端找最近的 N 个 /getbot 生成的目录。
    
    按时间找最近的 N 个 相册_* 目录（不按 bot 名匹配）。
    必须调阿里云 API 实时列，不能靠本地记录。
    
    Args:
        cloud_drive: aligo 对象（需有 get_file_list）
        root_fid: TG收藏/ 的 fid
        count: 取最近几个，默认 2
    
    Returns:
        [(folder_id, folder_name), ...] 按时间倒序，或空列表
    """
    try:
        _items = cloud_drive.get_file_list(root_fid)
    except Exception as e:
        print('INCREMENTAL_ROOT_LIST_FAIL: %r' % e, flush=True)
        return []
    
    _candidates = []
    for _it in _items:
        _is_folder = getattr(_it, 'is_folder', False) or getattr(_it, 'type', '') == 'folder'
        _name = getattr(_it, 'name', '') or ''
        # 取所有文件夹，排除已知的非 getbot 目录
        # （短视频是共享目录，深南第一深情是单视频，都不是 getbot 批量）
        if _is_folder and _name not in ('短视频',):
            _candidates.append(_it)
    
    if not _candidates:
        return []
    
    # 按名称倒序（时间戳在名称中，新的在前）
    _candidates.sort(key=lambda x: getattr(x, 'name', ''), reverse=True)
    
    _result = []
    for _it in _candidates[:count]:
        _fid = getattr(_it, 'id', None) or getattr(_it, 'file_id', None)
        _fname = getattr(_it, 'name', '')
        _result.append((_fid, _fname))
        print('INCREMENTAL candidate dir: %s' % _fname, flush=True)
    
    return _result


def find_last_getbot_dir_sync(cloud_drive, root_fid, bot_username=None):
    """兼容旧接口：只取最近 1 个。"""
    _dirs = find_last_getbot_dirs_sync(cloud_drive, root_fid, count=1)
    if not _dirs:
        return None
    _fid, _fname = _dirs[0]
    # 读 manifest（可选）
    _manifest = {}
    try:
        _files = cloud_drive.get_file_list(_fid)
        for _f in _files:
            if getattr(_f, 'name', '') == '.manifest.json':
                break
    except Exception:
        pass
    return (_fid, _fname, _manifest)


async def incremental_sync(cloud_drive, root_fid, bot_username, collected):
    """增量同步主流程（去云端匹配版）。
    
    流程（用户 2026-10-07 确认）：
    1. 获取到链接后、下载前，去云端找最近的2个目录
    2. 分别比对文件重叠度，选重叠高的那个
    3. 有重叠 → 是同一个，增量补齐；无重叠 → 新建目录
    
    Args:
        cloud_drive: aligo 对象
        root_fid: TG收藏/ 的 fid
        bot_username: 保留参数
        collected: 新收集的 Telegram 消息对象列表
    
    Returns:
        (is_incremental, files_to_download, target_folder_id, target_folder_name)
    """
    import asyncio
    loop = asyncio.get_event_loop()
    
    # 1. 找最近的2个目录
    _dirs = await loop.run_in_executor(
        None, find_last_getbot_dirs_sync, cloud_drive, root_fid, 2)
    if not _dirs:
        return False, collected, None, None
    
    # 2. 分别比对，选重叠度最高的
    _best_fid, _best_fname, _best_missing, _best_existing = None, None, None, -1
    for _fid, _fname in _dirs:
        try:
            _cloud_files = await loop.run_in_executor(
                None, cloud_drive.get_file_list, _fid)
        except Exception as e:
            print('INCREMENTAL_LIST_FAIL %s: %r' % (_fname, e), flush=True)
            continue
        _missing, _existing = diff_files(collected, _cloud_files)
        print('INCREMENTAL diff %s: %d collected, %d existing, %d missing' % (
            _fname, len(collected), _existing, len(_missing)), flush=True)
        if _existing > _best_existing:
            _best_existing = _existing
            _best_fid, _best_fname = _fid, _fname
            _best_missing = _missing
    
    # 3. 决策
    if _best_fid is None or _best_existing <= 0:
        print('INCREMENTAL no overlap in 2 dirs, treat as new', flush=True)
        return False, collected, None, None
    if len(_best_missing) == 0:
        return True, [], _best_fid, _best_fname
    return True, _best_missing, _best_fid, _best_fname
