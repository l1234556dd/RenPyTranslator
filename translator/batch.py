# -*- coding: utf-8 -*-
"""批量预翻译（路线 B）：把提取出的文本全部翻译进本地 SQLite 缓存。

- CLI: python translator/batch.py <texts.json> [--port 24610] [--workers 8]
- 亦提供 run_batch() 供 GUI 调用（带进度回调）。

流程：读取提取的 JSON → 与游戏侧一致地保护占位符 → 多线程翻译（写缓存）→
完成后游戏内任意句子（含跳过模式）从缓存秒出中文。可断点续跑（本地已缓存的自动跳过）。

两条路径：
- free 引擎（支持 translate_batch）→ 本地批量路径：直接分批调用引擎多行批量翻译
  （Google 一次请求翻 20 句，实测 ~14x 提速），免 TCP 服务器。
- llm/mock 等引擎 → 原逐条 TCP 路径：多线程请求本地翻译服务器，行为完全不变。
"""

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from translator import cache as cache_mod       # noqa: E402
from translator import config as config_mod     # noqa: E402
from translator import engines                  # noqa: E402
from translator import paths                    # noqa: E402
from translator import server as server_mod     # noqa: E402
from translator import setup_safe_io            # noqa: E402

setup_safe_io()

# 与 gamehook 相同的占位符保护（保证缓存键一致）
# 注意：占位符格式必须与 gamehook/hook.py 的 _PLACEHOLDER_TOKEN_FMT（§§N§§）完全一致，
# 否则批量翻译写入缓存的 src 与游戏内 hook 查询的 src 不同 -> md5 不同 -> 缓存永远不命中。
_PLACEHOLDER_RE = re.compile(r'(\[[^\]]*\]|\{[^}]*\})')
_PLACEHOLDER_FMT = '\u00A7\u00A7%d\u00A7\u00A7'

# 本地批量路径的批次大小：Google 多行批量一次请求翻 20 句（实测 ~14x 提速）
BATCH_SIZE = 20


def protect(text):
    table = []

    def _sub(m):
        table.append(m.group(0))
        return _PLACEHOLDER_FMT % (len(table) - 1)

    return _PLACEHOLDER_RE.sub(_sub, text), table


def _ask(port, key):
    """向服务器请求翻译，返回译文或 None。"""
    s = socket.create_connection(('127.0.0.1', port), timeout=10)
    try:
        s.settimeout(180)
        req_id = hashlib.md5(key.encode('utf-8')).hexdigest()[:16]
        s.sendall((json.dumps({'type': 'req', 'id': req_id, 'text': key,
                               'prefetch': False}) + '\n').encode('utf-8'))
        buf = b''
        while b'\n' not in buf:
            chunk = s.recv(4096)
            if not chunk:
                return None
            buf += chunk
        resp = json.loads(buf.split(b'\n', 1)[0].decode('utf-8'))
        if resp.get('type') == 'res' and resp.get('id') == req_id:
            return resp.get('text') or None
    except Exception:
        return None
    finally:
        try:
            s.close()
        except Exception:
            pass
    return None


def load_texts(texts_json):
    """读取提取 JSON，返回 (原始条数, 去重后键列表)。"""
    with open(texts_json, 'r', encoding='utf-8') as f:
        data = json.load(f)
    raw = data.get('texts', [])
    keys = sorted({protect(t)[0] for t in raw})
    return len(raw), keys


def _run_batch_tcp(engine, c, todo, cfg, port, workers, progress_cb, result):
    """原逐条 TCP 路径（llm/mock 等引擎）：起 TranslatorServer，worker 逐条 _ask。

    行为与改造前完全一致（回归零风险）。
    """
    srv = server_mod.TranslatorServer(engine, c, host='127.0.0.1', port=port,
                                      src=cfg['src'], dst=cfg['dst'],
                                      max_concurrent=workers, log=lambda m: None)
    port = srv.start()

    queue = list(todo)
    lock = threading.Lock()
    t0 = time.time()

    def worker():
        while True:
            with lock:
                if not queue:
                    return
                key = queue.pop()
            try:
                r = _ask(port, key)
                ok = r is not None and r != key
            except Exception:
                ok = False
            with lock:
                result['done'] += 1
                if not ok:
                    result['fail'] += 1
                if progress_cb and (result['done'] % 50 == 0
                                    or result['done'] == len(todo)):
                    progress_cb(result['done'], len(todo), result['fail'], None)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    srv.stop()
    result['seconds'] = time.time() - t0


