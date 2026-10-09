"""Telegram 多连接并行下载器（fastdl v2.0.0）

VERSION = "2.0.0"   2026-10-08

对比 v1.2.0 的改动（v1 的两个大坑：同 DC 直接回落单连接、CDN 直接回落单连接）：

1. 同 DC 也能多连接
   v1 里 `dc_id == client.session.dc_id` 直接 raise，导致亚洲号（home DC5）
   下载 DC5 文件时整个并行逻辑一行不跑，静默回落成单连接 ——
   之前把 part size 从 2MB 调到 1MB、连接数从 12 降到 8，全是在给一个
   没启动的引擎调参。
   v2 改成：复用主连接的 auth key（`client._sender.auth_key`）另开 N 条 TCP，
   连接同一个 DC 的地址（官方客户端就是这么干多连接的）。
   同 DC 不能用 ExportAuthorization（服务端会拒），但一个 auth key 挂多条
   TCP 是允许的。连接按 DC 缓存在池子里复用，不是每个文件重连。
   这些连接和主连接共用 auth key，服务端可能把 update 发过来 ——
   已把 updates_queue 指回主客户端的队列，不会丢消息。

2. CDN 文件也能并行
   v1 遇到 `FileCdnRedirect` 直接 raise，大文件走 CDN 时并行同样失效。
   v2 按官方 `upload.FileHash` 分片并行取 `GetCdnFileRequest`，
   用 AES-CTR 按分片绝对偏移定位计数器解密（与 Telethon CDN 解密器同一套方案），
   每片解密后做 sha256 校验（官方 hash）——校验不过直接抛错回落，
   绝不把脏数据写进文件。hash 列表覆盖不全时用 GetCdnFileHashesRequest 补齐；
   遇到 CdnFileReuploadNeeded 回主连接重新授权再取。

3. 分块失败不再整单回落
   单块失败（连接坏了 / 超时 / 空包）自动换一条新连接重试，最多 CHUNK_RETRIES 次；
   每块带 CHUNK_TIMEOUT 超时保护，老连接假死不会把任务挂死。
   只有真没救（重试用尽 / CDN 校验不过 / FloodWait）才抛错，交给调用方回落单连接。

4. 日志不再骗人
   实际生效的模式通过 `mode_out` 回传（调用方记进 PIPELINE_DL_DONE），
   另有 FASTDL_MODE / FASTDL_POOL / FASTDL_CHUNK_RETRY 便于对日志排查。

用法不变：成功返回 path，失败抛异常（调用方的单连接回落逻辑保持原样）。
"""
import asyncio
import inspect
import random
import os
import struct
import time
from hashlib import sha256

from telethon import utils
from telethon.tl import functions, types

try:
    from telethon import errors as tg_errors
except Exception:  # pragma: no cover - 老版本兜底
    tg_errors = None

PART_SIZE = 1024 * 1024   # 2026-10-05 回滚：2MB→1MB（12连接反而更慢）
CONNECTIONS = 7           # 2026-10-08: 用户要求7
INFLIGHT = 2              # 兼容旧调用；v2 每连接串行取块（瓶颈在带宽/RTT，
                          # 不在单连接排队，串行+失败换连接重试更稳，也少踩并发限流）

# 下载限速：8MB/s（2026-10-02 加，削峰保稳定）
DL_RATE_LIMIT = 0         # 2026-10-05 Jack 要求：关掉限速

CHUNK_TIMEOUT = 90        # 单块最长等待（秒）；超时视为连接坏掉，换连接重试
CHUNK_RETRIES = 3         # 单块失败重试次数（每次换一条新连接）
PROBE_LIMIT = 4096        # 探测块大小（4096 的倍数）
MAX_POOL = 8              # 每个 DC 每种连接池的闲置上限
HASH_FETCH_ROUNDS = 512   # CDN hash 列表补齐的最大轮数（防死循环）
RECONNECT_DELAY = 0.4     # 换连接前的退避基数（秒）

# 回滚开关（一行就能退回 v1 行为）：
#   ENABLE_SAME_DC=False → 同 DC 不再开多连接，直接抛错回落单连接（v1 行为）
#   SAME_DC_INIT=False   → 同 DC 新连接不补发 InitConnection（日志里 FASTDL_INIT
#                          一直报错时试这个）
ENABLE_SAME_DC = True
SAME_DC_INIT = True


