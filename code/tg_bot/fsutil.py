"""文件工具：原子写入 JSON（先写临时文件 + fsync，再 os.replace 覆盖）。
进程在写入中途被杀时，目标文件要么是旧内容、要么是新内容，不会出现截断的半截 JSON。"""
import json
import os


def atomic_write_json(path, obj, **dump_kwargs):
    """原子写 JSON。失败时抛异常（调用方自行决定是否吞掉），并清理临时文件。
    若目标文件已存在，沿用它的权限位（配置里可能有敏感信息，设成了 0600 就保持）。"""
    dump_kwargs.setdefault('ensure_ascii', False)
    d = os.path.dirname(path) or '.'
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        os.makedirs(d, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except OSError:
            pass  # 目标不存在：保持默认权限
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
