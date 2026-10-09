VERSION = "1.0.1"  # 2026-10-06: 修 save_thread 重置水位

"""讨论组消息缓存：SQLite 本地化，避免每次全量扫描。

原理：
- Telegram 同一聊天内消息 ID 单调递增
- 评论 C 一定满足 C.id > R.id（R = thread root）
- 扫描下界精确 = R，无需 magic number

表：
- discussion: 讨论组元数据 + 扫描水位
- msg: 消息缓存（只存映射需要的字段）
- thread: 频道帖子 → thread root 映射
"""

import os
import sqlite3
import time

_DB_PATH = os.path.expanduser('~/workspace/TG转存机器人/disc_cache.db')
# 服务器路径
if os.path.exists('/root/TG转存机器人'):
    _DB_PATH = '/root/TG转存机器人/disc_cache.db'


def _get_db():
    db = sqlite3.connect(_DB_PATH)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    return db


def init_db():
    """初始化表结构。"""
    db = _get_db()
    try:
        db.execute('''
            CREATE TABLE IF NOT EXISTS discussion (
                peer_id INTEGER PRIMARY KEY,
                title TEXT,
                last_scanned_max_id INTEGER NOT NULL DEFAULT 0,
                last_scan_ts INTEGER NOT NULL DEFAULT 0
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS msg (
                peer_id INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,
                reply_to_top_id INTEGER,
                reply_to_msg_id INTEGER,
                has_media INTEGER NOT NULL DEFAULT 0,
                grouped_id INTEGER,
                date INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (peer_id, msg_id)
            )
        ''')
        db.execute('''
            CREATE INDEX IF NOT EXISTS idx_msg_top
            ON msg(peer_id, reply_to_top_id)
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS thread (
                channel_id INTEGER NOT NULL,
                post_id INTEGER NOT NULL,
                peer_id INTEGER NOT NULL,
                root_msg_id INTEGER NOT NULL,
                root_date INTEGER NOT NULL DEFAULT 0,
                last_scanned_max INTEGER NOT NULL DEFAULT 0,
                last_sync_ts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (channel_id, post_id)
            )
        ''')
        # 兼容老表：加列
        try:
            db.execute('ALTER TABLE thread ADD COLUMN last_scanned_max INTEGER NOT NULL DEFAULT 0')
        except Exception:
            pass
        db.execute('''
            CREATE INDEX IF NOT EXISTS idx_thread_peer_root
            ON thread(peer_id, root_msg_id)
        ''')
        db.commit()
    finally:
        db.close()


def get_discussion_water(peer_id):
    """返回讨论组的已扫描最大 ID（水位），0 表示无缓存。"""
    db = _get_db()
    try:
        row = db.execute(
            'SELECT last_scanned_max_id FROM discussion WHERE peer_id=?',
            (peer_id,)
        ).fetchone()
        return row[0] if row else 0
    finally:
        db.close()