class _RateLimiter:
    """令牌桶限速器。"""

    def __init__(self, rate):
        self.rate = rate
        self._tokens = rate
        self._last = 0
        self._lock = None

    async def acquire(self, n):
        if self.rate <= 0:
            return  # 限速关闭，直接放行
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.monotonic()
            if self._last == 0:
                self._last = now
            self._tokens = min(self.rate, self._tokens + (now - self._last) * self.rate)
            self._last = now
            while self._tokens < n:
                await asyncio.sleep((n - self._tokens) / self.rate)
                now = time.monotonic()
                self._tokens = min(self.rate, self._tokens + (now - self._last) * self.rate)
                self._last = now
            self._tokens -= n


_dl_limiter = _RateLimiter(DL_RATE_LIMIT)

# per-DC semaphore：限制同一 DC 上的并发连接总量，避免 -429
_dc_semaphores = {}


def _dc_sem(dc_id):
    sem = _dc_semaphores.get(dc_id)
    if sem is None:
        sem = asyncio.Semaphore(16)
        _dc_semaphores[dc_id] = sem
    return sem


class _CdnRedirect(Exception):
    """直连过程中才发现文件走 CDN 时，用它把重定向抛上来。"""

    def __init__(self, redirect):
        super().__init__('file is served by CDN dc %s' % getattr(redirect, 'dc_id', '?'))
        self.redirect = redirect


# --------------------------------------------------------------------------
# 连接池： kind -> dc_id -> [sender]
#   same     : 与主连接同一个 DC，复用主 auth key 另开的 TCP
#   exported : 其它 DC，走 ExportAuthorization（v1 的老路）
#   cdn      : CDN DC，复用主 auth key（与 Telethon `_get_cdn_client` 同做法）
# --------------------------------------------------------------------------
_pools = {'same': {}, 'exported': {}, 'cdn': {}}
_pool_locks = {}


def _pool_lock():
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    lock = _pool_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _pool_locks[loop] = lock
    return lock


def _pool_get(kind, dc_id):
    return _pools[kind].setdefault(dc_id, [])


def _is_sender_alive(s):
    try:
        return bool(s) and s.is_connected()
    except Exception:
        return False


async def _disconnect_quiet(sender):
    if not sender:
        return
    try:
        await sender.disconnect()
    except Exception:
        pass


def _err_is(e, name):
    cls = getattr(tg_errors, name, None) if tg_errors is not None else None
    return bool(cls) and isinstance(e, cls)


def _is_flood(e):
    return _err_is(e, 'FloodWaitError')


def _is_ref_expired(e):
    return _err_is(e, 'FileReferenceExpiredError') or _err_is(e, 'FilerefUpgradeNeededError')


def _new_sender_obj(client):
    """造一个裸 MTProtoSender，共用主连接的 auth key。"""
    from telethon.network import MTProtoSender

    loggers = getattr(client, '_log', None)
    try:
        params = inspect.signature(MTProtoSender.__init__).parameters
    except (TypeError, ValueError):
        params = {}

    kwargs = {}
    if 'loggers' in params:
        kwargs['loggers'] = loggers
    if 'retries' in params:
        kwargs['retries'] = 3
    if 'connect_timeout' in params:
        kwargs['connect_timeout'] = 30
    if 'updates_queue' in params:
        q = getattr(client, '_updates_queue', None)
        if q is not None:
            kwargs['updates_queue'] = q
    elif 'update_callback' in params:
        q = getattr(client, '_updates_queue', None)
        if q is not None:
            kwargs['update_callback'] = (lambda obj, _q=q: _q.put_nowait(obj))

    auth_key = getattr(getattr(client, '_sender', None), 'auth_key', None)
    if auth_key is None:
        auth_key = getattr(getattr(client, 'session', None), 'auth_key', None)
    if auth_key is None:
        raise RuntimeError('client has no auth key')

    try:
        return MTProtoSender(auth_key, **kwargs)
    except TypeError:
        for args in ((auth_key, loggers, 3), (auth_key, loggers)):
            try:
                return MTProtoSender(*args)
            except TypeError:
                continue
        raise


