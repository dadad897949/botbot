"""统一导航器：bot 菜单遍历模块。

不硬编码按钮文本，靠 classify_button 分类 + 状态机，自动走通
"菜单 → 动作 → 新菜单 → 文件"型的 bot。

- classify_button: 按钮分类器（定义在 nav_core，这里 re-export 保持兼容）
- hash_menu_state: 菜单状态哈希（去重防循环）
- parse_jump_range: 跳转范围解析
- unified_navigator: 统一导航器
- _nav_jump_collect: 导航+跳转组合模式（@zhangsheng984_bot，旧版保留）
- _jump_once: 两种模式共用的"点跳转 → 输入序号"单步
"""
import re
import asyncio
import hashlib

from .nav_core import (  # noqa: F401
    Btn, classify_button, message_buttons, has_keyboard, page_label,
)
from .nav_utils import snapshot, wait_for_change, find_button, click

VERSION = "1.3.0"


def hash_menu_state(menu_msg, with_text=True):
    """菜单状态哈希：按钮文本排序后的元组（+ 消息正文摘要），用于去重防循环。
    按钮完全相同但正文不同（如"第2页/第3页"）的菜单不会再被误判为已访问。
    with_text=False 可退回旧行为（正文含时间戳等易变内容时用）。"""
    try:
        texts = [bt for bt, _ in message_buttons(menu_msg)]
        if not texts:
            return None
        state = tuple(sorted(texts))
        if with_text:
            body = (getattr(menu_msg, 'text', None) or '').strip()
            if body:
                state += ('#' + hashlib.md5(body.encode('utf-8')).hexdigest()[:12],)
        return state
    except Exception as e:
        print('GETBOT_NAV_HASH_FAIL: %r' % e, flush=True)
        return None


_JUMP_RES = (
    (re.compile(r'(\d+)-(\d+)/(\d+)'), lambda m: (int(m.group(2)), int(m.group(3)))),
    (re.compile(r'(\d+)-(\d+)'), lambda m: (int(m.group(1)), int(m.group(2)))),
    (re.compile(r'(\d+)\s*/\s*(\d+)\s*条'), lambda m: (int(m.group(1)), int(m.group(2)))),
)


def parse_jump_range(text):
    """解析跳转范围：'1-23/99' → (23, 99)；'23-99' → (23, 99)；'23 / 99 条' → (23, 99)。
    失败返回 (None, None)。"""
    for rx, fn in _JUMP_RES:
        m = rx.search(text or '')
        if m:
            return fn(m)
    return None, None


async def _resolve_jump(client, bot_entity, btn_text):
    """解析下一个跳转序号：先看按钮文本，再看最近 5 条消息。返回 (next_num, total)。"""
    cur, tot = parse_jump_range(btn_text)
    if cur:
        return cur + 1, tot
    try:
        for m in await client.get_messages(bot_entity, limit=5):
            cur, tot = parse_jump_range(getattr(m, 'text', '') or '')
            if cur:
                return cur + 1, tot
    except Exception:
        pass
    return None, None


async def _jump_once(client, bot_entity, do_click, btn_text, action_delay=10, click_wait=3,
                     next_hint=None, total_hint=None):
    """跳转单步：点跳转按钮 → 等 bot 提示输入 → 发送下一批序号 → 等响应。
    do_click: 无参协程工厂，负责点按钮。next_hint/total_hint: 调用方已算好序号时直接用，
    否则用 _resolve_jump 从按钮文本/最近消息解析。
    返回 'sent'（已发序号）| 'complete'（已到末尾）| 'fail'。"""
    before = await snapshot(client, bot_entity)
    try:
        await do_click()
    except Exception as e:
        print('GETBOT_NAV_CLICK_FAIL: %r' % e, flush=True)
        return 'fail'
    await wait_for_change(client, bot_entity, before, timeout=click_wait)

    if next_hint:
        next_num, total = next_hint, total_hint
    else:
        next_num, total = await _resolve_jump(client, bot_entity, btn_text)
    if next_num and total and next_num > total:
        print('GETBOT navigator jump complete %d/%d' % (next_num - 1, total), flush=True)
        try:
            await client.send_message(bot_entity, '/cancel')
        except Exception:
            pass
        return 'complete'
    if not next_num:
        print('GETBOT navigator jump: cannot parse next', flush=True)
        return 'fail'

    before = await snapshot(client, bot_entity)
    try:
        await client.send_message(bot_entity, str(next_num))
    except Exception as e:
        print('GETBOT_NAV_JUMP_SEND_FAIL: %r' % e, flush=True)
        return 'fail'
    await wait_for_change(client, bot_entity, before, timeout=action_delay)
    return 'sent'


