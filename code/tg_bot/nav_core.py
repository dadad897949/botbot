"""导航器底层工具：按钮解析与分类。不依赖包内其他模块，避免循环导入。"""
import re
from collections import namedtuple
from functools import lru_cache

# text: strip 后的文本（用于分类/日志）；raw: 原文（Telethon click 精确匹配要用原文）
Btn = namedtuple('Btn', 'text raw msg')

_MEDIA_WORDS = ('图片', '视频', '文件', '音频', '音乐')
_NEXT_WORDS = ('下一组', '下一页', '下一批', '继续', '更多', '查看更多')
_ENTER_WORDS = ('查看内容', '进入', '打开', '详情')
_BACK_WORDS = ('返回', 'BACK', '⬅')
_RANGE_RE = re.compile(r'\d+-\d+')


@lru_cache(maxsize=1024)
def classify_button(text):
    """按钮分类器。返回: NUMBER, ALL, BACK, NEXT, MEDIA_TYPE, JUMP, ENTER, UNKNOWN

    判断顺序很重要：BACK / NEXT 必须先于 MEDIA_TYPE，否则
    "返回文件列表""继续下载视频" 会因含有"文件""视频"被误判成媒体类型。
    """
    t = (text or '').strip()
    # 纯数字（页码/文件夹编号），允许装饰符 · 和空格。isdecimal 排除 '²' 这类 isdigit 误判
    if t.replace('·', '').replace(' ', '').isdecimal():
        return 'NUMBER'
    if '全部' in t and ('获取' in t or '下载' in t):
        return 'ALL'
    tu = t.upper()
    if any(k in tu for k in _BACK_WORDS):
        return 'BACK'
    if any(k in t for k in _NEXT_WORDS):
        return 'NEXT'
    # 2026-10-09：含"文件夹"的是文件夹导航按钮，不是媒体类型
    if '文件夹' in t:
        return 'UNKNOWN'
    if any(k in t for k in _MEDIA_WORDS):
        return 'MEDIA_TYPE'
    if '跳转' in t:
        return 'JUMP'
    if any(k in t for k in _ENTER_WORDS):
        return 'ENTER'
    if _RANGE_RE.search(t):  # 范围跳转如 "23-99"
        return 'JUMP'
    return 'UNKNOWN'


def message_buttons(msg):
    """返回消息上所有按钮 [(stripped, raw), ...]；无键盘返回 []"""
    rm = getattr(msg, 'reply_markup', None)
    rows = getattr(rm, 'rows', None) if rm else None
    out = []
    for row in rows or ():
        for b in getattr(row, 'buttons', None) or ():
            raw = getattr(b, 'text', '') or ''
            out.append((raw.strip(), raw))
    return out


def has_keyboard(msg):
    return bool(message_buttons(msg))


def page_label(text):
    return text.replace('·', '').replace(' ', '')
