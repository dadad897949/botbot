"""下载流水线：两阶段生产者-消费者模式。

下载（TG）与上传（阿里云）重叠，提速 ~40%，但：
- 同一时刻只有 1 个 TG 下载（防 FloodWait）
- 同一时刻只有 1 个阿里云上传（防 429）

比 3 并发安全，比串行快。

版本：1.1.1 (2026-10-07)
"""

VERSION = "1.1.9"  # 2026-10-07: 修 DeepSeek 5 个 blocker（取消死锁/续传污染/size 校验/去 join/parts 泄漏）

import asyncio
import os
import re as _re
import shutil
import subprocess as _sp
import time


def _mb(n):
    try:
        n = float(n or 0)
    except Exception:
        n = 0.0
    if n >= 1073741824:
        return '%.1fGB' % (n / 1073741824)
    if n >= 1048576:
        return '%.1fMB' % (n / 1048576)
    if n >= 1024:
        return '%.1fKB' % (n / 1024)
    return '%dB' % int(n)


def _sanitize_name(name):
    """与 tg_bot.alipan.sanitize 同行为；import 失败时用本地兜底。"""
    try:
        from tg_bot.alipan import sanitize
        return sanitize(name)
    except Exception:
        name = (name or '').replace('/', '_').replace('\\', '_').strip()
        name = ''.join(c for c in name if ord(c) >= 32)
        return name[:120] or '未命名视频'


def _extract_name(msg, is_photo):
    """复用 handle_media 的文件名逻辑。"""
    _file = getattr(msg, 'file', None)
    _episode = ''
    _cap = getattr(msg, 'text', '') or getattr(msg, 'caption', '') or ''
    if _cap:
        _em = _re.search(r'第\s*\d+\s*期', _cap)
        if _em:
            _episode = _em.group(0).replace(' ', '') + '_'
    _raw_base = getattr(_file, 'name', None)
    if _raw_base:
        raw_name = _raw_base
        if is_photo and _episode:
            _bn = os.path.basename(_raw_base.replace('/', '_').replace('\\', '_'))
            if not _bn.startswith(_episode):
                raw_name = _episode + _bn
    else:
        if is_photo:
            raw_name = '%sphoto_%d.jpg' % (_episode, getattr(msg, 'id', 0))
        else:
            raw_name = 'video_%d.mp4' % getattr(msg, 'id', 0)
    # 防路径穿越
    name = os.path.basename(raw_name.replace('/', '_').replace('\\', '_')) \
        or ('video_%d.mp4' % getattr(msg, 'id', 0))
    return name


def _validate_complete(path, file_size_hint):
    """校验已存在的完整文件是否有效（复用 handle_media 逻辑）。"""
    try:
        _dl = path.lower()
        _st = os.stat(path)
        # 稀疏检查（所有类型）：预分配但没写满的文件，大小对、实际块数少。
        # 视频若 moov 在文件头，ffprobe 对截断文件也能读出时长，所以不能只靠 ffprobe
        if file_size_hint and _st.st_blocks * 512 < file_size_hint * 0.9:
            return False
        if _dl.endswith(('.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.m4v')):
            _r = _sp.run(['ffprobe', '-v', 'error', '-show_entries',
                          'format=duration', '-of',
                          'default=noprint_wrappers=1:nokey=1', path],
                         capture_output=True, text=True, timeout=30)
            return float(_r.stdout.strip() or 0) > 0
        return True
    except Exception:
        return False


