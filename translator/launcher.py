# -*- coding: utf-8 -*-
"""游戏启动与 Hook 注入。

流程：定位 exe -> 带环境变量启动 -> 等待进程内嵌 Python 就绪 -> 注入 hook.py。
也可附加到已运行进程（attach）。
"""

import glob
import importlib.util
import os
import subprocess
import sys
import time

from translator.paths import bundled

_HOOK_PATH = bundled('gamehook/hook.py')
_INJECTOR_PATH = bundled('injector/injector.py')
_DUMP_PATH = bundled('tools/zz_dump_texts.py')


def _load_injector():
    spec = importlib.util.spec_from_file_location('rt_injector', _INJECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_NOISE_EXE = ('renpy.exe', 'python.exe', 'pythonw.exe', 'setup.exe',
              'unins000.exe', 'uninstall.exe', 'uninst.exe', 'installer.exe')


def _candidate_exes(d):
    out = []
    try:
        for f in glob.glob(os.path.join(d, '*.exe')):
            b = os.path.basename(f).lower()
            if b in _NOISE_EXE or b.startswith('vc_redist') or b.startswith('dotnet'):
                continue
            out.append(f)
    except Exception:
        pass
    return out


def _find_exe_in_dir(game_dir):
    """在单个目录里找候选 exe（不递归）。"""
    import re as _re
    if not game_dir or not os.path.isdir(game_dir):
        return None
    exes = _candidate_exes(game_dir)
    if not exes:
        return None
    if len(exes) == 1:
        return exes[0]

    # 目录名匹配优先（忽略版本号/分隔符差异）
    name_hint = os.path.basename(game_dir.rstrip('\\/')).lower()
    name_hint = _re.sub(r'[\s_\-\.]*\d[\d\.v]*$', '', name_hint)
    hints = [h for h in (name_hint,
                         name_hint.split('_')[0],
                         name_hint.split(' ')[0]) if h]
    for h in hints:
        for e in exes:
            if os.path.basename(e).lower().startswith(h):
                return e
    return exes[0]


def find_game_exe(game_dir):
    """找游戏启动 exe。

    策略：先扫目录顶层；没有再递归子目录（深度 2）；仍没有则逐级向上一级
    目录找（用户可能选到了游戏根目录下的子文件夹，如 renpy/）。
    排除安装器/运行时类 exe；多候选时优先与目录名匹配。
    """
    if not game_dir or not os.path.isdir(game_dir):
        return None

    # 1) 当前目录 + 子目录（深度 2）
    exe = _find_exe_in_dir(game_dir)
    if exe:
        return exe
    base = game_dir.rstrip('\\/')
    seen = set()
    for root, dirs, files in os.walk(game_dir):
        depth = root[len(base):].count(os.sep)
        if depth >= 3:
            dirs[:] = []
            continue
        e = _find_exe_in_dir(root)
        if e and e not in seen:
            seen.add(e)
            if exe is None:
                exe = e
    if exe:
        return exe

    # 2) 逐级向上找（用户可能选到了子目录）
    d = os.path.dirname(game_dir.rstrip('\\/'))
    while d and d != os.path.dirname(d):
        e = _find_exe_in_dir(d)
        if e:
            return e
        d = os.path.dirname(d)
    return None


def find_process_by_name(name):
    inj = _load_injector()
    return inj.find_process(name)


def launch_game(game_dir, host, port, exe=None):
    """启动游戏，返回 subprocess.Popen 对象。"""
    exe = exe or find_game_exe(game_dir)
    if not exe:
        raise RuntimeError('未找到游戏 exe: %s' % game_dir)
    env = dict(os.environ)
    env['RENPY_MT_HOST'] = host
    env['RENPY_MT_PORT'] = str(port)
    print('启动游戏: %s' % exe)
    return subprocess.Popen([exe], cwd=game_dir, env=env)


def inject_into(pid, hook_path=_HOOK_PATH, max_retries=30, retry_interval=2.0):
    """等待进程内嵌 Python DLL 就绪后注入 hook。返回 (ok, message)。"""
    inj = _load_injector()
    with open(hook_path, 'r', encoding='utf-8') as f:
        script = f.read()
    last = (False, '未尝试')
    for i in range(max_retries):
        py = inj.find_python_dll(pid)
        if not py:
            time.sleep(retry_interval)
            continue
        last = inj.inject(pid, script)
        if last[0]:
            return last
        time.sleep(retry_interval)
    return last


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('game_dir')
    p.add_argument('--port', type=int, default=24567)
    a = p.parse_args()
    exe = find_game_exe(a.game_dir)
    print('exe:', exe)
    proc = launch_game(a.game_dir, '127.0.0.1', a.port, exe)
    print('launched pid:', proc.pid)
    time.sleep(20)
    ok, msg = inject_into(proc.pid)
    print(msg)
