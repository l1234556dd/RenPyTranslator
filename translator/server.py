# -*- coding: utf-8 -*-
"""TCP 翻译服务器（多线程）。

协议（JSON 行，\\n 结尾）：
  游戏 -> 工具: {"type":"req","id":"<hash16>","text":"原文","prefetch":false}
  工具 -> 游戏: {"type":"res","id":"<hash16>","text":"译文"}   （text 为 null 表示失败）

- 缓存命中直接回；未命中调引擎（信号量限流 + 同文去重），成功写缓存。
- prefetch=true 的请求后台处理，不阻塞游戏。
"""

import json
import socket
import threading
import time

from translator import setup_safe_io

setup_safe_io()


class TranslatorServer(object):
    def __init__(self, engine, cache, host='127.0.0.1', port=24567,
                 src='auto', dst='zh', max_concurrent=2, log=None, verbose=True):
        self.engine = engine
        self.cache = cache
        self.host = host
        self.port = port
        self.src = src
        self.dst = dst
        self.verbose = verbose      # True 时记录逐句翻译；False 只记录连接/错误
        self._sem = threading.Semaphore(max_concurrent)
        self._inflight = {}          # hash -> threading.Event
        self._inflight_lock = threading.Lock()
        self._log = log or (lambda msg: print(msg))
        self._sock = None
        self._threads = []
        self._running = False

    # ---------------- 生命周期 ----------------

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(8)
        s.settimeout(0.5)
        self._sock = s
        self.port = s.getsockname()[1]
        self._running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self._log('翻译服务器监听中: %s:%d (引擎=%s)' % (self.host, self.port, self.engine.name))
        return self.port

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except Exception:
            pass

    def _accept_loop(self):
        while self._running:
            try:
                conn, addr = self._sock.accept()
            except socket.timeout:
                continue
            except Exception:
                if self._running:
                    continue
                break
            t = threading.Thread(target=self._handle, args=(conn, addr), daemon=True)
            t.start()
            self._threads.append(t)

    # ---------------- 请求处理 ----------------

    def _handle(self, conn, addr):
        if self.verbose:
            self._log('游戏侧已连接: %s' % (addr,))
        buf = b''
        try:
            while True:
                while b'\n' not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    buf += chunk
                line, buf = buf.split(b'\n', 1)
                req = json.loads(line.decode('utf-8'))
                if req.get('type') != 'req':
                    continue
                self._on_request(conn, req)
        except Exception as e:
            if self.verbose:
                self._log('连接结束 %s: %r' % (addr, e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _on_request(self, conn, req):
        text = req.get('text') or ''
        req_id = req.get('id') or ''
        prefetch = bool(req.get('prefetch'))
        if prefetch:
            threading.Thread(target=self._translate_and_cache, args=(text,), daemon=True).start()
            return
        try:
            result = self._translate_cached(text)
            if self.verbose:
                self._log('[翻译] %r -> %r' % (text[:60], (result or '')[:60]))
            self._send(conn, req_id, result)
        except Exception as e:
            self._log('翻译失败: %r' % (e,))
            self._send(conn, req_id, None)

    def _translate_cached(self, text):
        """缓存命中直接回；未命中（带同文去重）调引擎并写缓存。"""
        hit = self.cache.get(text, self.dst, self.engine.name)
        if hit is not None:
            return hit
        key = self.cache._h(text)
        # 同文去重：同一句并发请求共享一次引擎调用
        with self._inflight_lock:
            ev = self._inflight.get(key)
            if ev is None:
                ev = threading.Event()
                self._inflight[key] = ev
                mine = True
            else:
                mine = False
        try:
            if not mine:
                ev.wait(timeout=60)
                return self.cache.get(text, self.dst, self.engine.name)
            return self._translate_and_cache(text)
        finally:
            if mine:
                with self._inflight_lock:
                    self._inflight.pop(key, None)
                ev.set()

    def _translate_and_cache(self, text):
        src = self.cache.apply_glossary(text)
        with self._sem:
            translated = self.engine.translate(src, self.src, self.dst)
        if translated:
            self.cache.set(text, translated, self.dst, self.engine.name)
        self._record_usage()
        return translated

    def _record_usage(self):
        """翻译成功后记录本次 token 消耗（用于费用统计）。"""
        usage = getattr(self.engine, 'last_usage', None)
        if not usage:
            return
        try:
            model = getattr(self.engine, 'model', '') or self.engine.name
            self.cache.record_usage(
                self.engine.name, model,
                usage.get('prompt_tokens', 0),
                usage.get('completion_tokens', 0))
        except Exception:
            pass

    def _send(self, conn, req_id, text):
        conn.sendall((json.dumps({'type': 'res', 'id': req_id, 'text': text},
                                 ensure_ascii=False) + '\n').encode('utf-8'))