def _run_batch_local(engine, c, todo, cfg, workers, progress_cb, result):
    """本地批量路径（free 引擎）：直接多行批量翻译，免 TCP 服务器。

    - 分批调用 engine.translate_batch()，每批最多 BATCH_SIZE 条（一次请求翻多句）；
    - 批量请求异常 -> 回退逐条 engine.translate()（内部已有 Google + MyMemory 兜底）；
    - 与 TCP 路径一致：翻译前先应用术语表，缓存键仍用原文；Cache.set 自带线程锁，
      多 worker 并发写安全；每批回调一次进度。
    - free 引擎 last_usage=None，不记录 token 用量（保持）。
    """
    batches = [todo[i:i + BATCH_SIZE] for i in range(0, len(todo), BATCH_SIZE)]
    batch_queue = list(batches)
    lock = threading.Lock()
    t0 = time.time()

    def worker():
        while True:
            with lock:
                if not batch_queue:
                    return
                batch = batch_queue.pop()
            ok_count = 0
            try:
                translated = engine.translate_batch(
                    [c.apply_glossary(k) for k in batch], cfg['src'], cfg['dst'])
            except Exception:
                translated = None
            if translated is None:
                # 批量失败 -> 逐条兜底（translate 内部已有 Google + MyMemory 兜底）
                for k in batch:
                    try:
                        r = engine.translate(c.apply_glossary(k), cfg['src'], cfg['dst'])
                        if r and r != k:
                            c.set(k, r, cfg['dst'], engine.name)
                            ok_count += 1
                    except Exception:
                        pass
            else:
                for idx, k in enumerate(batch):
                    # 防御：译文列表短于输入时按缺失处理（不计成功）
                    r = translated[idx] if idx < len(translated) else ''
                    if r and r != k:
                        c.set(k, r, cfg['dst'], engine.name)
                        ok_count += 1
            with lock:
                result['done'] += len(batch)
                result['fail'] += len(batch) - ok_count
                if progress_cb:
                    progress_cb(result['done'], result['total'], result['fail'], None)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    result['seconds'] = time.time() - t0


def run_batch(texts_json, port=24610, workers=8, progress_cb=None, engine_name=None,
              api_key=None, max_items=None):
    """批量翻译：返回 {'total', 'done', 'fail', 'cached', 'seconds'}。

    progress_cb(done, total, fail, msg) 可选，进度回调。
    max_items: 可选，只翻译去重后前 N 条（keys 为 sorted 顺序，≈游戏脚本顺序，
        即“游戏开头部分”）；None 表示翻译全部。raw_count 始终按全量统计。

    引擎分派：
    - free 引擎（有 translate_batch）-> 本地批量路径，一次请求翻多句（~14x 提速）；
    - llm/mock 等 -> 原逐条 TCP 路径不变。port 参数仅 TCP 路径使用（保留兼容）。
    """
    raw_count, keys = load_texts(texts_json)
    if max_items is not None and max_items > 0:
        keys = keys[:max_items]
    cfg = config_mod.load()
    if engine_name:
        cfg['engine'] = engine_name
    if api_key:
        cfg['api_key'] = api_key

    c = cache_mod.Cache(paths.cache_path(cfg.get('game_dir')))
    c.seed_ui_glossary()
    # 迁移旧 \x00N\x00 占位符格式的缓存为 §§N§§（与游戏内 hook 一致），
    # 避免用户此前批量翻译的缓存因 key 不一致而永远无法被游戏内命中、被重复翻译浪费钱。
    migrated = c.migrate_old_placeholder_keys()
    if migrated and progress_cb:
        progress_cb(0, 0, 0, '已迁移旧缓存格式 %d 条' % migrated)
    todo = [k for k in keys if c.get(k, cfg['dst'], cfg['engine']) is None]
    cached = len(keys) - len(todo)
    if progress_cb:
        progress_cb(0, len(todo), 0, '本地已有 %d/%d' % (cached, len(keys)))

    result = {'total': len(todo), 'done': 0, 'fail': 0, 'cached': cached, 'seconds': 0.0}
    if not todo:
        c.close()
        return result

    engine = engines.make_engine(cfg)
    if hasattr(engine, 'translate_batch'):
        # free 引擎：本地批量路径（一次请求翻多句，免 TCP，大幅提速）
        _run_batch_local(engine, c, todo, cfg, workers, progress_cb, result)
    else:
        # llm/mock 等：保持原逐条 TCP 路径不变（回归零风险）
        _run_batch_tcp(engine, c, todo, cfg, port, workers, progress_cb, result)

    c.close()
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description='批量预翻译')
    p.add_argument('texts_json')
    p.add_argument('--port', type=int, default=24610)
    p.add_argument('--workers', type=int, default=8)
    a = p.parse_args(argv)

    raw_count, keys = load_texts(a.texts_json)
    print('提取文本: %d, 去重后键: %d' % (raw_count, len(keys)))

    def cb(done, total, fail, note):
        if note:
            print(note)
        elif done % 200 == 0 or done == total:
            print('进度 %d/%d 失败 %d' % (done, total, fail))

    r = run_batch(a.texts_json, port=a.port, workers=a.workers, progress_cb=cb)
    print('完成: 成功 %d, 失败 %d, 本地已有 %d, 用时 %.1f 秒' % (
        r['done'] - r['fail'], r['fail'], r['cached'], r['seconds']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
