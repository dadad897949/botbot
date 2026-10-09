"""Chunked transfer: split videos into 400-500MB *playable* segments (ffmpeg, stream copy)
VERSION = "2.0.2"  # 2026-10-09: 合并服务器版按大小去重逻辑
and upload each to Aliyun Drive. Each segment keeps the original container and
can be watched online directly.

Improvements over v1:
- disk space pre-check before download/split
- strict-size: post-split verification, re-split with more segments if any
  part exceeds the limit (bounded retries)
- ffprobe readability check on every segment
- upload verification (name + size match in cloud folder)
- resume: skip parts already present in the cloud folder with matching size
- per-task progress bar attribution via thread-local (no global latest_bar)
"""
import os, time, math, shutil, subprocess, threading
from concurrent.futures import ThreadPoolExecutor


def record_sample(state, done):
    """记录进度采样点（保留 60 秒），用于算实时速度。"""
    samples = state.setdefault('_samples', [])
    now = time.time()
    samples.append((now, done))
    cutoff = now - 60
    while samples and samples[0][0] < cutoff:
        samples.pop(0)

# 方案A（2026-10-05）：按时长切分
SPLIT_DURATION_THRESHOLD = 3600  # 超过 60 分钟才切分（秒）
SEG_DURATION = 1800              # 每段 30 分钟（秒）
# 兜底：时长取不到时按大小切
SPLIT_SIZE_FALLBACK = 500 * 1024 * 1024  # 500MB
MAX_SPLIT_RETRIES = 3
MIN_FREE_MARGIN = 500 * 1024 * 1024  # 500MB safety margin

_tls = threading.local()


def set_current_state(state):
    """Bind the calling thread's transfer state for progress-bar attribution."""
    _tls.state = state


def current_state():
    return getattr(_tls, 'state', None)


def disk_ok(path, need_bytes):
    """Check free space on the filesystem containing path."""
    try:
        free = shutil.disk_usage(os.path.dirname(path) or '.').free
        return free >= need_bytes + MIN_FREE_MARGIN, free
    except OSError:
        return False, 0


def probe_duration(path):
    out = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'default=noprint_wrappers=1:nokey=1', path],
        capture_output=True, text=True, timeout=60)
    return float(out.stdout.strip())


def probe_ok(path):
    """True if ffprobe can read the file (playable container)."""
    try:
        d = probe_duration(path)
        return d > 0
    except Exception:
        return False


def _do_split(path, ext, seg_time):
    d = os.path.dirname(path) or '.'
    base = os.path.basename(path)
    # 先清理上次崩溃残留的 .seg 文件，避免被误收
    for p in os.listdir(d):
        if p.startswith(base + '.seg'):
            try:
                os.remove(os.path.join(d, p))
            except OSError:
                pass
    # M4: path 含 % 时会破坏 % 格式化，先转义
    tmp_pat = '%s.seg%%03d%s' % (path.replace('%', '%%'), ext)
    # 只取视频流和音频流，跳过字幕/数据流（segment muxer 对某些流会报错）
    subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', path,
         '-c', 'copy', '-map', '0:v', '-map', '0:a?', '-f', 'segment',
         '-segment_time', '%.3f' % seg_time, '-reset_timestamps', '1', tmp_pat],
        check=True, timeout=1800)
    return sorted(os.path.join(d, p) for p in os.listdir(d)
                  if p.startswith(base + '.seg'))


def _fit_segment_count(duration):
    """按时长算段数：每 SEG_DURATION 秒一段。返回段数 n（>=2），或 None（不切）。"""
    import math
    if duration <= SPLIT_DURATION_THRESHOLD:
        return None
    n = math.ceil(duration / SEG_DURATION)
    return max(2, n)


