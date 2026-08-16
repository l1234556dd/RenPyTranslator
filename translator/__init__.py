# -*- coding: utf-8 -*-
"""renpy-translator 核心包：Ren'Py 实时翻译工具（类 MTool）。"""

__version__ = '0.1.0'


def setup_safe_io():
    """Windows 控制台默认 GBK，无法打印部分字符（如游戏 UI 的 U+272D 装饰符），
    统一转 UTF-8 输出并容错，避免 print 崩溃。"""
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
