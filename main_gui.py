# -*- coding: utf-8 -*-
"""GUI 启动入口（PyInstaller 打包目标）。

用法:
  RenPyTranslator.exe                # 正常启动 GUI
  RenPyTranslator.exe --selftest <out.txt>   # 自检并退出（诊断用）
"""

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _selftest(out_path):
    from translator import batch as batch_mod       # noqa
    from translator import cache as cache_mod       # noqa
    from translator import config as config_mod     # noqa
    from translator import engines                  # noqa
    from translator import launcher                 # noqa
    from translator import paths                    # noqa
    from translator import server as server_mod     # noqa

    lines = []
    def log(m):
        lines.append(str(m))
        print(m)

    log('frozen=%s app_root=%s' % (paths.is_frozen(), paths.app_root()))
    for rel in ('gamehook/hook.py', 'injector/injector.py',
                'tools/zz_dump_texts.py'):
        log('bundled %s: %s' % (rel, os.path.exists(paths.bundled(rel))))

    cfg = config_mod.load()
    log('config: game_dir=%s engine=%s api_key=%s model=%s' % (
        cfg.get('game_dir'), cfg.get('engine'),
        'set' if cfg.get('api_key') else 'EMPTY', cfg.get('model')))
    log('config_path=%s' % config_mod.config_path())

    try:
        c = cache_mod.Cache(paths.cache_path(cfg.get('game_dir')))
        st = c.stats()
        log('cache: translations=%d glossary=%d' % (
            st.get('translations', 0), st.get('glossary', 0)))
        c.close()
    except Exception as e:
        log('cache ERROR: %r' % (e,))

    try:
        eng = engines.make_engine(cfg)
        log('engine: %s (mock 结果: %r)' % (
            eng.name, eng.translate('test') if eng.name == 'mock' else 'N/A'))
    except Exception as e:
        log('engine ERROR: %r' % (e,))

    try:
        exe = launcher.find_game_exe(cfg.get('game_dir'))
        log('find_game_exe: %s' % exe)
    except Exception as e:
        log('find_game_exe ERROR: %r' % (e,))

    try:
        import socket
        s = socket.create_connection(('127.0.0.1', 1), timeout=1)
        s.close()
    except Exception:
        pass  # 端口 1 必然拒绝，仅验证网络可用
    log('selftest done')

    try:
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    except Exception as e:
        log('write selftest output ERROR: %r' % (e,))
    return 0


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == '--selftest':
        sys.exit(_selftest(sys.argv[2]))
    from translator.app import main  # noqa
    main()
