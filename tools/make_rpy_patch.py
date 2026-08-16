# -*- coding: utf-8 -*-
"""
tools/make_rpy_patch.py — 把 gamehook/hook.py 打包成临时 .rpy 补丁（M0 验证用）

生成的 .rpy 放进游戏 game/ 目录，启动游戏即加载 hook（`init 999 python:` 块）。
验证完务必删除，避免污染游戏目录。

用法: python tools/make_rpy_patch.py [输出路径，默认 zz_mt_hook.rpy]
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_PATH = os.path.join(HERE, '..', 'gamehook', 'hook.py')


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else 'zz_mt_hook.rpy'
    with open(HOOK_PATH, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    body = '\n'.join(('    ' + ln) if ln.strip() else '' for ln in lines)
    patch = ('# M0 验证用临时补丁：验证完请删除本文件。\n'
             'init 999 python:\n' + body + '\n')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(patch)
    print('已生成:', os.path.abspath(out_path))
    print('下一步：复制到游戏 game/ 目录 -> 启动 tools/test_server.py -> 启动游戏')


if __name__ == '__main__':
    main()