def _new_connection(client, dc_id, ip, port):
    cls = getattr(client, '_connection', None)
    if cls is None:
        raise RuntimeError('client._connection missing')
    loggers = getattr(client, '_log', None)
    proxy = getattr(client, '_proxy', None)
    local_addr = getattr(client, '_local_addr', None)
    try:
        return cls(ip, port, dc_id, loggers=loggers, proxy=proxy, local_addr=local_addr)
    except TypeError:
        try:
            return cls(ip, port, dc_id, loggers, proxy, local_addr)
        except TypeError:
            return cls(ip, port, dc_id, loggers)


async def _dc_addr(client, dc_id, cdn=False):
    """拿某个 DC 的 ip/port：先问服务端 config，再退回 session 里记的地址。"""
    variants = ({'cdn': cdn}, {}) if cdn else ({},)
    for kwargs in variants:
        try:
            dc = await client._get_dc(dc_id, **kwargs)
            if getattr(dc, 'ip_address', None):
                return dc.ip_address, dc.port
        except Exception:
            continue
    sess = getattr(client, 'session', None)
    if (sess is not None and getattr(sess, 'dc_id', None) == dc_id
            and getattr(sess, 'server_address', None)):
        return sess.server_address, sess.port
    raise RuntimeError('cannot resolve address of DC %s (cdn=%s)' % (dc_id, cdn))


_INIT_LOCK = None


def _init_lock():
    global _INIT_LOCK
    if _INIT_LOCK is None:
        _INIT_LOCK = asyncio.Lock()
    return _INIT_LOCK


def _layer():
    import importlib
    for mod_name in ('telethon.tl.alltlobjects', 'telethon.tl'):
        try:
            layer = getattr(importlib.import_module(mod_name), 'LAYER', None)
        except Exception:
            layer = None
        if layer:
            return layer
    return None


async def _init_bare_sender(client, sender):
    """给新开的同 DC 连接补一次 InvokeWithLayer(InitConnection)。

    Telethon 的 CDN client 不发这个也能取 CDN 文件；同 DC 的普通连接按主连接的
    做法补一次更保险。发失败只记日志，不中断下载。
    """
    layer = _layer()
    init = getattr(client, '_init_request', None)
    if not SAME_DC_INIT or not layer or init is None:
        return
    async with _init_lock():
        saved = getattr(init, 'query', None)
        try:
            init.query = functions.help.GetConfigRequest()
            await sender.send(functions.InvokeWithLayerRequest(layer, init))
        except Exception as e:
            print('FASTDL_INIT warn %r' % (e,), flush=True)
        finally:
            try:
                init.query = saved
            except Exception:
                pass


async def _connect_one(client, dc_id, kind):
    if kind == 'exported':
        return await client._create_exported_sender(dc_id)
    cdn = (kind == 'cdn')
    ip, port = await _dc_addr(client, dc_id, cdn=cdn)
    sender = _new_sender_obj(client)
    conn = _new_connection(client, dc_id, ip, port)
    try:
        await sender.connect(conn)
    except Exception:
        await _disconnect_quiet(sender)
        raise
    if not cdn:
        await _init_bare_sender(client, sender)
    return sender


async def _acquire(client, dc_id, n, kind):
    """从池里拿 n 条连接；新建失败时有几条用几条（至少 1 条）。"""
    async with _pool_lock():
        pool = _pool_get(kind, dc_id)
        out = []
        while pool and len(out) < n:
            s = pool.pop()
            if _is_sender_alive(s):
                out.append(s)
            else:
                await _disconnect_quiet(s)
        reused = len(out)
        created = 0
        while len(out) < n:
            try:
                out.append(await _connect_one(client, dc_id, kind))
                created += 1
            except Exception as e:
                print('FASTDL_POOL %s dc=%s create_fail %r (have %d/%d)'
                      % (kind, dc_id, e, len(out), n), flush=True)
                break
        print('FASTDL_POOL %s dc=%s acquired=%d (reuse=%d new=%d idle=%d)'
              % (kind, dc_id, len(out), reused, created, len(pool)), flush=True)
    if not out:
        raise RuntimeError('no usable connection to dc %s (%s)' % (dc_id, kind))
    return out


async def _release(dc_id, senders, kind):
    """用完放回池里；死的丢掉；池子太大就 trim。"""
    async with _pool_lock():
        pool = _pool_get(kind, dc_id)
        for s in senders:
            if _is_sender_alive(s):
                pool.append(s)
            else:
                await _disconnect_quiet(s)
        while len(pool) > MAX_POOL:
            await _disconnect_quiet(pool.pop())


