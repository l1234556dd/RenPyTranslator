# -*- coding: utf-8 -*-
# M0 验证用测试脚本（可复用）：随游戏启动，后台线程调用 hook 函数，
# 验证 config.replace_text / say_callback 挂钩与 TCP 翻译链路，
# 结果写入游戏根目录 zz_mt_test_log.txt。
# 用法：与 make_rpy_patch.py 生成的 zz_mt_hook.rpy 一起放进游戏 game/ 目录，
#       先启动 tools/test_server.py，再启动游戏。
# 验证完删除 zz_mt_hook.rpy 与 zz_mt_test.rpy。

init 999 python:

    import os
    import time
    import threading

    def zz_mt_run_test():
        time.sleep(10)  # 等待 zz_mt_hook 的安装线程完成挂钩
        log = []
        sample = 'Hello [hero], welcome to the Summer!'

        # 1) 直接调用 hook 替换函数（store 命名空间里应已定义）
        try:
            r = replace_text(sample)
            log.append('DIRECT replace_text -> %r' % (r,))
        except Exception as e:
            log.append('DIRECT replace_text ERROR: %r' % (e,))

        # 2) 检查 config 挂钩状态
        try:
            log.append('config.replace_text hooked: %r' % callable(getattr(renpy.config, 'replace_text', None)))
            log.append('config.say_callback hooked: %r' % callable(getattr(renpy.config, 'say_callback', None)))
        except Exception as e:
            log.append('hook check ERROR: %r' % (e,))

        # 3) 第二次调用应命中本地缓存（不再走网络）
        try:
            r2 = replace_text(sample)
            log.append('SECOND call (cache) -> %r' % (r2,))
        except Exception as e:
            log.append('SECOND call ERROR: %r' % (e,))

        # 3.5) 读取 hook 安装诊断
        try:
            log.append('install_log: %r' % (globals().get('_install_log', []),))
        except Exception as e:
            log.append('install_log ERROR: %r' % (e,))

        # 4) 写日志到游戏根目录
        try:
            path = os.path.join(renpy.config.basedir, 'zz_mt_test_log.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(log))
            log.append('LOG written to %s' % path)
            print('zz_mt_test done:', ' / '.join(log))
        except Exception as e:
            print('zz_mt_test LOG WRITE FAILED:', repr(e))

    threading.Thread(target=zz_mt_run_test, daemon=True).start()