# 动作优先级：ALL > ENTER > MEDIA_TYPE > NUMBER > NEXT > JUMP；BACK 不主动点
_PRIORITY = {'ALL': 0, 'ENTER': 1, 'MEDIA_TYPE': 2, 'NUMBER': 3, 'NEXT': 4, 'JUMP': 5}


async def unified_navigator(_client, bot_entity, _collect_phase, _gb_key, _getbot_cancel,
                            _transfer_status, _t, _action_delay=10):
    """统一导航器：遍历 bot 菜单状态机，收集所有文件。返回 True 表示处理完成。
    _action_delay 现在是"等 bot 响应的上限"：bot 响应后会提前继续，不再固定睡满。"""
    max_iter = 100
    visited_states = set()
    seen_pages = set()  # 已点过/当前所在的页码，数字页码只点没去过的
    last_id = 0
    try:
        ms = await _client.get_messages(bot_entity, limit=1)
        last_id = ms[0].id if ms else 0
    except Exception:
        pass

    it = 0
    while it < max_iter:
        it += 1
        if _gb_key and _getbot_cancel.get(_gb_key):
            print('GETBOT navigator cancelled', flush=True)
            break
        # 1. 找最新菜单
        menu = None
        try:
            menu = next((m for m in await _client.get_messages(bot_entity, limit=10)
                         if has_keyboard(m)), None)
        except Exception as e:
            print('GETBOT_NAV_MENU_FAIL: %r' % e, flush=True)
        # 2. 状态去重
        state = hash_menu_state(menu) if menu else None
        if state is not None and state in visited_states:
            print('GETBOT navigator state revisited, done', flush=True)
            break
        if state is not None:
            visited_states.add(state)
        # 3. 收集当前文件
        last_id = await _collect_phase(last_id, _timeout=60, _idle_timeout=20,
                                       _phase_name='nav_%d' % it)
        # 4. 无菜单则结束
        if not menu:
            print('GETBOT navigator no menu, done', flush=True)
            break
        # 5. 选动作：(优先级, 次序键, 按钮文本, 原文, 类型)
        actions = []
        for idx, (bt, raw) in enumerate(message_buttons(menu)):
            kind = classify_button(bt)
            pri = _PRIORITY.get(kind)
            if pri is None:
                continue
            order = idx
            if kind == 'NUMBER':
                label = page_label(bt)
                if '·' in bt:            # 当前页
                    seen_pages.add(label)
                    continue
                if label in seen_pages:
                    continue
                order = int(label)       # 页码从小到大
            actions.append((pri, order, bt, raw, kind))
        if not actions:
            print('GETBOT navigator no actions, done', flush=True)
            break
        pri, _, btxt, raw, kind = min(actions, key=lambda a: a[:2])
        print('GETBOT navigator action: %s [%s]' % (kind, btxt), flush=True)
        if kind == 'NUMBER':
            seen_pages.add(page_label(btxt))

        if kind == 'JUMP':
            status = await _jump_once(_client, bot_entity,
                                      lambda m=menu, r=raw: m.click(text=r),
                                      btxt, action_delay=_action_delay)
            if status != 'sent':
                break
            continue

        before = await snapshot(_client, bot_entity)
        try:
            await menu.click(text=raw)
        except Exception as e:
            print('GETBOT_NAV_CLICK_FAIL: %r' % e, flush=True)
            break
        await wait_for_change(_client, bot_entity, before, timeout=_action_delay)
    print('GETBOT navigator done, iters=%d' % it, flush=True)
    return True


_JUMP_BTN_RE = re.compile(r'(\d+)-(\d+)')
_JUMP_TEXT_RE = re.compile(r'(\d+)-(\d+)/(\d+)')
_JUMP_TOTAL_RE = re.compile(r'/(\d+)')