async def _drop_pool(dc_id, kind):
    """整池清空（连接批量坏掉时调用）。"""
    async with _pool_lock():
        pool = _pools[kind].pop(dc_id, [])
        for s in pool:
            await _disconnect_quiet(s)
        if pool:
            print('FASTDL_POOL %s dc=%s cleared=%d' % (kind, dc_id, len(pool)), flush=True)


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _split_bytes(size, n):
    """把 [0,size) 切成 n 段连续字节区间（对齐 PART_SIZE，尾段按 size 截）。"""
    total_parts = (size + PART_SIZE - 1) // PART_SIZE
    per, rem = divmod(total_parts, n)
    ranges = []
    p = 0
    for i in range(n):
        cnt = per + (1 if i < rem else 0)
        s = min(p * PART_SIZE, size)
        e = min(size, (p + cnt) * PART_SIZE)
        if e < s:
            e = s
        ranges.append((s, e))
        p += cnt
    return ranges


def _split_index(total, n):
    """把 [0,total) 个元素切成 n 段连续下标区间。"""
    if n <= 1:
        return [(0, total)]
    per, rem = divmod(total, n)
    out = []
    p = 0
    for i in range(n):
        cnt = per + (1 if i < rem else 0)
        out.append((p, p + cnt))
        p += cnt
    return out


def _open_target(path, size):
    """确保文件存在且长度 >= size（预分配），返回只写 fd。"""
    if not os.path.exists(path):
        with open(path, 'wb') as f:
            f.truncate(size)
    else:
        try:
            cur = os.path.getsize(path)
        except OSError:
            cur = 0
        if cur < size:
            with open(path, 'r+b') as f:
                f.truncate(size)
    return os.open(path, os.O_WRONLY)


def _write_at(fd, data, off):
    os.pwrite(fd, data, off)


async def _notify(cb, done, total):
    if cb is None:
        return
    try:
        r = cb(done, total)
        if asyncio.iscoroutine(r):
            await r
    except Exception:
        pass


def _record_mode(mode_out, text):
    if mode_out is not None:
        try:
            mode_out.append(text)
        except Exception:
            pass


def _is_self_bot(client):
    try:
        return bool(getattr(client, '_mb_entity_cache', None).self_bot)
    except Exception:
        return False


# --------------------------------------------------------------------------
# CDN 分支
# --------------------------------------------------------------------------
async def _cdn_hash_chunks(client, redirect, size):
    """返回覆盖整个文件的 [(offset, limit, sha256), ...]（按 offset 升序）。"""
    seen = {}

    def add(items):
        for h in items or []:
            try:
                off = int(h.offset)
                lim = int(h.limit)
            except Exception:
                continue
            if lim <= 0 or off < 0 or off >= size:
                continue
            seen[off] = (min(lim, size - off), bytes(h.hash))

    add(getattr(redirect, 'file_hashes', None))

    for _ in range(HASH_FETCH_ROUNDS):
        off = 0
        while off in seen:
            off += seen[off][0]
        if off >= size:
            break
        try:
            more = await client(functions.upload.GetCdnFileHashesRequest(
                file_token=redirect.file_token, offset=off))
        except Exception as e:
            print('FASTDL_CDN hash_fetch_fail off=%d %r' % (off, e), flush=True)
            break
        if not more:
            break
        before = len(seen)
        add(more)
        if len(seen) == before:
            break

    chunks = sorted((off, lim, h) for off, (lim, h) in seen.items())

    cov = 0
    for off, lim, _h in chunks:
        if off > cov:
            break
        cov = max(cov, off + lim)
    if cov < size:
        raise RuntimeError('cdn hash list incomplete: %d/%d' % (cov, size))

    # 丢掉被前一片完全覆盖的重复片
    clean = []
    for off, lim, h in chunks:
        if clean and off < clean[-1][0] + clean[-1][1]:
            continue
        clean.append((off, lim, h))
    return clean


