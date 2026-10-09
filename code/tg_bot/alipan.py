import os
VERSION = "1.0.0"  # 2026-10-04 初始版本化
from aligo import Aligo

FOLDER_NAME = 'TG收藏'


def get_ali():
    """创建 aligo 实例并验证登录态有效。
    
    S2: 若 ~/.aligo/tg_sync.json 缺失/过期，aligo 会回落到扫码登录并永久阻塞。
    这里主动调一次轻量 API 验证，失败时抛明确异常，避免 bot 静默卡死。
    """
    proxy_url = os.environ.get('https_proxy') or os.environ.get('http_proxy')
    proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
    ali = Aligo(name='tg_sync', level='WARNING', proxies=proxies)
    # 验证登录态：调 get_user，若需扫码会抛异常或阻塞，这里加超时保护
    import threading
    err = []
    def _check():
        try:
            ali.get_user()
        except Exception as e:
            err.append(e)
    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        raise RuntimeError('阿里云盘登录验证超时（可能需重新扫码），请检查 ~/.aligo/tg_sync.json')
    if err:
        raise RuntimeError('阿里云盘登录失效: %s' % err[0])
    return ali


def ensure_folder(ali, name=FOLDER_NAME):
    for f in ali.get_file_list(parent_file_id='root'):
        if f.type == 'folder' and f.name == name:
            return f.file_id
    created = ali.create_folder(name=name, parent_file_id='root')
    return created.file_id


def sanitize(name):
    """Make a string safe for use as a cloud folder/file name."""
    name = name.replace('/', '_').replace('\\', '_').strip()
    name = ''.join(c for c in name if ord(c) >= 32)
    return name[:120] or '未命名视频'


def ensure_subfolder(ali, parent_id, name):
    """Find or create a subfolder under parent_id."""
    name = sanitize(name)
    for f in ali.get_file_list(parent_file_id=parent_id):
        if f.type == 'folder' and f.name == name:
            return f.file_id, name
    created = ali.create_folder(name=name, parent_file_id=parent_id)
    return created.file_id, name
