"""D类导航：分组型。首组直接收，循环点 NEXT 按钮。"""
import re

from .nav_utils import (find_kind, parse_page_of_total, click,
                        snapshot, wait_for_change)

_TOTAL_RE = re.compile(r'共\s*(\d+)\s*组')
MAX_GROUPS = 50


async def _recent(client, bot_entity):
    try:
        return await client.get_messages(bot_entity, limit=10)
    except Exception:
        return []


async def _nav_group_collect(_client, bot_entity, _collect_phase, _gb_key, _getbot_cancel, _t, last_id=0):
    """返回 (is_group_mode, last_id)；不是分组模式返回 (False, None)。"""
    try:
        msgs = await _client.get_messages(bot_entity, limit=10)
    except Exception:
        return False, None
    nxt = find_kind(msgs, 'NEXT')
    if not nxt:
        return False, None
    # 总组数：优先取按钮上的 "n/m"，否则取最近一条"共 N 组"文本
    _, total = parse_page_of_total(nxt.text)
    if not total:
        for m in msgs:
            tm = _TOTAL_RE.search(getattr(m, 'text', '') or '')
            if tm:
                total = int(tm.group(1))
                break
    # 必须有总组数，否则不是 D 类（防误判 zhangsheng984_bot）
    if not total:
        print('GETBOT group: no total groups text, not group mode', flush=True)
        return False, None
    if total > MAX_GROUPS:
        print('GETBOT group: total=%d > MAX_GROUPS, truncated' % total, flush=True)
    print('GETBOT group mode: total=%d' % total, flush=True)

    # last_id 用调用方传入的（/start 之前的）
    last_id = await _collect_phase(last_id, _timeout=90, _idle_timeout=20, _phase_name='group1')
    done = 1
    print('GETBOT group1 done', flush=True)
    while done < min(total, MAX_GROUPS):
        if _gb_key and _getbot_cancel.get(_gb_key):
            break
        nxt = find_kind(await _recent(_client, bot_entity), 'NEXT')
        if not nxt:
            break
        before = await snapshot(_client, bot_entity)
        try:
            await click(nxt)
            print('GETBOT group click: %s' % nxt.text, flush=True)
        except Exception:
            break
        await wait_for_change(_client, bot_entity, before, timeout=4)
        done += 1
        last_id = await _collect_phase(last_id, _timeout=90, _idle_timeout=20,
                                       _phase_name='group%d' % done)
        print('GETBOT group%d done' % done, flush=True)
    return True, last_id
