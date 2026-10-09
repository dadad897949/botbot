VERSION = "1.0.0"  # 2026-10-06: session 健康检查 + 自动重连

"""Session 健康检查：借鉴 Douyin_TikTok_Download_API 的自愈身份池思路。

- 定期检查 client 连接状态
- 断开时自动重连
- 记录健康状态供面板查看
"""

import asyncio
import time

# 健康状态
_health = {
    'user_client': {'ok': True, 'last_check': 0, 'last_ok': 0, 'fails': 0, 'reconnects': 0},
    'bot_client': {'ok': True, 'last_check': 0, 'last_ok': 0, 'fails': 0, 'reconnects': 0},
}

CHECK_INTERVAL = 60  # 每 60 秒检查一次
MAX_FAILS_BEFORE_RECONNECT = 3


async def _check_client(name, client):
    """检查单个 client，返回是否健康。
    轻量检查：is_connected（本地）+ ping（联网，低成本）。
    """
    st = _health[name]
    st['last_check'] = int(time.time())
    if client is None:
        st['ok'] = False
        st['fails'] += 1
        return False
    # 快速失败：TCP 未连接
    if not client.is_connected():
        st['ok'] = False
        st['fails'] += 1
        print('HEALTH_CHECK_FAIL %s: not connected (fails=%d)' % (name, st['fails']), flush=True)
        return False
    try:
        # 轻量 ping 验证 session 有效（PingRequest 比 get_me 轻得多）
        from telethon import functions
        import random
        await asyncio.wait_for(
            client(functions.PingRequest(ping_id=random.randint(1, 2**63 - 1))),
            timeout=10
        )
        st['ok'] = True
        st['last_ok'] = int(time.time())
        st['fails'] = 0
        return True
    except Exception as e:
        st['ok'] = False
        st['fails'] += 1
        print('HEALTH_CHECK_FAIL %s: %r (fails=%d)' % (name, e, st['fails']), flush=True)
        return False


async def _try_reconnect(name, client):
    """尝试重连（带节流：5 分钟内只试一次）。"""
    st = _health[name]
    if client is None:
        print('HEALTH_RECONNECT_SKIP %s: client is None' % name, flush=True)
        return False
    now = int(time.time())
    last_try = st.get('last_reconnect_try', 0)
    if now - last_try < 300:
        return False  # 5 分钟内试过，跳过
    st['last_reconnect_try'] = now
    try:
        print('HEALTH_RECONNECT %s ...' % name, flush=True)
        await asyncio.wait_for(client.connect(), timeout=30)
        from telethon import functions
        import random
        await asyncio.wait_for(
            client(functions.PingRequest(ping_id=random.randint(1, 2**63 - 1))),
            timeout=10
        )
        st['ok'] = True
        st['last_ok'] = now
        st['fails'] = 0
        st['reconnects'] += 1
        print('HEALTH_RECONNECT_OK %s' % name, flush=True)
        return True
    except Exception as e:
        print('HEALTH_RECONNECT_FAIL %s: %r' % (name, e), flush=True)
        return False


async def health_loop(user_client_getter, bot_client_getter):
    """后台健康检查循环。

    user_client_getter/bot_client_getter: 返回 client 对象的函数
    （用 getter 是因为 client 可能在重连时被替换）。
    """
    print('HEALTH_LOOP started (interval=%ds)' % CHECK_INTERVAL, flush=True)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL)
            for name, getter in (('user_client', user_client_getter),
                                 ('bot_client', bot_client_getter)):
                try:
                    client = getter()
                except Exception:
                    client = None
                ok = await _check_client(name, client)
                if not ok and _health[name]['fails'] >= MAX_FAILS_BEFORE_RECONNECT:
                    if client is not None:
                        await _try_reconnect(name, client)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print('HEALTH_LOOP_ERR: %r' % e, flush=True)


def get_health():
    """返回健康状态快照，供面板/API 查看。"""
    return {k: dict(v) for k, v in _health.items()}


def is_healthy():
    """所有 client 都健康才返回 True。"""
    return all(v['ok'] for v in _health.values())