async def download_phase(msg, dl_client, tmp_dir, state=None,
                         fastdl=None, sim_delay=None, refresh_cb=None):
    """阶段1：只从 TG 下载到本地，不上传。
    
    Args:
        msg: Telegram 消息对象（需有 .id, .photo/.document）
        dl_client: Telethon client（模拟时可为 None）
        tmp_dir: 本地临时目录
        state: 进度状态 dict（可选）
        fastdl: fastdl 模块（模拟时可为 None）
        sim_delay: 模拟下载耗时（秒），用于测试
    
    Returns:
        (local_path, file_info)
        file_info = {'name': str, 'size': int, 'is_photo': bool, 'msg_id': int}
    """
    msg_id = getattr(msg, 'id', 0)
    
    # 模拟模式：不真下载
    if sim_delay is not None:
        await asyncio.sleep(sim_delay)
        _name = 'sim_file_%d.dat' % msg_id
        _path = os.path.join(tmp_dir, _name)
        # 写一个小文件模拟
        with open(_path, 'wb') as f:
            f.write(b'0' * 1024)  # 1KB
        _info = {
            'name': _name,
            'size': 1024,
            'is_photo': False,
            'msg_id': msg_id,
        }
        if state is not None:
            state['downloading_name'] = _name
        print('PIPELINE_DL_DONE %s (sim, %.1fs)' % (_name, sim_delay), flush=True)
        return _path, _info
    
    # 2026-10-07 v1.1.3：refresh_cb 参数（替代 monkey patch）
    # getbot 收集的消息放久了 file_reference 会过期，调用方传入刷新回调
    if refresh_cb is not None:
        try:
            _rm = await refresh_cb(msg)
            if _rm is not None:
                msg = _rm
        except Exception:
            pass
    # 真实模式：从 handle_media 提取的下载逻辑（断点续传 + fastdl 并行 + FloodWait 重试）
    if dl_client is None:
        raise RuntimeError('真实下载需要 dl_client')
    try:
        from telethon.errors import FloodWaitError
    except Exception:
        FloodWaitError = None

    doc = getattr(msg, 'document', None)
    is_photo = bool(getattr(msg, 'photo', None)) or (
        doc is not None and (getattr(doc, 'mime_type', '') or '').startswith('image'))
    if not getattr(msg, 'photo', None) and doc is None and not getattr(msg, 'file', None):
        raise RuntimeError('消息无媒体内容，跳过')

    _name = _extract_name(msg, is_photo)
    chat_id = getattr(msg, 'chat_id', None) or 'dm'
    # tmp 内按 (聊天, 消息ID) 隔离，同名不撞车
    path = os.path.join(tmp_dir, '%s_%d_%s' % (chat_id, msg_id, _name))
    if not os.path.realpath(path).startswith(os.path.realpath(tmp_dir) + os.sep):
        raise RuntimeError('文件名非法，拒绝处理')

    _file = getattr(msg, 'file', None)
    file_size_hint = getattr(_file, 'size', 0) or 0

    # 磁盘预检
    try:
        _free = shutil.disk_usage(tmp_dir).free
    except Exception:
        _free = 0
    if file_size_hint and _free < file_size_hint:
        raise RuntimeError('磁盘空间不足：需要约 %s，可用 %s'
                           % (_mb(file_size_hint), _mb(_free)))

    # 断点续传：检查 tmp 内是否有未完成的部分文件
    resume_offset = 0
    if os.path.exists(path):
        existing = os.path.getsize(path)
        expect = file_size_hint
        if expect and 0 < existing < expect:
            _part = 512 * 1024
            resume_offset = (existing // _part) * _part
            print('PIPELINE_RESUME %s from %s/%s (aligned %s)'
                  % (_name, _mb(existing), _mb(expect), _mb(resume_offset)), flush=True)
        elif expect and existing >= expect:
            # ffprobe 最长阻塞 30s，放线程池，避免卡住事件循环
            if await asyncio.get_event_loop().run_in_executor(
                    None, _validate_complete, path, expect):
                print('PIPELINE_RESUME_DONE %s already complete, skip download' % _name,
                      flush=True)
                _info = {'name': _name, 'size': existing, 'is_photo': bool(is_photo),
                         'msg_id': msg_id}
                if state is not None:
                    state['downloading_name'] = _name
                return path, _info
            print('PIPELINE_RESUME_CORRUPT %s size ok but invalid, re-download' % _name,
                  flush=True)
            try:
                os.remove(path)
            except OSError:
                pass
            resume_offset = 0

    total = [1]
    last_upd = [0]

    def cb(cur, tot):
        total[0] = tot or 1
        if state is not None:
            state['downloading_name'] = _name
            state['dl_done'] = cur
            state['dl_total'] = total[0]
        if cur - last_upd[0] > 50 * 1048576:
            last_upd[0] = cur
            print('PIPELINE_DL %s %s/%s' % (_name, _mb(cur), _mb(total[0])), flush=True)

    dl_t0 = time.time()
    dl_mode = 'single'
    _last_fw = None
    for _flood_retry in range(4):
        try:
            try:
                if fastdl is not None:
                    # mode_out：拿回真实生效的模式（same-dc / exported / cdn），
                    # refresh_cb：下载中 file_reference 过期时自动换新引用重试
                    _mode = []
                    await fastdl.download_parallel(
                        dl_client, msg, path,
                        file_size_hint or None, progress_callback=cb,
                        resume_offset=resume_offset, mode_out=_mode,
                        refresh_cb=refresh_cb)
                    dl_mode = _mode[0] if _mode else ('parallel x%d' % fastdl.CONNECTIONS)
                else:
                    raise RuntimeError('no fastdl, use single')
            except Exception as e:
                if FloodWaitError is not None and isinstance(e, FloodWaitError):
                    raise
                print('PIPELINE_PARALLEL_DL fallback: %r' % (e,), flush=True)
                if resume_offset > 0:
                    # Blocker#2: 先 truncate 丢弃对齐点后的脏字节，避免新旧混合污染
                    with open(path, 'r+b') as _f:
                        _f.truncate(resume_offset)
                        _f.seek(resume_offset)
                        _done = resume_offset
                        async for _chunk in dl_client.iter_download(msg, offset=resume_offset):
                            _f.write(_chunk)
                            _done += len(_chunk)
                            cb(_done, file_size_hint or _done)
                    dl_mode = 'single-resumed'
                else:
                    try:
                        await asyncio.wait_for(
                            dl_client.download_media(msg, file=path, progress_callback=cb),
                            timeout=120)
                    except asyncio.TimeoutError:
                        raise RuntimeError('下载超时 120s（file_reference 可能过期）')
                    dl_mode = 'single'
            _last_fw = None
            break
        except Exception as fe:
            if FloodWaitError is not None and isinstance(fe, FloodWaitError):
                _last_fw = fe
                wait = (fe.seconds or 60) + 5
                print('PIPELINE_FLOOD_WAIT %s 等待 %ds 后重试' % (_name, wait), flush=True)
                await asyncio.sleep(wait)
                continue
            raise
    if _last_fw is not None:
        raise _last_fw

    dl_size = os.path.getsize(path)
    # Blocker#3: 最终 size 校验；不完整直接抛错，不进上传
    if file_size_hint and dl_size != file_size_hint:
        raise RuntimeError('下载不完整：%d != %d' % (dl_size, file_size_hint))
    dl_dt = time.time() - dl_t0
    print('PIPELINE_DL_DONE %s (%s, %s, %.1f MB/s)'
          % (_name, dl_mode, _mb(dl_size),
             dl_size / dl_dt / 1048576 if dl_dt > 0 else 0), flush=True)
    _info = {'name': _name, 'size': dl_size, 'is_photo': bool(is_photo), 'msg_id': msg_id}
    if state is not None:
        state['downloading_name'] = _name
    return path, _info


async def upload_phase(local_path, file_info, cloud, state=None,
                       transfer=None, sim_delay=None):
    """阶段2：只上传到阿里云，不下载。
    
    Args:
        local_path: 本地文件路径
        file_info: download_phase 返回的 info
        cloud: (targets, cerrors) 元组（模拟时可为 None）
        state: 进度状态 dict（可选）
        transfer: transfer 模块（模拟时可为 None）
        sim_delay: 模拟上传耗时（秒），用于测试
    
    Returns:
        (success: bool, cloud_names: list)
    """
    _name = file_info.get('name', os.path.basename(local_path))
    
    # 模拟模式
    if sim_delay is not None:
        await asyncio.sleep(sim_delay)
        if state is not None:
            state['uploading_name'] = _name
        # 清理本地模拟文件
        try:
            os.remove(local_path)
        except Exception:
            pass
        print('PIPELINE_UP_DONE %s (sim, %.1fs)' % (_name, sim_delay), flush=True)
        return True, [_name]
    
    # 真实模式：从 handle_media 提取的上传逻辑（transfer_video 切分 + 多盘）
    if transfer is None:
        raise RuntimeError('真实上传需要 transfer 模块')
    is_photo = bool(file_info.get('is_photo', False))
    msg_id = file_info.get('msg_id', 0)

    # 探片长（图片跳过）
    duration = 0
    if not is_photo:
        try:
            duration = transfer.probe_duration(local_path) or 0
        except Exception:
            duration = 0

    targets, cerrors = cloud
    if cerrors:
        print('PIPELINE_UP_WARN 有网盘没接上：%s'
              % '；'.join('%s（%s）' % (k, v) for k, v in cerrors), flush=True)
    if not targets:
        raise RuntimeError('没有可用的网盘')

    _nb, _ = os.path.splitext(_name)
    disp_base = _sanitize_name(_nb) or (
        ('photo_%d' % msg_id) if is_photo else ('video_%d' % msg_id))

    # transfer_video 需要的 state 结构（phase/done/total/base/detail/local_paths）
    st = {'phase': '同步云盘中', 'done': 0,
          'total': os.path.getsize(local_path), 'base': 0,
          'detail': '', 'local_paths': {local_path}}
    if state is not None:
        state['uploading_name'] = _name

    loop = asyncio.get_event_loop()
    uploaded, was_split = [], False
    all_parts = []  # Blocker#5: 收集所有盘的 parts，避免某盘失败时泄漏
    done_drives, failed_drives = [], []
    for _idx, _t in enumerate(targets, 1):
        try:
            st['phase'] = '同步云盘中'
            st['detail'] = '%s %d/%d' % (_t.label, _idx, len(targets))
            up, parts, split = await loop.run_in_executor(
                None, transfer.transfer_video, _t.drive, _t.fid,
                local_path, st, disp_base, not is_photo)
            uploaded, was_split = up, split
            all_parts.extend(parts or [])
            done_drives.append(_t.label)
            print('PIPELINE_SYNC_DRIVE_OK %s %s parts=%d'
                  % (_t.label, _name, len(up)), flush=True)
        except Exception as e:
            failed_drives.append('%s（%s）' % (_t.label, str(e)[:80]))
            print('PIPELINE_SYNC_DRIVE_FAIL %s %s: %r' % (_t.label, _name, e), flush=True)
    # 清理本地：所有分段 + 原文件（复用 handle_media 逻辑）
    try:
        transfer.cleanup_local(all_parts + [local_path])
    except Exception as e:
        print('PIPELINE_CLEANUP_FAIL %s: %r' % (_name, e), flush=True)
    if state is not None:
        state['detail'] = ''
    if not done_drives:
        raise RuntimeError('所有网盘都传失败：%s' % '; '.join(failed_drives))
    print('PIPELINE_UP_DONE %s drives=%s' % (_name, '+'.join(done_drives)), flush=True)
    return True, list(uploaded)


async def pipeline_process(collected, cloud, dl_client,
                           cancel_flag=None, status=None,
                           tmp_dir='/tmp/tg_pipeline',
                           fastdl=None, transfer=None,
                           sim_dl_delay=None, sim_up_delay=None,
                           refresh_cb=None):
    """两阶段流水线：下载 N+1 的同时上传 N。
    
    Args:
        collected: 消息列表
        cloud: (targets, cerrors)
        dl_client: Telethon client
        cancel_flag: dict 或 callable，返回 True 表示取消
        status: _transfer_status['getbot'] dict
        tmp_dir: 临时目录
        fastdl, transfer: 真实模块（模拟时为 None）
        sim_dl_delay: 模拟下载耗时（秒/文件）
        sim_up_delay: 模拟上传耗时（秒/文件）
    
    Returns:
        (ok_count, failed_list)
        failed_list = [(msg_id, error_str), ...]
    """
    os.makedirs(tmp_dir, exist_ok=True)
    queue = asyncio.Queue(maxsize=2)
    ok_count = 0
    failed = []
    # v1.1.8: 更新状态供 /status/面板查询
    if status is not None:
        status['phase'] = 'downloading'
        status['total'] = len(collected)
        status['downloaded'] = 0
    
    def _cancelled():
        if cancel_flag is None:
            return False
        if callable(cancel_flag):
            return cancel_flag()
        if isinstance(cancel_flag, dict):
            return bool(cancel_flag.get('cancelled'))
        return False
    
    async def _cancel_aware_put(item):
        """Blocker#1: 取消感知的入队。

        队列满时轮询而不是在 put 上永久阻塞，保证取消信号能被响应。
        返回 True=已入队；False=已取消未入队。
        """
        while True:
            if _cancelled():
                return False
            try:
                queue.put_nowait(item)
                return True
            except asyncio.QueueFull:
                await asyncio.sleep(0.5)

    async def _cancel_aware_get():
        """Blocker#1: 取消感知的出队，避免生产者退出后在 get 上永久阻塞。

        返回 (got, item)：got=False 表示因取消返回（没有发生 get，
        调用方不要调 task_done）。
        """
        while True:
            if _cancelled():
                return False, None
            try:
                return True, queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.2)

    async def producer():
        """生产者：下载"""
        for _idx, m in enumerate(collected):
            if _cancelled():
                print('PIPELINE producer cancelled', flush=True)
                break
            try:
                path, info = await download_phase(
                    m, dl_client, tmp_dir,
                    state=status,
                    fastdl=fastdl,
                    sim_delay=sim_dl_delay,
                    refresh_cb=refresh_cb)
            except Exception as e:
                _mid = getattr(m, 'id', 0)
                failed.append((_mid, str(e)[:100], _idx))
                print('PIPELINE_DL_FAIL %s: %r' % (_mid, e), flush=True)
                import traceback as _tb
                _tb.print_exc()
                continue
            # Blocker#1: 入队可响应取消；取消则删掉刚下载的文件（无人会上传）
            info['_idx'] = _idx
            if not await _cancel_aware_put((m, path, info)):
                print('PIPELINE producer cancelled while enqueueing, drop %s'
                      % info.get('name', path), flush=True)
                try:
                    os.remove(path)
                except Exception:
                    pass
                break
            if status is not None:
                status['downloaded_files'] = status.get('downloaded_files', 0) + 1
        # 结束信号（取消感知；消费者侧 get 同样取消感知，双保险防死锁）
        await _cancel_aware_put(None)
    
    async def consumer():
        """消费者：上传"""
        nonlocal ok_count
        while True:
            _got, item = await _cancel_aware_get()
            if not _got or item is None:
                if _got:
                    queue.task_done()  # 配对取到的结束信号 None
                # 取消或正常结束：排空残留（删文件防泄漏）
                while True:
                    try:
                        _ri = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if _ri is None:
                        queue.task_done()
                        continue
                    _rm, _rp, _rinfo = _ri
                    try:
                        os.remove(_rp)
                    except Exception:
                        pass
                    queue.task_done()
                break
            m, path, info = item
            if _cancelled():
                try:
                    os.remove(path)
                except Exception:
                    pass
                queue.task_done()
                continue
            try:
                success, names = await upload_phase(
                    path, info, cloud,
                    state=status,
                    transfer=transfer,
                    sim_delay=sim_up_delay)
                if success:
                    ok_count += 1
                if status is not None:
                    status['uploaded_files'] = ok_count
                    status['downloaded'] = ok_count
            except Exception as e:
                _mid = getattr(m, 'id', 0)
                failed.append((_mid, str(e)[:100]))
                print('PIPELINE_UP_FAIL %s: %r' % (_mid, e), flush=True)
                # 上传失败也要清本地文件
                try:
                    os.remove(path)
                except Exception:
                    pass
            finally:
                queue.task_done()
    
    _t0 = time.time()
    await asyncio.gather(producer(), consumer())
    # Blocker#4: 删掉多余的 queue.join()（gather 返回时双方已退出；
    # 取消路径下 task_done 配对稍有差错 join 就会永久阻塞）
    _dt = time.time() - _t0
    if status is not None:
        status['phase'] = 'done'
    print('PIPELINE_DONE ok=%d failed=%d time=%.1fs' % (ok_count, len(failed), _dt),
          flush=True)
    return ok_count, failed


async def serial_process(collected, cloud, dl_client,
                         cancel_flag=None, status=None,
                         tmp_dir='/tmp/tg_serial',
                         fastdl=None, transfer=None,
                         sim_dl_delay=None, sim_up_delay=None):
    """串行对照组：用于测试对比。"""
    os.makedirs(tmp_dir, exist_ok=True)
    ok_count = 0
    failed = []
    _t0 = time.time()
    for m in collected:
        _mid = getattr(m, 'id', 0)
        try:
            path, info = await download_phase(
                m, dl_client, tmp_dir, state=status,
                fastdl=fastdl, sim_delay=sim_dl_delay)
            success, names = await upload_phase(
                path, info, cloud, state=status,
                transfer=transfer, sim_delay=sim_up_delay)
            if success:
                ok_count += 1
        except Exception as e:
            failed.append((_mid, str(e)[:100]))
    _dt = time.time() - _t0
    print('SERIAL_DONE ok=%d failed=%d time=%.1fs' % (ok_count, len(failed), _dt),
          flush=True)
    return ok_count, failed
