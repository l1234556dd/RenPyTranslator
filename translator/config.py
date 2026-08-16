# -*- coding: utf-8 -*-
"""配置管理：JSON 配置文件读写。

配置文件位置：数据目录（开发模式 <仓库>/data/config.json；
冻结模式 %APPDATA%/RenPyTranslator/config.json）。
首次运行时若发现旧版仓库根 config.json，自动迁移。
亦可用环境变量覆盖：RT_ENGINE / RT_BASE_URL / RT_API_KEY / RT_MODEL / RT_DST / RT_PORT。
"""

import json
import os
import shutil

from translator.paths import config_path

DEFAULTS = {
    'game_dir': '',
    'game_exe': '',              # 手动指定的启动程序（可选，覆盖自动检测）
    'engine': 'llm',             # llm(AI 高质量) | free(免费快速) | mock(离线联调)
    'base_url': 'https://api.deepseek.com',
    'api_key': '',
    'model': 'deepseek-chat',
    'src': 'auto',
    'dst': 'zh',
    'port': 24567,
    'host': '127.0.0.1',
    'prompt_price': 1.0,        # 输入 tokens 单价（元 / 百万 token）
    'completion_price': 2.0,    # 输出 tokens 单价（元 / 百万 token）
}

# 旧版（仓库根目录）配置文件，首次运行时迁移
_LEGACY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'config.json')


def _migrate_legacy():
    try:
        if not os.path.exists(config_path()) and os.path.exists(_LEGACY_PATH):
            os.makedirs(os.path.dirname(config_path()), exist_ok=True)
            shutil.copyfile(_LEGACY_PATH, config_path())
    except Exception:
        pass


def load():
    _migrate_legacy()
    cfg = dict(DEFAULTS)
    try:
        if os.path.exists(config_path()):
            with open(config_path(), 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
    except Exception:
        pass
    # 环境变量覆盖
    env_map = {
        'RT_ENGINE': 'engine', 'RT_BASE_URL': 'base_url', 'RT_API_KEY': 'api_key',
        'RT_MODEL': 'model', 'RT_DST': 'dst', 'RT_PORT': 'port', 'RT_HOST': 'host',
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            cfg[key] = os.environ.get(env)
    if isinstance(cfg.get('port'), str) and cfg['port'].isdigit():
        cfg['port'] = int(cfg['port'])
    return cfg


def save(cfg):
    os.makedirs(os.path.dirname(config_path()), exist_ok=True)
    with open(config_path(), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
