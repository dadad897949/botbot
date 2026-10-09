"""阿里云盘扫码登录（Telegram 机器人用）。"""
import os
import time
import qrcode
import asyncio

QR_DIR = "/tmp"
_qr_counter = [0]

def _make_qr_png(qr_link):
    _qr_counter[0] += 1
    path = os.path.join(QR_DIR, "alipan_qr_%d.png" % _qr_counter[0])
    img = qrcode.make(qr_link)
    img.save(path)
    return path


def login_qr_loop(send_tg_fn=None, timeout=300, attempts=3, main_loop=None):
    """扫码登录阿里云盘，二维码过期自动重发。

    send_tg_fn: async (png_path, caption) -> None
    main_loop: 主事件循环，用于线程安全的 TG 发送
    """
    from aligo import Aligo

    last_err = None
    for i in range(1, attempts + 1):
        def _on_qr(qr_link):
            png = _make_qr_png(qr_link)
            print("ALIPAN_QR_SAVED %s" % png, flush=True)
            if send_tg_fn and main_loop:
                caption = "🔐 阿里云盘登录二维码（第 %d 张）\n用阿里云盘 App 扫一下，2 分钟内有效" % i
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        send_tg_fn(png, caption), main_loop)
                    fut.result(timeout=30)
                    print("ALIPAN_QR_SENT", flush=True)
                except Exception as e:
                    print("ALIPAN_QR_SEND_FAIL %r" % e, flush=True)
            return png

        try:
            login_file = os.path.expanduser("~/.aligo/tg_sync.json")
            if os.path.exists(login_file):
                os.remove(login_file)
            ali = Aligo(name="tg_sync", show=_on_qr, level="INFO")
            me = ali.get_user()
            print("ALIPAN_LOGIN_OK user=%s" % me.user_name, flush=True)
            return True
        except Exception as e:
            last_err = e
            print("ALIPAN_QR_ATTEMPT_%d_FAIL %r" % (i, e), flush=True)
            time.sleep(2)

    raise last_err or RuntimeError("阿里云盘扫码登录失败")
