# -*- coding: utf-8 -*-
"""CLI 测试客户端：向翻译服务器发一条请求，打印响应。

用法: python -m translator.client <文本> [--port 24567]
"""

import argparse
import json
import socket
import sys


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('text')
    p.add_argument('--port', type=int, default=24567)
    p.add_argument('--host', default='127.0.0.1')
    a = p.parse_args(argv)

    s = socket.create_connection((a.host, a.port), timeout=5)
    s.settimeout(15)
    req_id = 'cli-%d' % (id(s) % 100000)
    s.sendall((json.dumps({'type': 'req', 'id': req_id, 'text': a.text,
                           'prefetch': False}) + '\n').encode('utf-8'))
    buf = b''
    while b'\n' not in buf:
        buf += s.recv(4096)
    resp = json.loads(buf.split(b'\n', 1)[0].decode('utf-8'))
    print('原文:', a.text)
    print('译文:', resp.get('text'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
