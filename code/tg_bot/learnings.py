"""Transfer learnings: Hindsight-inspired Retain / Recall / Reflect.

v1.0.0 (2026-10-06): 学 vectorize-io/hindsight 的三操作模式。
不用向量库，轻量 JSONL 实现：

- Retain:  每次转存结束记录一条 observation（来源、大小、用时、成败）
- Recall:  新任务开始时查该来源的历史统计
- Reflect: 生成一句人话洞察（如"该频道近7天成功率95%"）

文件：与 history.jsonl 同目录的 learnings.jsonl
"""
import json
import os
import time

VERSION = "1.0.0"

_LEARNINGS_FILE = None


def init(path=None):
    """初始化。path 为 learnings.jsonl 完整路径，不传则用默认。"""
    global _LEARNINGS_FILE
    _LEARNINGS_FILE = path or os.path.expanduser(
        '~/workspace/TG转存机器人/learnings.jsonl')
    d = os.path.dirname(_LEARNINGS_FILE)
    if d:
        os.makedirs(d, exist_ok=True)


def _path():
    if not _LEARNINGS_FILE:
        init()
    return _LEARNINGS_FILE


def retain(source, size_mb=0, duration_s=0, success=True, error=None,
           media_type='video'):
    """Retain: 记录一次转存观察。"""
    rec = {
        'ts': int(time.time()),
        'source': source or 'unknown',
        'size_mb': round(size_mb, 1),
        'duration_s': round(duration_s, 1),
        'success': bool(success),
        'error': (error or '')[:120],
        'media_type': media_type,
    }
    try:
        with open(_path(), 'a') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception:
        pass


def recall(source, days=7):
    """Recall: 查某来源近 N 天的统计。返回 dict，无数据时返回 None。"""
    cutoff = int(time.time()) - days * 86400
    total = 0
    ok = 0
    durations = []
    sizes = []
    errors = {}
    try:
        with open(_path()) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get('ts', 0) < cutoff:
                    continue
                if r.get('source') != source:
                    continue
                total += 1
                if r.get('success'):
                    ok += 1
                    if r.get('duration_s'):
                        durations.append(r['duration_s'])
                    if r.get('size_mb'):
                        sizes.append(r['size_mb'])
                else:
                    e = r.get('error') or 'unknown'
                    errors[e] = errors.get(e, 0) + 1
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if total == 0:
        return None
    return {
        'total': total,
        'success_rate': round(ok / total * 100, 1),
        'avg_duration_s': round(sum(durations) / len(durations), 1) if durations else 0,
        'avg_size_mb': round(sum(sizes) / len(sizes), 1) if sizes else 0,
        'top_error': max(errors, key=errors.get) if errors else None,
    }


def reflect(source, days=7):
    """Reflect: 生成一句人话洞察。无数据时返回空字符串。"""
    st = recall(source, days)
    if not st or st['total'] < 2:
        return ''
    parts = ['📈 %s 近%d天：%d次转存，成功率%.0f%%' % (
        source, days, st['total'], st['success_rate'])]
    if st['avg_duration_s'] > 0:
        parts.append('平均用时%.0f秒' % st['avg_duration_s'])
    if st['success_rate'] < 70 and st['top_error']:
        parts.append('⚠️ 主要失败原因：%s' % st['top_error'][:30])
    return '，'.join(parts)
