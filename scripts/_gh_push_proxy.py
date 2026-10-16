"""本机推 GitHub 的绕行方案：一个只做字节转发的 CONNECT 代理。

**为什么需要它**（本机实测）：
  · `github.com` 解析到 20.205.243.166，**TCP 443 全被阻断** —— `git push` 报
    `Failed to connect to github.com port 443` / `Recv failure: Connection was reset`；
    换 140.82.112~121.x 等 GitHub US IP 也全部不可达。
  · 同网段的 **20.205.243.169 可达**，且它就是 GitHub 的边缘：带 SNI/Host `github.com`
    访问 `.../info/refs?service=git-upload-pack` 与 `.../git-receive-pack` 都返回 200
    （证书也是 github.com 的），所以**只要把 TCP 连到 .169，TLS 与 git 协议原样可用**。
  · `%SystemRoot%\\System32\\drivers\\etc\\hosts` 写不了（非管理员），所以不改 DNS，
    改用本地代理：git 用 `http.proxy` 发 `CONNECT github.com:443`，代理转连 .169。

用法：
    python scripts/_gh_push_proxy.py            # 监听 127.0.0.1:9418
    git -c http.proxy=http://127.0.0.1:9418 push -u origin main

代理只做 TCP 转发，不解密、不改写、不缓存；凭据仍由 Git Credential Manager 正常提供。
"""

from __future__ import annotations

import contextlib
import socket
import threading

LISTEN = ("127.0.0.1", 9418)
# 只有被阻断的主机需要改地址；其它主机原样直连。
UPSTREAM = {"github.com": "20.205.243.169"}


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)


def _handle(client: socket.socket) -> None:
    stream = client.makefile("rb")
    try:
        line = stream.readline()
        parts = line.split()
        if len(parts) < 2 or parts[0] != b"CONNECT":
            client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            client.close()
            return
        host, _, port = parts[1].decode().partition(":")
        while True:  # 吃掉请求头
            header = stream.readline()
            if header in (b"\r\n", b"\n", b""):
                break
        target = UPSTREAM.get(host, host)
        up = socket.create_connection((target, int(port or 443)), timeout=30)
    except OSError:
        with contextlib.suppress(OSError):
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        client.close()
        return

    client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    threading.Thread(target=_pipe, args=(client, up), daemon=True).start()
    threading.Thread(target=_pipe, args=(up, client), daemon=True).start()


def main() -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(LISTEN)
    server.listen(64)
    print(f"gh proxy on {LISTEN[0]}:{LISTEN[1]} -> {UPSTREAM}", flush=True)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
