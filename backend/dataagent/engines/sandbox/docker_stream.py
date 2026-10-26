"""Docker exec 输出流清理。"""

import errno

from paramiko import Channel


def close_exec_stream(stream: object) -> None:
    """关闭输出流，并保证底层 HTTP 响应释放。"""
    response = getattr(stream, "_response", None)
    try:
        raw = getattr(response, "raw", None)
        socket_file = getattr(getattr(raw, "_fp", None), "fp", None)
        channel = getattr(socket_file, "channel", None)
        if isinstance(channel, Channel):
            # Docker SDK 的 CancellableStream.close 不支持 SSH；直接关闭
            # Paramiko 通道，防止成功命令报错并耗尽服务端 SSH 会话数。
            channel.close()
            return
        close_stream = getattr(stream, "close", None)
        if callable(close_stream):
            try:
                close_stream()
            except OSError as exc:
                if exc.errno != errno.ENOTCONN:
                    raise
    finally:
        close_response = getattr(response, "close", None)
        if callable(close_response):
            close_response()
