# -*- coding: utf-8 -*-
"""
tools/test_server.py — M0 验证用假翻译工具（多线程版）

监听 TCP 端口，打印游戏侧发来的原文，回一条带【译】前缀的假译文。
每个连接独立线程处理，单个坏连接不会卡死整个服务。
用于验证：注入/Hook 链路是否通、TCP 协议是否正确、缓存是否生效。

用法: python -u tools/test_server.py [端口]
"""

import json
import socket
import sys
import threading

# Windows 控制台默认 GBK，无法打印部分特殊字符（如游戏 UI 里的装饰符号），
# 统一转 UTF-8 输出并容错。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

HOST = '127.0.0.1'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 24567


def handle(conn, addr):
    print('== 游戏侧已连接:', addr)
    buf = b''
    try:
        while True:
            while b'\n' not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    raise EOFError('连接关闭')
                buf += chunk
            line, buf = buf.split(b'\n', 1)
            req = json.loads(line.decode('utf-8'))
            if req.get('type') != 'req':
                continue
            tag = '预取' if req.get('prefetch') else '对话'
            print('[%s] id=%s text=%r' % (tag, req.get('id'), req.get('text')))
            resp = {
                'type': 'res',
                'id': req.get('id'),
                'text': '【译】' + req.get('text', ''),
            }
            conn.sendall((json.dumps(resp, ensure_ascii=False) + '\n').encode('utf-8'))
    except Exception as e:
        print('== 连接结束:', e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(5)
    print('test_server 监听中: %s:%d' % (HOST, PORT))
    print('（收到原文会打印，并回复【译】+原文）')
    while True:
        conn, addr = s.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == '__main__':
    main()