def _find_jump_button(msgs):
    """在最近消息（新→旧）里找跳转按钮，返回 (Btn|None, cur_max, cur_total)。
    按钮文本里的"a-b[/total]"优先；没带 total 时用较新消息正文里的"a-b/total"补。"""
    jbtn, cur_max, cur_total = None, 0, 0
    for m in msgs:
        for bt, raw in message_buttons(m):
            rm = _JUMP_BTN_RE.search(bt)
            if rm and ('跳转' in bt or int(rm.group(1)) > 1):
                jbtn = Btn(bt, raw, m)
                cur_max = int(rm.group(2))
                tm = _JUMP_TOTAL_RE.search(bt)
                if tm:
                    cur_total = int(tm.group(1))
                break
        if jbtn:
            break
        tm = _JUMP_TEXT_RE.search(getattr(m, 'text', '') or '')
        if tm:
            cur_max, cur_total = int(tm.group(2)), int(tm.group(3))
    return jbtn, cur_max, cur_total


async def _nav_jump_collect(_client, bot_entity, _collect_phase, _gb_key, _getbot_cancel, _t):
    """导航+跳转组合模式（@zhangsheng984_bot 这类）。
    点文件夹编号 → 点"查看内容" → 循环点跳转按钮+输入序号。
    返回 (is_nav_mode, last_id)。不是导航模式返回 (False, None)。"""
    # 检测：最近消息里"只有数字按钮、没有图片/视频按钮"的键盘 = 文件夹列表
    folders = []
    try:
        for m in await _client.get_messages(bot_entity, limit=10):
            btns = [bt for bt, _ in message_buttons(m)]
            if not btns:
                continue
            nums = [bt for bt in btns if page_label(bt).isdecimal()]
            if nums and not any('图片' in b or '视频' in b for b in btns):
                raws = dict(message_buttons(m))
                folders = [(bt, raws[bt], m) for bt in nums]
                break
    except Exception as e:
        print('GETBOT_NAVJUMP_DETECT_FAIL: %r' % e, flush=True)
        return False, None
    if not folders:
        return False, None
    print('GETBOT navjump: %d folders' % len(folders), flush=True)

    last_id = 0
    try:
        ms = await _client.get_messages(bot_entity, limit=1)
        last_id = ms[0].id if ms else 0
    except Exception:
        pass

    async def recent(n=10):
        try:
            return await _client.get_messages(bot_entity, limit=n)
        except Exception:
            return []

    def cancelled():
        return bool(_gb_key and _getbot_cancel.get(_gb_key))

    for fbt, fraw, fmsg in folders:
        if cancelled():
            break
        # 优先从最新消息里重新找文件夹按钮（列表被 bot 重发/编辑后旧对象可能失效）；
        # 找不到（已被新消息顶出最近 10 条）就退回最初检测到的那条消息，旧消息的按钮仍可点
        target = find_button(await recent(), lambda t, _b=fbt: t == _b) or Btn(fbt, fraw, fmsg)
        try:
            print('GETBOT navjump click folder: %s' % fbt, flush=True)
            before = await snapshot(_client, bot_entity)
            await click(target)
        except Exception as e:
            print('GETBOT navjump click fail %s: %r' % (fbt, e), flush=True)
            continue
        await wait_for_change(_client, bot_entity, before, timeout=3)

        view = find_button(await recent(), lambda t: '查看内容' in t)
        if view:
            try:
                print('GETBOT navjump click view-content: %s' % view.text, flush=True)
                before = await snapshot(_client, bot_entity)
                await click(view)
                await wait_for_change(_client, bot_entity, before, timeout=5)
            except Exception as e:
                print('GETBOT navjump view-content click fail: %r' % e, flush=True)
        else:
            print('GETBOT navjump: no view-content btn, collect directly', flush=True)
            await asyncio.sleep(1)
        last_id = await _collect_phase(last_id, _timeout=60, _idle_timeout=20,
                                       _phase_name='navjump_0')

        for j in range(1, 31):
            if cancelled():
                break
            jbtn, cur_max, cur_total = _find_jump_button(await recent())
            if not jbtn:
                break
            nxt = cur_max + 1
            if cur_total and nxt > cur_total:
                break
            status = await _jump_once(_client, bot_entity, lambda b=jbtn: click(b), jbtn.text,
                                      action_delay=10, next_hint=nxt, total_hint=cur_total)
            if status != 'sent':
                break
            last_id = await _collect_phase(last_id, _timeout=60, _idle_timeout=20,
                                           _phase_name='navjump_%d' % j)

        # 返回文件夹列表（只点一次；原版会在多条消息上重复点）
        back = find_button(await recent(), lambda t: '返回' in t and '列表' in t)
        if back:
            try:
                before = await snapshot(_client, bot_entity)
                await click(back)
                await wait_for_change(_client, bot_entity, before, timeout=3)
            except Exception:
                pass
    return True, last_id