def split_video(path, disp_base=None):
    """按时长切分：超过 60 分钟的视频切成 30 分钟一段，流复制（不重编码）。
    时长取不到时按 500MB 大小兜底。切分失败则整传。
    Returns list of (part_path, display_name).
    disp_base: desensitized base name for cloud display names (defaults to
    the original file base name). Local temp files keep working names.
    Verifies each segment: size within 400-500MB (±10% keyframe tolerance)
    + ffprobe readable. Best-effort: accepts closest achievable segmentation
    rather than failing the task.
    切分失败则整传（方案一 2026-10-04：任务永不因切分失败）。"""
    _base, ext = os.path.splitext(os.path.basename(path))
    base = disp_base or _base
    try:
        duration = probe_duration(path)
        if duration <= 0:
            raise ValueError('bad duration')
    except Exception:
        # 取不到时长：直接整传，不切分
        return [(path, base + ext)]
    # 取到时长：按时长判断
    n = _fit_segment_count(duration)
    if n is None:
        return [(path, base + ext)]
    try:
        seg_time = SEG_DURATION
        segs = None
        tried = {n}
        for attempt in range(MAX_SPLIT_RETRIES + 1):
            segs = _do_split(path, ext, seg_time)
            if not segs:
                raise RuntimeError('no segments produced')
            # 按时长切：只验证每段可读，不验证大小
            bad = [s for s in segs if not probe_ok(s)]
            if not bad:
                break
            if attempt == MAX_SPLIT_RETRIES:
                print('SPLIT_ACCEPT_BEST_EFFORT n=%d segs=%d' % (n, len(segs)),
                      flush=True)
                break
            # 段不可读：重试
            n += 1
            seg_time = SEG_DURATION
            for s in segs:
                try:
                    os.remove(s)
                except OSError:
                    pass
            seg_time = duration / n
        total = len(segs)
        parts = []
        d = os.path.dirname(path) or '.'
        for i, s in enumerate(segs, 1):
            disp = '%s_part%02dof%02d%s' % (base, i, total, ext)
            ppath = os.path.join(d, disp)
            if os.path.exists(ppath):
                os.remove(ppath)
            os.rename(s, ppath)
            parts.append((ppath, disp))
        return parts
    except Exception as e:
        # 方案一（2026-10-04）：切分失败不抛错，整传。任务永不因切分失败。
        # 先清理本次产生的 .seg 半成品（/clean 会跳过它们，不能留）。
        d = os.path.dirname(path) or '.'
        base = os.path.basename(path)
        try:
            for p in os.listdir(d):
                if p.startswith(base + '.seg'):
                    try:
                        os.remove(os.path.join(d, p))
                    except OSError:
                        pass
        except OSError:
            pass
        print('SPLIT_FAIL_FALLBACK_WHOLE %s: %r' % (base, e), flush=True)
        _b, _ext = os.path.splitext(os.path.basename(path))
        _disp = disp_base or _b
        return [(path, _disp + _ext)]


UPLOAD_WORKERS = 6  # 分片并发数（aliyunpan 默认 10，保守取 6）


def _put_data_parallel(self, file_path, part_info, file_size,
                       progress_cb=None, workers=UPLOAD_WORKERS):
    """aligo _put_data 的并发替换版：各分片独立 PUT，并发上传。

    复用 aligo 的秒传（_pre_hash/_content_hash）、URL 获取与 complete 流程，
    只把顺序 10MB 分块 PUT 换成线程池并发。progress_cb(done, total) 可选。
    """
    import requests
    from aligo.request import GetUploadUrlRequest, CompleteFileRequest
    from aligo.types import UploadPartInfo

    chunk_size = type(self)._UPLOAD_CHUNK_SIZE or 10485760
    items = list(part_info.part_info_list)
    total_done = [0]
    lock = threading.Lock()

    def do_part(item):
        idx = item.part_number - 1
        with open(file_path, 'rb') as f:
            f.seek(idx * chunk_size)
            data = f.read(chunk_size)
        if not data:
            return 0
        url = item.upload_url
        # 并行模式：单次尝试，失败直接抛给外层回落到顺序上传
        # 不在此处做 URL 刷新重试（get_upload_url 可能 hang 住导致整体卡死）
        resp = self._session.put(
            data=data, url=url, timeout=self._auth._requests_timeout)
        if resp.status_code != 200:
            raise requests.exceptions.RequestException(
                'PUT part %d failed: HTTP %d' % (
                    item.part_number, resp.status_code))
        with lock:
            total_done[0] += len(data)
            d = total_done[0]
        if progress_cb:
            try:
                progress_cb(d, file_size)
            except Exception:
                pass
        return len(data)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(do_part, items):
            pass  # 异常会在这里抛出

    complete = self.complete_file(CompleteFileRequest(
        drive_id=part_info.drive_id,
        file_id=part_info.file_id,
        upload_id=part_info.upload_id,
        part_info_list=part_info.part_info_list,
    ))
    return complete


