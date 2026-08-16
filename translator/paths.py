# -*- coding: utf-8 -*-
"""路径管理：兼容 开发模式（源码目录）与 PyInstaller 冻结模式（_MEIPASS）。

- app_root():  内置资源根（hook.py / injector.py / 工具脚本所在处）
- data_dir():  用户数据根目录（config.json / games/）——冻结模式下用 %APPDATA%，
  避免写到 exe 所在目录（可能在 Program Files）。
- cache_path(game_dir): 按游戏分文件夹的缓存库路径
- config_path(): 全局配置文件（含 api_key 等跨游戏配置，不按游戏拆分）
"""

import hashlib
import os
import shutil
import sys


def is_frozen():
    return bool(getattr(sys, 'frozen', False))


def app_root():
    """内置资源根目录。"""
    if is_frozen():
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_dir():
    """用户数据根目录（config.json / games/<hash>/cache.db / 备份 dump）。"""
    override = os.environ.get('RT_DATA_DIR')
    if override:
        return override
    if is_frozen():
        d = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')),
                         'RenPyTranslator')
    else:
        d = os.path.join(app_root(), 'data')
    os.makedirs(d, exist_ok=True)
    return d


def bundled(rel):
    """内置资源文件路径（相对 app_root）。"""
    return os.path.join(app_root(), rel)


def config_path():
    """全局配置文件路径（含 api_key 等跨游戏配置，不按游戏拆分）。"""
    return os.path.join(data_dir(), 'config.json')


def game_hash(game_dir):
    """按游戏目录计算稳定标识（MD5 前 12 位）。"""
    key = (game_dir or '').strip()
    if key:
        key = os.path.normcase(os.path.normpath(key))
    else:
        key = '_default'
    return hashlib.md5(key.encode('utf-8')).hexdigest()[:12]


def cache_dir(game_dir=None):
    """某游戏的缓存目录（<data_dir>/games/<hash>/）。"""
    return os.path.join(data_dir(), 'games', game_hash(game_dir))


def cache_path(game_dir=None):
    """某游戏的缓存库路径；旧版根 cache.db 首次自动迁移到新游戏路径。"""
    target = os.path.join(cache_dir(game_dir), 'cache.db')
    legacy = os.path.join(data_dir(), 'cache.db')
    if os.path.exists(legacy) and not os.path.exists(target):
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(legacy, target)
        except Exception:
            pass
    return target
