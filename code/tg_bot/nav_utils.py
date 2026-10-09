"""导航器公共工具：把各处重复的"取最近消息 → 遍历按钮 → 找某类按钮"收敛到这里。
另提供事件驱动等待（snapshot / wait_for_change），替代写死的 sleep。"""
import re
import asyncio
import time as _time

from .nav_core import (  # noqa: F401  (re-export，保持旧的导入路径可用)
    Btn, classify_button, message_buttons, has_keyboard, page_label,
)


def find_button(msgs, pred):
    """在消息列表（新→旧）里找第一个满足 pred(stripped_text) 的按钮，找不到返回 None"""
    for m in msgs:
        for bt, raw in message_buttons(m):
            if bt and pred(bt):
                return Btn(bt, raw, m)
    return None


def find_kind(msgs, kind):
    return find_button(msgs, lambda t: classify_button(t) == kind)


async def click(btn):
    """用原文点击按钮"""
    return await btn.msg.click(text=btn.raw)


# ---------- 事件驱动等待 ----------

async def snapshot(client, bot_entity, limit=10):
    """对话当前状态签名：(最新消息 id, 最新菜单的 (id, 按钮, 编辑时间, 文本))。失败返回 None。"""
    try:
        msgs = await client.get_messages(bot_entity, limit=limit)
    except Exception as e:
        print('GETBOT_SNAPSHOT_FAIL: %r' % e, flush=True)
        return None
    top = msgs[0].id if msgs else 0
    menu = next((m for m in msgs if has_keyboard(m)), None)
    msig = None
    if menu is not None:
        msig = (menu.id,
                tuple(bt for bt, _ in message_buttons(menu)),
                getattr(menu, 'edit_date', None),
                getattr(menu, 'text', None))
    return (top, msig)


async def wait_for_change(client, bot_entity, before, timeout=10.0, poll=1.0, settle=1.0):
    """点击/发送之后等 bot 响应：出现新消息或菜单被编辑就返回 True（再等 settle 秒让一批消息到齐）；
    超时返回 False。before 是点击前的 snapshot；为 None 时退化成短等待。
    timeout 是上限，与旧的固定 sleep(timeout) 相比，bot 响应快时能提前继续。"""
    if before is None:
        await asyncio.sleep(min(timeout, 2))
        return False
    deadline = _time.monotonic() + timeout
    while True:
        remain = deadline - _time.monotonic()
        if remain <= 0:
            return False
        await asyncio.sleep(min(poll, remain))
        now = await snapshot(client, bot_entity)
        if now is not None and now != before:
            await asyncio.sleep(min(settle, max(0.0, deadline - _time.monotonic())))
            return True


_NUM_PAGE_RE = re.compile(r'(\d+)\s*/\s*(\d+)')


def parse_page_of_total(text):
    """'2/5' → (2, 5)，解析不了 → (None, None)"""
    m = _NUM_PAGE_RE.search(text or '')
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


async def collect_numbered_pages(client, bot_entity, collect_phase, last_id, *,
                                 max_pages=50, phase_prefix='page',
                                 timeout=60, idle_timeout=15, is_cancelled=None):
    """数字页码翻页收集：每轮只看"最新带键盘的那条消息"，点还没访问过的页码。
    返回 (last_id, visited_labels)。"""
    visited = set()
    for _ in range(max_pages):
        if is_cancelled and is_cancelled():
            break
        msgs = await client.get_messages(bot_entity, limit=10)
        menu = next((m for m in msgs if has_keyboard(m)), None)
        if menu is None:
            break
        # 带 · 的是当前页，视为已访问（否则翻到第2页后会回点第1页）
        visited.update(page_label(bt) for bt, _ in message_buttons(menu)
                       if '·' in bt and classify_button(bt) == 'NUMBER')
        btn = find_button(
            [menu],
            lambda t: classify_button(t) == 'NUMBER' and '·' not in t
            and page_label(t) not in visited)
        if btn is None:
            break
        label = page_label(btn.text)
        visited.add(label)
        try:
            await click(btn)
        except Exception as e:
            print('GETBOT_PAGE_CLICK_FAIL %s: %r' % (label, e), flush=True)
            break
        last_id = await collect_phase(last_id, _timeout=timeout, _idle_timeout=idle_timeout,
                                      _phase_name='%s%s' % (phase_prefix, label))
    return last_id, visited