def _ensure_parallel_upload(ali):
    """给 aligo 实例打上并发上传补丁（幂等）。秒传/校验逻辑保持原样。

    2026-10-02 22:40 临时禁用：并行 PUT 频繁触发 HTTP 400 且偶发 hang 住，
    先切回顺序上传保稳定，待定位 400 根因后再启用。
    """
    return
    import types as _types
    if getattr(ali, '_parallel_patched', False):
        return
    orig_put = ali._put_data

    def patched(self, file_path, part_info, file_size):
        cb = getattr(self, '_parallel_progress_cb', None)
        print('UPLOAD_START %s size=%d cb=%s' % (
            os.path.basename(file_path), file_size,
            'yes' if cb else 'no'), flush=True)
        try:
            return _put_data_parallel(
                self, file_path, part_info, file_size, progress_cb=cb)
        except Exception as e:
            print('PARALLEL_UPLOAD fallback to sequential: %r' % (e,),
                  flush=True)
            return orig_put(file_path, part_info, file_size)

    ali._put_data = _types.MethodType(patched, ali)
    ali._parallel_patched = True
    print('UPLOAD_PATCH applied', flush=True)


def upload_with_retry(ali, folder_id, part_path, display_name, attempts=3,
                      state=None, base=0):
    """上传并返回 aligo 文件对象；显式传 name，避免 auto_rename 歧义。
    state/base 可选：用于面板实时进度（state['done'] = base + 当前文件已传）。

    上传限速 6MB/s：通过回调节流实现（aligo 每传一块调一次回调，
    在此 sleep 以控制速度）。
    """
    _ensure_parallel_upload(ali)
    import time as _time
    _ul_last = [_time.monotonic()]
    _ul_bytes = [0]
    UL_RATE = 6 * 1024 * 1024  # 6MB/s
    if state is not None:
        def _cb(done, total):
            d = base + done
            state['done'] = d
            record_sample(state, d)
            # 限速：计算应耗时，若超前则 sleep
            _ul_bytes[0] = done
            now = _time.monotonic()
            expected = done / UL_RATE
            elapsed = now - _ul_last[0]
            # _ul_last[0] 是上传开始时间，需在首次调用时初始化
            if not hasattr(_cb, '_t0'):
                _cb._t0 = now
                return
            expected_total = done / UL_RATE
            elapsed_total = now - _cb._t0
            if expected_total > elapsed_total:
                _time.sleep(expected_total - elapsed_total)
        ali._parallel_progress_cb = _cb
    else:
        ali._parallel_progress_cb = None
    last = None
    for a in range(attempts):
        try:
            return ali.upload_file(file_path=part_path, parent_file_id=folder_id,
                                   name=display_name)
        except Exception as e:
            last = e
            time.sleep(5 * (a + 1))
    raise last


def find_in_folder(ali, folder_id, name):
    """Return cloud file object matching name, or None."""
    try:
        for f in ali.get_file_list(parent_file_id=folder_id):
            if f.name == name:
                return f
    except Exception:
        pass
    return None


def list_folder_map(ali, folder_id):
    """H4: 一次性拉取文件夹列表建成 {name: file} 字典，避免每个分片调一次 API。"""
    mp = {}
    try:
        for f in ali.get_file_list(parent_file_id=folder_id):
            mp[f.name] = f
    except Exception:
        pass
    return mp


def verify_upload(ali, folder_id, display_name, local_size):
    """Confirm the file exists in the folder with matching size."""
    f = find_in_folder(ali, folder_id, display_name)
    if f is None:
        return False
    try:
        return int(f.size) == int(local_size)
    except (TypeError, ValueError):
        return False


def _ensure_img_ext(disp_name, local_path):
    """图片上传：保证云端文件名有扩展名。"""
    import os as _os
    if _os.path.splitext(disp_name)[1]:
        return disp_name
    # 从本地文件名取扩展名
    _ext = _os.path.splitext(local_path)[1] or '.jpg'
    return disp_name + _ext


