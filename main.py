# -*- coding: utf-8 -*-
"""renpy-translator 主入口（CLI）。

用法:
  python main.py --game <游戏目录>                # 启动服务器 -> 启动游戏 -> 注入 -> 服务
  python main.py --game <游戏目录> --engine llm   # 用 LLM 引擎（需配置 api_key）
  python main.py --attach <pid>                   # 附加到已运行的游戏进程
  python main.py --serve-only                     # 只起服务器（供手动测试）

配置: 首次运行写入 config.json；也可用环境变量 RT_API_KEY/RT_MODEL/RT_ENGINE 覆盖。
"""

import argparse
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO_ROOT)

from translator import cache as cache_mod       # noqa: E402
from translator import config as config_mod     # noqa: E402
from translator import engines                  # noqa: E402
from translator import launcher                 # noqa: E402
from translator import paths                    # noqa: E402
from translator import server as server_mod     # noqa: E402
from translator import setup_safe_io            # noqa: E402

setup_safe_io()


def main(argv=None):
    p = argparse.ArgumentParser(description="Ren'Py 实时翻译工具")
    p.add_argument('--game', help='游戏根目录（含 exe）')
    p.add_argument('--attach', type=int, help='附加到已运行进程的 PID（跳过启动）')
    p.add_argument('--serve-only', action='store_true', help='只启动翻译服务器')
    p.add_argument('--engine', choices=['mock', 'llm'], help='翻译引擎（默认读配置）')
    p.add_argument('--port', type=int, help='服务器端口')
    a = p.parse_args(argv)

    cfg = config_mod.load()
    if a.engine:
        cfg['engine'] = a.engine
    if a.port:
        cfg['port'] = a.port
    if a.game:
        cfg['game_dir'] = a.game
    config_mod.save(cfg)

    if not cfg.get('game_dir') and not a.attach and not a.serve_only:
        p.error('请提供 --game 游戏目录，或 --attach PID，或 --serve-only')

    engine = engines.make_engine(cfg)
    c = cache_mod.Cache(paths.cache_path(cfg.get('game_dir')))
    c.seed_ui_glossary()
    srv = server_mod.TranslatorServer(engine, c, host=cfg['host'], port=cfg['port'],
                                      src=cfg['src'], dst=cfg['dst'])
    port = srv.start()

    try:
        if a.serve_only:
            print('仅服务模式，Ctrl+C 退出')
            while True:
                time.sleep(1)
        elif a.attach:
            ok, msg = launcher.inject_into(a.attach)
            print(msg)
            if not ok:
                print('注入失败，服务器仍保持运行')
            while True:
                time.sleep(1)
        else:
            exe = cfg.get('game_exe') or None
            proc = launcher.launch_game(cfg['game_dir'], cfg['host'], port, exe=exe)
            print('游戏进程 pid=%d，等待内嵌 Python 就绪后注入...' % proc.pid)
            ok, msg = launcher.inject_into(proc.pid)
            print(msg)
            if not ok:
                print('注入失败，服务器仍保持运行；可手动重试注入')
            print('游戏运行中。关闭游戏后本工具自动退出。')
            while proc.poll() is None:
                time.sleep(1)
            print('游戏已退出')
    except KeyboardInterrupt:
        print('收到中断，退出')
    finally:
        srv.stop()
        c.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