def _ctr_for(key, iv12, offset):
    """CDN 分片的 AES-CTR：计数器 = 分片绝对偏移 // 16（大端 4 字节）。"""
    from telethon.crypto import AESModeCTR
    counter = struct.pack('>I', (int(offset) // 16) & 0xFFFFFFFF)
    return AESModeCTR(key=key, iv=iv12 + counter)


async def _download_cdn(client, redirect, path, size, progress_callback,
                        resume_offset, n, mode_out):
    dc_id = getattr(redirect, 'dc_id', None)
    if dc_id is None:
        raise RuntimeError('cdn redirect without dc_id')
    if _is_self_bot(client):
        raise RuntimeError('FileCdnRedirect：bot 客户端用不了 GetCdnFile，'
                           '这个文件得用用户号下载')

    token = redirect.file_token
    key = bytes(redirect.encryption_key)
    iv = bytes(redirect.encryption_iv)
    if len(iv) < 16:
        raise RuntimeError('cdn iv too short: %d' % len(iv))
    iv12 = iv[:12]

    chunks = await _cdn_hash_chunks(client, redirect, size)
    todo = [c for c in chunks if c[0] + c[1] > resume_offset]
    if not todo:
        todo = chunks[-1:]
    note = 'cdn x%d (dc%s)' % (n, dc_id)

    async with _dc_sem(dc_id):
        senders = await _acquire(client, dc_id, n, 'cdn')
        nsend = max(1, min(len(senders), len(todo)))
        ranges = _split_index(len(todo), nsend)
        fd = None
        done = resume_offset
        lock = asyncio.Lock()

        async def fetch(slot, off, lim, expected):
            last = None
            for attempt in range(CHUNK_RETRIES):
                try:
                    req = functions.upload.GetCdnFileRequest(
                        file_token=token, offset=off, limit=lim)
                    res = await asyncio.wait_for(
                        client._call(senders[slot], req), timeout=CHUNK_TIMEOUT)
                except Exception as e:
                    if _is_flood(e):
                        raise
                    last = e
                    print('FASTDL_CDN_RETRY slot=%d off=%d attempt=%d err=%r'
                          % (slot, off, attempt + 1, e), flush=True)
                else:
                    if isinstance(res, types.upload.CdnFileReuploadNeeded):
                        try:
                            await client(functions.upload.ReuploadCdnFileRequest(
                                file_token=token, request_token=res.request_token))
                            continue
                        except Exception as e:
                            last = e
                    else:
                        data = getattr(res, 'bytes', None)
                        if not data:
                            last = RuntimeError('empty cdn chunk at %d' % off)
                        elif len(data) < lim:
                            last = RuntimeError('short cdn chunk at %d: %d/%d'
                                                % (off, len(data), lim))
                        else:
                            plain = _ctr_for(key, iv12, off).decrypt(data)
                            if expected and sha256(plain).digest() != expected:
                                raise RuntimeError(
                                    'cdn chunk tampered at %d (sha256 mismatch)' % off)
                            return plain
                wait = RECONNECT_DELAY * (2 ** attempt) + random.uniform(0, 0.2)
                wait = min(wait, 5.0)
                await asyncio.sleep(wait)
                await _disconnect_quiet(senders[slot])
                try:
                    senders[slot] = await _connect_one(client, dc_id, 'cdn')
                except Exception as e2:
                    print('FASTDL_CDN_RECONNECT fail %r' % (e2,), flush=True)
            raise last or RuntimeError('cdn chunk failed at %d' % off)

        async def worker(slot):
            nonlocal done
            s, e = ranges[slot]
            for idx in range(s, e):
                off, lim, h = todo[idx]
                if off + lim <= resume_offset:
                    continue
                data = await fetch(slot, off, lim, h)
                await _dl_limiter.acquire(len(data))
                _write_at(fd, data, off)
                async with lock:
                    done += len(data)
                    d = min(done, size)
                await _notify(progress_callback, d, size)

        try:
            fd = _open_target(path, size)
            await asyncio.gather(*[worker(i) for i in range(nsend)])
        except Exception as e:
            if not _is_flood(e):
                await _drop_pool(dc_id, 'cdn')
            raise
        finally:
            if fd is not None:
                os.close(fd)
            await _release(dc_id, senders, 'cdn')

    actual = os.path.getsize(path)
    if actual != size:
        raise RuntimeError('size mismatch: %d != %d' % (actual, size))
    _record_mode(mode_out, note)
    print('FASTDL_CDN_DONE dc=%s size=%d chunks=%d' % (dc_id, size, len(chunks)),
          flush=True)
    return path


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
async def download_parallel(client, media, path, file_size=None,
                            progress_callback=None,
                            connections=CONNECTIONS, inflight=INFLIGHT,
                            resume_offset=0, mode_out=None, refresh_cb=None):
    """并行下载 `media` 到 `path`。

    progress_callback(done, total) 与 download_media 的回调一致。
    成功返回 path；任何失败抛异常（调用方回落单连接）。
    """
    info = utils._get_file_info(media)
    dc_id = info.dc_id
    size = file_size or info.size
    if not size:
        raise ValueError('parallel download needs file size')
    if dc_id is None:
        dc_id = getattr(getattr(client, 'session', None), 'dc_id', None)
    if dc_id is None:
        raise ValueError('parallel download needs dc_id')

    size = int(size)
    resume_offset = max(0, min(int(resume_offset or 0), size))
    total_parts = (size + PART_SIZE - 1) // PART_SIZE
    n = max(1, min(int(connections), total_parts))
    loc = {'location': info.location}
    # 2026-10-08：用临时文件下载，完成后校验大小，原子 move（防半截文件）
    _tmp_path = path + '.tmp'
    _final_path = path
    path = _tmp_path
    _dl_ok = False  # 2026-10-08 fix W1：成功才为 True，finally 里据此清理 .tmp
    # .tmp 由 _open_target 用 truncate 预分配成完整大小（稀疏文件），getsize 恒等于 size，
    # 并不代表已下载量。进程被强杀后残留的 .tmp 若被当作"已下完"，所有块都会被跳过，
    # 产出大小正确、内容却是空洞的文件。没有可靠的进度记录，所以一律清掉、从头下。
    resume_offset = 0
    if os.path.exists(_tmp_path):
        try:
            os.remove(_tmp_path)
            print('FASTDL_STALE_TMP_REMOVED %s' % _tmp_path, flush=True)
        except OSError:
            try:
                open(_tmp_path, 'wb').close()  # 删不掉就清空，保证从 0 字节开始
            except OSError:
                pass
    sess_dc = getattr(getattr(client, 'session', None), 'dc_id', None)
    t0 = time.time()

    # ---- 1) 探测块：同时定 直连 / CDN / 需要迁移到别的 DC ----
    probe_off = (resume_offset // PROBE_LIMIT) * PROBE_LIMIT
    if probe_off >= size:
        probe_off = 0
    redirect = None
    # 2026-10-08：probe 前先尝试刷新（media 可能携带过期引用）
    # media 是 download_parallel 的参数，本作用域可直接访问
    _probe_refreshed = False
    try:
        res = await client._call(client._sender, functions.upload.GetFileRequest(
            loc['location'], offset=probe_off, limit=PROBE_LIMIT))
    except Exception as e:
        if _err_is(e, 'FileMigrateError'):
            dc_id = e.new_dc
            print('FASTDL_MIGRATE file lives in dc %s' % dc_id, flush=True)
        elif _is_ref_expired(e) and refresh_cb is not None and not _probe_refreshed:
            # 2026-10-08：probe 阶段引用过期，尝试刷新后重试一次
            print('FASTDL_PROBE_REF_EXPIRED, trying refresh', flush=True)
            try:
                # refresh_cb 需要 media，但这里没有直接引用
                # 用闭包外的 media 变量（download_parallel 的参数）
                _new_media = await refresh_cb(media)
                if _new_media is not None:
                    loc['location'] = utils._get_file_info(_new_media).location
                    _probe_refreshed = True
                    print('FASTDL_PROBE_REFRESH ok, retry probe', flush=True)
                    res = await client._call(client._sender, functions.upload.GetFileRequest(
                        loc['location'], offset=probe_off, limit=PROBE_LIMIT))
                    # 2026-10-08 fix W2：重试成功也要检查 res 类型
                    # （之前跳过 else 分支，FileCdnRedirect 会被误判走直连）
                    if isinstance(res, types.upload.FileCdnRedirect):
                        redirect = res
                    elif isinstance(res, types.upload.CdnFileReuploadNeeded):
                        raise RuntimeError('unexpected ReuploadNeeded on probe retry')
                    elif not getattr(res, 'bytes', None):
                        raise RuntimeError('empty probe chunk at %d' % probe_off)
                else:
                    raise
            except Exception as e2:
                # 2026-10-08 fix：FileMigrate/FloodWait 不要包成引用过期
                # 否则 DC 迁移走不成、调用方的限流等待也被跳过
                if _err_is(e2, 'FileMigrateError') or _is_flood(e2):
                    raise
                # refresh 失败，抛中文友好错误
                raise RuntimeError(
                    '文件引用已过期（Telegram 已清理该文件的访问凭证），'
                    '请重新发送该文件再试')
        else:
            raise
    else:
        if isinstance(res, types.upload.FileCdnRedirect):
            redirect = res
        elif isinstance(res, types.upload.CdnFileReuploadNeeded):
            raise RuntimeError('unexpected ReuploadNeeded on probe')
        elif not getattr(res, 'bytes', None):
            raise RuntimeError('empty probe chunk at %d' % probe_off)

    # ---- 2) CDN 分支 ----
    if redirect is not None:
        try:
            await _download_cdn(client, redirect, path, size, progress_callback,
                                resume_offset, n, mode_out)
        except Exception:
            # 2026-10-08 fix W1：CDN 下载失败清 .tmp 残留
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            raise
        # CDN 下载完也要原子 move（path 此时是 .tmp）
        import os as _os
        _actual = _os.path.getsize(path)
        if _actual != size:
            try:
                _os.remove(path)
            except OSError:
                pass
            raise RuntimeError('CDN size mismatch: %d != %d' % (_actual, size))
        _os.rename(path, _final_path)
        print('FASTDL_ATOMIC_MOVE(CDN) %s -> %s' % (path, _final_path), flush=True)
        _dl_ok = True
        return _final_path

    # ---- 3) 直连分支 ----
    kind = 'same' if (sess_dc is not None and dc_id == sess_dc) else 'exported'
    if kind == 'same' and not ENABLE_SAME_DC:
        raise RuntimeError('same-dc parallel disabled (ENABLE_SAME_DC=False)')
    try:
        senders = await _acquire(client, dc_id, n, kind)
    except Exception as e:
        if _err_is(e, 'DcIdInvalidError') and kind == 'exported':
            print('FASTDL_FALLBACK same-dc (DcIdInvalid on export)', flush=True)
            kind = 'same'
            senders = await _acquire(client, dc_id, n, kind)
        else:
            raise

    note = 'parallel x%d (%s%s)' % (len(senders), kind,
                                    ', same dc' if kind == 'same' else '')
    print('FASTDL_MODE %s dc=%s size=%s resume=%s' % (note, dc_id, size, resume_offset),
          flush=True)

    fd = None
    done = resume_offset
    lock = asyncio.Lock()

    try:
        async with _dc_sem(dc_id):
            fd = _open_target(path, size)

            async def _do_refresh():
                if refresh_cb is None:
                    return False
                try:
                    new_msg = await refresh_cb(media)
                    if new_msg is not None:
                        loc['location'] = utils._get_file_info(new_msg).location
                        print('FASTDL_REF_REFRESH ok', flush=True)
                        return True
                except Exception as e:
                    print('FASTDL_REF_REFRESH fail %r' % (e,), flush=True)
                return False

            async def fetch(slot, off, allow_refresh=True):
                last = None
                for attempt in range(CHUNK_RETRIES):
                    try:
                        req = functions.upload.GetFileRequest(
                            loc['location'], offset=off, limit=PART_SIZE)
                        res = await asyncio.wait_for(
                            client._call(senders[slot], req), timeout=CHUNK_TIMEOUT)
                    except Exception as e:
                        if _is_flood(e):
                            raise
                        if _is_ref_expired(e) and allow_refresh:
                            if await _do_refresh():
                                return await fetch(slot, off, allow_refresh=False)
                            # 2026-10-08：refresh 也失败，说明文件引用彻底过期
                            raise RuntimeError(
                                '文件引用已过期（Telegram 已清理该文件的访问凭证），'
                                '请重新发送该文件再试')
                        last = e
                        print('FASTDL_CHUNK_RETRY slot=%d off=%d attempt=%d err=%r'
                              % (slot, off, attempt + 1, e), flush=True)
                    else:
                        if isinstance(res, types.upload.FileCdnRedirect):
                            raise _CdnRedirect(res)
                        if isinstance(res, types.upload.CdnFileReuploadNeeded):
                            last = RuntimeError('reupload needed on direct request')
                        else:
                            data = getattr(res, 'bytes', None)
                            if data:
                                return data
                            last = RuntimeError('empty chunk at %d' % off)
                            print('FASTDL_CHUNK_RETRY slot=%d off=%d empty'
                                  % (slot, off), flush=True)
                    wait = RECONNECT_DELAY * (2 ** attempt) + random.uniform(0, 0.2)
                    wait = min(wait, 5.0)
                    await asyncio.sleep(wait)
                    await _disconnect_quiet(senders[slot])
                    try:
                        senders[slot] = await _connect_one(client, dc_id, kind)
                    except Exception as e2:
                        print('FASTDL_RECONNECT fail %r' % (e2,), flush=True)
                        if 'AuthBytesInvalid' in type(e2).__name__ or 'AuthBytesInvalid' in str(e2):
                            try:
                                if hasattr(client, '_exported_senders'):
                                    client._exported_senders.pop(dc_id, None)
                                if hasattr(client, '_sender') and hasattr(client._sender, '_exported_auth'):
                                    client._sender._exported_auth.pop(dc_id, None)
                                print('FASTDL_RECONNECT retry with fresh auth', flush=True)
                                senders[slot] = await _connect_one(client, dc_id, kind)
                                print('FASTDL_RECONNECT ok after auth refresh', flush=True)
                            except Exception as e3:
                                print('FASTDL_RECONNECT retry fail %r' % (e3,), flush=True)
                raise last or RuntimeError('chunk failed at %d' % off)

            ranges = _split_bytes(size, len(senders))

            async def worker(slot):
                nonlocal done
                s, e = ranges[slot]
                off = s
                while off < e:
                    if off + PART_SIZE <= resume_offset:
                        off += PART_SIZE   # 断点前的整块：跳过（已计入 done）
                        continue
                    data = await fetch(slot, off)
                    await _dl_limiter.acquire(len(data))
                    _write_at(fd, data, off)
                    async with lock:
                        done += len(data)
                        d = min(done, size)
                    await _notify(progress_callback, d, size)
                    off += PART_SIZE

            try:
                await asyncio.gather(*[worker(i) for i in range(len(senders))])
                # 2026-10-08 fix B1：直连成功也要标记，否则 finally 误删 .tmp
                _dl_ok = True
            except _CdnRedirect as cr:
                # 中途才发现是 CDN：关掉直连 fd，转 CDN 分支
                if fd is not None:
                    os.close(fd)
                    fd = None
                await _release(dc_id, senders, kind)
                senders = []
                print('FASTDL_CDN_SWITCH mid-download redirect', flush=True)
                await _download_cdn(client, cr.redirect, path, size,
                                    progress_callback, resume_offset, n,
                                    mode_out)
                # 原子 move（path 是 .tmp）
                _ac = os.path.getsize(path)
                if _ac != size:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    raise RuntimeError('CDN-switch size mismatch: %d != %d' % (_ac, size))
                os.rename(path, _final_path)
                print('FASTDL_ATOMIC_MOVE(CDN-switch) %s -> %s' % (path, _final_path), flush=True)
                _dl_ok = True
                return _final_path
            except Exception as e:
                if not _is_flood(e):
                    await _drop_pool(dc_id, kind)
                raise
    finally:
        if fd is not None:
            os.close(fd)
        if senders:
            await _release(dc_id, senders, kind)
        # 2026-10-08 fix W1：失败时删 .tmp 残留（成功时已 rename，无残留）
        if not _dl_ok:
            try:
                if os.path.exists(path):
                    os.remove(path)
                    print('FASTDL_TMP_CLEAN %s' % path, flush=True)
            except OSError:
                pass

    actual = os.path.getsize(path)
    if actual != size:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError('size mismatch: %d != %d' % (actual, size))
    # 原子 move：临时文件 -> 最终路径
    try:
        os.rename(path, _final_path)
        print('FASTDL_ATOMIC_MOVE %s -> %s' % (path, _final_path), flush=True)
    except OSError as e:
        raise RuntimeError('atomic move failed: %r' % e)
    _record_mode(mode_out, note)
    _dl_ok = True
    dt = time.time() - t0
    print('FASTDL_DONE %s dc=%s size=%d %.1fs (%.1f MB/s)'
          % (note, dc_id, size, dt, size / dt / 1048576 if dt > 0 else 0), flush=True)
    return _final_path