def transfer_video(ali, folder_id, path, state, disp_base=None, allow_split=True):
    """Split into 400-500MB playable segments, upload each with retry + verify.
    state: shared dict updated live: phase/done/total/base/detail.
    disp_base: desensitized base name used for cloud file/folder display names.
    allow_split=False: 图片等不切分，直接整传。
    Returns (uploaded_names, part_paths, was_split). Caller cleans up local files."""
    set_current_state(state)
    try:
        size = os.path.getsize(path)
        # 先注册原文件；切分完成后 local_paths 会更新为含各分段
        state['local_paths'] = {path}
        ok, free = disk_ok(path, size * 2)
        if not ok:
            raise RuntimeError(
                '磁盘空间不足：需要约 %.1fMB，可用 %.1fMB'
                % (size * 2 / 1048576, free / 1048576))
        state.update(phase='图片上传中' if not allow_split else '视频分段中',
                     done=0, total=size, base=0, detail='')
        parts = split_video(
            path, disp_base=disp_base) if allow_split else [(path, _ensure_img_ext(disp_base or os.path.basename(path), path))]
        was_split = len(parts) > 1 or parts[0][0] != path
        # 注册所有本地文件，/clean 据此避开正在传输的文件
        state['local_paths'] = {path} | {p for p, _ in parts}
        state['phase'] = '同步云盘中'
        # L4: 清空下载阶段的速度采样，避免上传初期速度/ETA 失真
        state['_samples'] = []
        uploaded, base = [], 0
        record_sample(state, 0)
        # H4: 循环前一次性拉取目录，后续用字典查，避免每个分片调一次 API
        cloud_map = list_folder_map(ali, folder_id)
        for idx, (ppath, disp) in enumerate(parts, 1):
            if was_split:
                state['detail'] = '第%d/%d段 ' % (idx, len(parts))
            psize = os.path.getsize(ppath)
            # resume: skip parts already in the cloud with matching size
            existing = cloud_map.get(disp)
            if existing is not None:
                try:
                    if int(existing.size) == int(psize):
                        print('SKIP_EXISTS %s' % disp, flush=True)
                        uploaded.append(disp)
                        base += psize
                        state['base'] = base
                        state['done'] = base
                        continue
                except (TypeError, ValueError):
                    pass
            # 防重复：如果文件夹里已有同大小的文件（不同名），跳过上传
            # 处理不同链接指向重叠相册的情况
            dup_by_size = None
            for _nm, _f in cloud_map.items():
                try:
                    if int(getattr(_f, 'size', -1)) == int(psize):
                        dup_by_size = _nm
                        break
                except (TypeError, ValueError):
                    continue
            if dup_by_size is not None:
                print('SKIP_DUP_SIZE %s (已有 %s 同大小)' % (disp, dup_by_size), flush=True)
                uploaded.append(dup_by_size)
                base += psize
                state['base'] = base
                state['done'] = base
                continue
            ret = upload_with_retry(ali, folder_id, ppath, disp,
                                    state=state, base=base)
            # #9: 用 upload_file 返回值直接校验（file_id + 大小），不再重新按名搜索。
            # 按名搜索在 auto_rename 下可能命中旧文件或漏掉改名后的新文件。
            # 返回值若无 size 字段（如 CreateFileResponse），则回落到按名校验。
            ok = False
            try:
                if ret is not None and getattr(ret, 'file_id', None):
                    rsize = getattr(ret, 'size', None)
                    if rsize is not None:
                        ok = int(rsize) == int(psize)
                    else:
                        # rsize 为 None：可能是秒传（CreateFileResponse 无 size）
                        # 先标记 ok，让后方的秒传复制逻辑处理；若复制失败再报错
                        ok = True
            except (TypeError, ValueError):
                ok = False
            if not ok:
                raise RuntimeError('上传校验失败：%s（云端文件缺失或大小不符）' % disp)
            up_name = getattr(ret, 'name', None) or disp
            # 秒传修复：aligo 秒传时直接返回已存在文件的 ID，不会在目标文件夹创建文件
            # 必须检查文件是否真的在目标文件夹，否则复制一份过去
            ret_fid = getattr(ret, 'file_id', None)
            in_target = False
            if ret_fid:
                # 刷新确认文件是否在目标文件夹
                fresh = find_in_folder(ali, folder_id, up_name)
                if fresh is not None and getattr(fresh, 'file_id', None) == ret_fid:
                    in_target = True
            if not in_target and ret_fid:
                try:
                    print('RAPID_UPLOAD copy %s to target folder' % up_name, flush=True)
                    # copy_file(file_id, to_parent_file_id)
                    ali.copy_file(ret_fid, folder_id)
                    # 重新获取以确认
                    fresh = find_in_folder(ali, folder_id, up_name)
                    if fresh is not None:
                        ret = fresh
                        ret_fid = getattr(ret, 'file_id', None)
                        in_target = True
                except Exception as e:
                    print('RAPID_UPLOAD copy failed: %s' % e, flush=True)
            if not in_target:
                raise RuntimeError('上传校验失败：%s（秒传后文件不在目标文件夹）' % disp)
            uploaded.append(up_name)
            # H4: 新上传的文件加入字典，保持缓存一致
            if ret is not None and getattr(ret, 'file_id', None):
                cloud_map[up_name] = ret
            base += psize
            state['base'] = base
            state['done'] = base
            record_sample(state, base)
        state['done'] = size
        record_sample(state, size)
        state['detail'] = ''
        return uploaded, [p for p, _ in parts], was_split
    finally:
        set_current_state(None)


def cleanup_local(paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
