"""
getbot 导航器 v2（按 bot 类型分流）
A类（菜单按钮型）：Alice网盘、wenjianchucunbot → 点最优按钮（ALL > MEDIA_TYPE > ENTER）→ 收集
B类（文件夹导航型）：zhangsheng984_bot → _nav_jump_collect
C类（直接发媒体型）：普通 bot → 直接收集图片+视频
"""
import asyncio
import time as _t

from .nav_core import classify_button, message_buttons
from .nav_utils import snapshot, wait_for_change

# 出现这些按钮就判为 A 类（数字页码按钮也算分页型）
_A_KINDS = frozenset(('ALL', 'MEDIA_TYPE', 'ENTER', 'NUMBER'))
_CLICK_PRIORITY = {'ALL': 0, 'MEDIA_TYPE': 1, 'ENTER': 2}


async def detect_bot_type(client, bot_entity, last_id, timeout=10):
    """10 秒内检测 bot 类型。Returns: ('A', menu_msg) | ('C', None)
    B 类由 _nav_jump_collect 内部判断，这里不区分。"""
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        try:
            for m in await client.get_messages(bot_entity, limit=10):
                if getattr(m, 'id', 0) <= last_id:
                    continue
                if any(classify_button(bt) in _A_KINDS for bt, _ in message_buttons(m)):
                    return ('A', m)
        except Exception as e:
            print('GETBOT_DETECT_FAIL: %r' % e, flush=True)
        await asyncio.sleep(1)
    return ('C', None)


async def click_best_button(menu_msg, client=None, bot_entity=None):
    """点最优按钮：ALL > MEDIA_TYPE > ENTER。Returns: (clicked, button_text)
    client/bot_entity 可选；不传则尝试从消息对象取，取不到就退化成固定等 3 秒。"""
    client = client or getattr(menu_msg, 'client', None)
    bot_entity = bot_entity if bot_entity is not None else getattr(menu_msg, 'chat_id', None)
    best = None  # (priority, stripped, raw)
    for bt, raw in message_buttons(menu_msg):
        pri = _CLICK_PRIORITY.get(classify_button(bt)) if bt else None
        if pri is not None and (best is None or pri < best[0]):
            best = (pri, bt, raw)
    if not best:
        return (False, '')
    try:
        before = await snapshot(client, bot_entity) if client is not None and bot_entity is not None else None
        # 用原文点击（Telethon 精确匹配）；返回 None 视为没点中
        if await menu_msg.click(text=best[2]) is None:
            print('GETBOT_NAV click missed: %r' % best[1], flush=True)
            return (False, best[1])
        print('GETBOT_NAV clicked: %s (pri=%d)' % (best[1], best[0]), flush=True)
        if before is not None:
            await wait_for_change(client, bot_entity, before, timeout=3)
        else:
            await asyncio.sleep(3)
        return (True, best[1])
    except Exception as e:
        print('GETBOT_NAV_CLICK_FAIL: %r' % e, flush=True)
        return (False, '')