def get_thread_root(channel_id, post_id):
    """返回 (peer_id, root_msg_id)，无缓存时返回 (None, None)。"""
    db = _get_db()
    try:
        row = db.execute(
            'SELECT peer_id, root_msg_id FROM thread WHERE channel_id=? AND post_id=?',
            (channel_id, post_id)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        db.close()


def save_thread(channel_id, post_id, peer_id, root_msg_id, root_date=0):
    """保存帖子 → root 映射（保留已有的 last_scanned_max 水位）。"""
    db = _get_db()
    try:
        db.execute('''
            INSERT INTO thread
            (channel_id, post_id, peer_id, root_msg_id, root_date, last_sync_ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id, post_id) DO UPDATE SET
                peer_id=excluded.peer_id,
                root_msg_id=excluded.root_msg_id,
                root_date=excluded.root_date,
                last_sync_ts=excluded.last_sync_ts
        ''', (channel_id, post_id, peer_id, root_msg_id, root_date, int(time.time())))
        # 确保 discussion 行存在
        db.execute('''
            INSERT OR IGNORE INTO discussion (peer_id, last_scanned_max_id)
            VALUES (?, 0)
        ''', (peer_id,))
        db.commit()
    finally:
        db.close()


def save_messages(peer_id, messages):
    """批量保存消息。
    messages: [{'msg_id', 'reply_to_top_id', 'reply_to_msg_id', 'has_media', 'grouped_id', 'date'}]
    返回保存的最大 msg_id。
    """
    if not messages:
        return 0
    db = _get_db()
    try:
        max_id = 0
        for m in messages:
            db.execute('''
                INSERT OR REPLACE INTO msg
                (peer_id, msg_id, reply_to_top_id, reply_to_msg_id, has_media, grouped_id, date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                peer_id, m['msg_id'],
                m.get('reply_to_top_id'), m.get('reply_to_msg_id'),
                1 if m.get('has_media') else 0,
                m.get('grouped_id'), m.get('date', 0),
            ))
            if m['msg_id'] > max_id:
                max_id = m['msg_id']
        # 更新水位
        db.execute('''
            UPDATE discussion SET last_scanned_max_id=MAX(last_scanned_max_id, ?),
            last_scan_ts=? WHERE peer_id=?
        ''', (max_id, int(time.time()), peer_id))
        db.commit()
        return max_id
    finally:
        db.close()


def get_thread_comments(peer_id, root_msg_id):
    """从缓存查某帖子的所有评论 msg_id（含嵌套）。
    返回 [msg_id, ...]。
    """
    db = _get_db()
    try:
        rows = db.execute('''
            SELECT msg_id FROM msg
            WHERE peer_id=? AND msg_id > ?
              AND (reply_to_top_id=? OR reply_to_msg_id=?)
            ORDER BY msg_id
        ''', (peer_id, root_msg_id, root_msg_id, root_msg_id)).fetchall()
        return [r[0] for r in rows]
    finally:
        db.close()


def get_media_messages(peer_id, msg_ids):
    """返回指定消息中带媒体的（msg_id, grouped_id）。"""
    if not msg_ids:
        return []
    db = _get_db()
    try:
        placeholders = ','.join('?' * len(msg_ids))
        rows = db.execute(
            'SELECT msg_id, grouped_id FROM msg WHERE peer_id=? AND msg_id IN (%s) AND has_media=1'
            % placeholders,
            (peer_id,) + tuple(msg_ids)
        ).fetchall()
        return rows
    finally:
        db.close()


def get_thread_water(channel_id, post_id):
    """返回某帖子的已扫描最大 ID，0 表示没扫过。"""
    db = _get_db()
    try:
        row = db.execute(
            'SELECT last_scanned_max, root_msg_id FROM thread WHERE channel_id=? AND post_id=?',
            (channel_id, post_id)
        ).fetchone()
        return (row[0], row[1]) if row else (0, None)
    finally:
        db.close()


def update_thread_water(channel_id, post_id, max_id):
    """更新某帖子的扫描水位。"""
    db = _get_db()
    try:
        db.execute('''
            UPDATE thread SET last_scanned_max=MAX(last_scanned_max, ?),
            last_sync_ts=? WHERE channel_id=? AND post_id=?
        ''', (max_id, int(time.time()), channel_id, post_id))
        db.commit()
    finally:
        db.close()


def cleanup_old(days=30):
    """清理 N 天未扫描的讨论组缓存。"""
    cutoff = int(time.time()) - days * 86400
    db = _get_db()
    try:
        # 找出过期的 peer
        rows = db.execute(
            'SELECT peer_id FROM discussion WHERE last_scan_ts < ?', (cutoff,)
        ).fetchall()
        for (peer_id,) in rows:
            db.execute('DELETE FROM msg WHERE peer_id=?', (peer_id,))
            db.execute('DELETE FROM thread WHERE peer_id=?', (peer_id,))
            db.execute('DELETE FROM discussion WHERE peer_id=?', (peer_id,))
        db.commit()
        return len(rows)
    finally:
        db.close()


# 初始化
init_db()
