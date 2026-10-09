"""getbot 收集缓存：只存元数据，不存文件本体。
收集完成后立即写缓存，bot 在下载前重启可恢复任务。"""
import os
import sqlite3
import time
from contextlib import contextmanager

VERSION = "1.1.0"

_CACHE_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         'config', 'getbot_cache.db')
_inited = False

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS getbot_cache (
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    file_name TEXT,
    size_bytes INTEGER DEFAULT 0,
    is_photo INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    created_at REAL,
    updated_at REAL,
    PRIMARY KEY (task_id, msg_id)
);
CREATE TABLE IF NOT EXISTS getbot_task (
    task_id TEXT PRIMARY KEY,
    bot_username TEXT,
    cloud_dir TEXT,
    total INTEGER,
    created_at REAL
);
'''


@contextmanager
def _db():
    """连接 + 事务（异常回滚）+ 确保关闭。"""
    init_cache()
    conn = sqlite3.connect(_CACHE_DB, timeout=10)  # timeout 即 busy_timeout
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA synchronous=NORMAL')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_cache():
    """建表（进程内只执行一次；WAL 是库级持久设置，只需设一次）"""
    global _inited
    if _inited:
        return
    os.makedirs(os.path.dirname(_CACHE_DB), exist_ok=True)
    conn = sqlite3.connect(_CACHE_DB, timeout=10)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _inited = True


def _extract_meta(m):
    """从 Telethon Message 提取 (msg_id, fname, fsize, is_photo)；无 id 返回 None。"""
    msg_id = getattr(m, 'id', 0)
    if not msg_id:
        return None
    f = getattr(m, 'file', None)
    fname = getattr(f, 'name', None) or ''
    if not fname:
        doc = getattr(m, 'document', None)
        for attr in getattr(doc, 'attributes', None) or ():
            if getattr(attr, 'file_name', None):
                fname = attr.file_name
                break
    fsize = getattr(f, 'size', None) or 0
    return (msg_id, fname, fsize, 1 if getattr(m, 'photo', None) else 0)


def _dir_text(cloud_dir):
    """cloud_dir 落库前统一转文本。调用方传的是 cloud.prepare() 返回的 Target 列表，
    SQLite 绑定 list/对象会抛 ProgrammingError，导致多文件任务的缓存整个保存失败。"""
    if cloud_dir is None:
        return ''
    if isinstance(cloud_dir, str):
        return cloud_dir
    if isinstance(cloud_dir, (list, tuple, set)):
        return ','.join(_dir_text(t) for t in cloud_dir)
    key, fid = getattr(cloud_dir, 'key', None), getattr(cloud_dir, 'fid', None)
    if key is not None or fid is not None:
        return '%s:%s' % (key or '', fid or '')
    return str(cloud_dir)


def save_collection(task_id, collected, bot_username, cloud_dir, chat_id):
    """保存收集的元数据。collected 是 Telethon Message 列表。
    已存在的 (task_id, msg_id) 只更新元数据，不改 status（done 不会被打回 pending）。"""
    now = time.time()
    rows, skipped = [], 0
    for idx, m in enumerate(collected):
        meta = _extract_meta(m)
        if meta is None:
            skipped += 1
            print('GETBOT_CACHE skip msg without id (idx=%d)' % idx, flush=True)
            continue
        rows.append((task_id, idx, meta[0], chat_id, meta[1], meta[2], meta[3], now, now))
    with _db() as conn:
        # total 用实际写入数：原来用 len(collected)，有被跳过的消息时任务永远判为"未完成"
        conn.execute('INSERT OR REPLACE INTO getbot_task VALUES (?, ?, ?, ?, ?)',
                     (task_id, bot_username, _dir_text(cloud_dir), len(rows), now))
        conn.executemany('''
            INSERT INTO getbot_cache
            (task_id, idx, msg_id, chat_id, file_name, size_bytes, is_photo, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(task_id, msg_id) DO UPDATE SET
                idx=excluded.idx, chat_id=excluded.chat_id, file_name=excluded.file_name,
                size_bytes=excluded.size_bytes, is_photo=excluded.is_photo,
                updated_at=excluded.updated_at
        ''', rows)
    print('GETBOT_CACHE saved %d files (skipped %d) for task %s' % (len(rows), skipped, task_id),
          flush=True)


def mark_done(task_id, msg_id):
    """标记单个文件下载完成"""
    mark_done_many(task_id, [msg_id])


def mark_done_many(task_id, msg_ids):
    """批量标记完成（一个事务）"""
    now = time.time()
    with _db() as conn:
        conn.executemany(
            "UPDATE getbot_cache SET status='done', updated_at=? WHERE task_id=? AND msg_id=?",
            [(now, task_id, mid) for mid in msg_ids])


def get_pending(task_id):
    """获取待下载的项"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT msg_id, chat_id, file_name, size_bytes, is_photo FROM getbot_cache "
            "WHERE task_id=? AND status='pending' ORDER BY idx", (task_id,)).fetchall()
    return [dict(r) for r in rows]


def get_incomplete_tasks():
    """未完成任务（重启恢复用）。LEFT JOIN 保留"任务建了但 cache 没写"的情况。"""
    with _db() as conn:
        rows = conn.execute('''
            SELECT t.task_id, t.bot_username, t.cloud_dir, t.total,
                   COALESCE(SUM(c.status='pending'), 0) AS pending,
                   COALESCE(SUM(c.status='done'), 0) AS done
            FROM getbot_task t LEFT JOIN getbot_cache c ON t.task_id=c.task_id
            GROUP BY t.task_id
            HAVING pending > 0 OR done < t.total
        ''').fetchall()
    return [dict(r) for r in rows]


def clear_task(task_id):
    """清除任务"""
    with _db() as conn:
        conn.execute("DELETE FROM getbot_cache WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM getbot_task WHERE task_id=?", (task_id,))
    print('GETBOT_CACHE cleared task %s' % task_id, flush=True)
