# -*- coding: utf-8 -*-
"""
gamehook/hook.py — Ren'Py 游戏进程内的 Hook 脚本（路线 A 核心）

职责：
  1. 挂钩 config.replace_text：每个文本块显示前拿到原文，
     查本地缓存 -> 未命中则通过 TCP 请求翻译工具 -> 返回译文。
  2. 挂钩 config.say_callback：对话事件时后台预取下一句，预热缓存。
  3. 动态插值 [var] 占位符保护，防止变量名被翻译破坏。
  4. 工具进程断开时自动降级显示原文，绝不卡死游戏。

兼容性：纯 Python，兼容 Ren'Py 7（Python 2.7）与 Ren'Py 8（Python 3.x）。
加载方式：
  A. 临时补丁（M0 验证）：用 tools/make_rpy_patch.py 把本文件包进
     `init 999 python:` 块，生成 .rpy 放进游戏 game/ 目录。
  B. 正式注入：由 injector/injector.py 通过 PyRun_SimpleString 执行本文件。
"""

import os
import sys
import json
import time
import socket
import hashlib
import threading
import re as _re

# 执行标记（诊断）：证明脚本本体已在游戏进程内运行（写入 %TEMP%）。
# 用于区分"脚本没执行"（注入失败）与"脚本执行了但安装失败"。
try:
    _marker = os.path.join(os.environ.get('TEMP', '.'), 'zz_mt_hook_executed.txt')
    with open(_marker, 'w', encoding='utf-8') as f:
        f.write('executed %s' % time.time())
except Exception:
    pass

HOOK_HOST = os.environ.get('RENPY_MT_HOST', '127.0.0.1')
HOOK_PORT = int(os.environ.get('RENPY_MT_PORT', '24567'))
# 超时只影响后台翻译线程（replace_text 永不阻塞游戏线程），给足 LLM 时间
TIMEOUT = float(os.environ.get('RENPY_MT_TIMEOUT', '15.0'))
RECONNECT_COOLDOWN = 5.0  # 连接失败后的重试冷却（秒）

_cache = {}
_cache_lock = threading.Lock()
_conn = None
_conn_lock = threading.Lock()
_last_fail = [0.0]

# 请求串行锁：多个后台线程共用一条连接，收发必须互斥，避免响应错配
_req_lock = threading.Lock()

# 正在后台翻译的 key 集合与待处理计数（防重复请求、防线程爆炸）
_inflight = set()
_inflight_lock = threading.Lock()
_pending = [0]
MAX_PENDING = 6

# 已翻译文本集合：界面重绘时 replace_text 会拿到"已翻译的文本"，
# 直接放行，避免二次翻译（【译】【译】Start 这类问题）。
_known_translations = set()
_known_lock = threading.Lock()


def _mark_known(text):
    if text:
        with _known_lock:
            _known_translations.add(text)


def _is_known(text):
    with _known_lock:
        return text in _known_translations

_PLACEHOLDER = _re.compile(r'(\[[^\]]*\]|\{[^}]*\})')
# hook 自己的占位符格式：双 §（section sign）夹数字。LLM 几乎不会翻译或删除
# 多 § 包裹的数字串（[[0]] 这种纯数字容易被 LLM 当装饰删掉）。
# engines 的 _PLACEHOLDER_RE 只匹配 [xxx]/{xxx}，_NULL_RE 只匹配 \x00N\x00，
# 都不会动 §§N§§，原样透传给 LLM。hook _restore 用 _PLACEHOLDER_TOKEN_RE 还原。
_PLACEHOLDER_TOKEN_FMT = '\u00A7\u00A7%d\u00A7\u00A7'
_PLACEHOLDER_TOKEN_RE = _re.compile(r'\u00A7\u00A7(\d+)\u00A7\u00A7')

# 中文字体候选（Windows 系统字体，按文件名匹配 pygame.sysfont）：
# Ren'Py 8 的 load_face 在游戏目录找不到字体时，会搜索系统字体。
# 优先 simhei.ttf（.ttf 单文件，最稳）；msyh.ttc 是 TrueType Collection，
# 某些 Ren'Py 版本 harfbuzz 加载 ttc 的 face index 会出问题 → 中文口口口。
_FONT_CANDIDATES = [
    'simhei.ttf',  # 黑体（.ttf 单文件，首选）
    'msyh.ttc',    # 微软雅黑（Windows 11 默认）
    'msyhbd.ttc',  # 微软雅黑粗体
    'simsun.ttc',  # 宋体
    'Deng.ttf',    # 等线
]


def _find_cjk_font():
    """找一个本机存在的中文字体，复制到游戏目录 fonts/ 并返回文件名。

    关键（借鉴 MTool）：字体文件必须放在游戏目录 game/fonts/，让
    renpy.loader.load(fn, directory='fonts') 直接加载（RWopsIO），
    而不是走 pygame.sysfont 搜索系统字体。sysfont 加载的系统 .ttf +
    harfbuzz 会渲染成 .notdef（口口口），游戏目录文件 + freetype 正常。
    返回不带路径的文件名；找不到返回 None。
    """
    try:
        if os.name != 'nt':
            return None
        fonts_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
        for name in _FONT_CANDIDATES:
            src = os.path.join(fonts_dir, name)
            if not os.path.exists(src):
                continue
            # 复制到游戏目录 game/fonts/（一次性，已存在则跳过）
            try:
                import renpy
                gamedir = getattr(renpy.config, 'gamedir', None)
                if gamedir:
                    dst_dir = os.path.join(gamedir, 'fonts')
                    if not os.path.isdir(dst_dir):
                        os.makedirs(dst_dir)
                    dst = os.path.join(dst_dir, name)
                    if not os.path.exists(dst):
                        import shutil
                        shutil.copyfile(src, dst)
            except Exception:
                pass
            return name
    except Exception:
        pass
    return None


_FONT_FILE_RE = _re.compile(r'[\w/\\.-]+\.(?:ttf|otf|ttc)', _re.I)


def _discover_game_fonts():
    """扫描游戏 .rpy 源码中的字体文件引用 + Ren'Py 常见默认字体。

    返回字体名字集合（含路径原名与 basename，兼容 get_font 收到的两种形式）。
    """
    fonts = set()
    for f in ('DejaVuSans.ttf', 'DejaVuSerif.ttf', 'DejaVuSans-Bold.ttf',
              'DejaVuSerif-Bold.ttf', 'DejaVuSans-Oblique.ttf',
              'LiberationSans-Regular.ttf', 'LiberationSans-Bold.ttf',
              'LiberationSerif-Regular.ttf', 'LiberationSerif-Bold.ttf'):
        fonts.add(f)
    try:
        import renpy
        roots = []
        for attr in ('basedir', 'gamedir'):
            b = getattr(renpy.config, attr, None)
            if b:
                roots.append(b)
                roots.append(os.path.join(b, 'game'))
        roots.append(os.getcwd())
        seen = set()
        for root in roots:
            if not root or not os.path.isdir(root) or root in seen:
                continue
            seen.add(root)
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.lower().endswith(('.rpy', '.rpym')):
                        try:
                            with open(os.path.join(dirpath, fn), 'r',
                                      encoding='utf-8', errors='ignore') as f:
                                txt = f.read()
                        except Exception:
                            continue
                        for m in _FONT_FILE_RE.findall(txt):
                            m = m.replace('\\', '/')
                            fonts.add(m)
                            fonts.add(os.path.basename(m))
    except Exception:
        pass
    return fonts


def _clear_text_caches():
    """清空字体/文本布局缓存，让下一次渲染重新走 replace_text（否则菜单会命中旧英文布局）。"""
    try:
        import renpy.text.font as _tf
        _tf.face_cache.clear()
        _tf.font_cache.clear()
    except Exception:
        pass
    try:
        import renpy.text.text as _tt
        if hasattr(_tt, 'layout_cache_clear'):
            _tt.layout_cache_clear()
    except Exception:
        pass


def _kill_text_recursive(disp):
    if disp is None:
        return
    if type(disp).__name__ == 'Text':
        try:
            disp.kill_layout()
            disp.dirty = True
            # 关键：设 tokenized=False 强制重新 tokenize，否则 update() 里
            # `if not self.tokenized` 为 False，直接复用旧 tokens（英文），
            # 不会重新走 apply_custom_tags -> replace_text（界面卡英文）。
            disp.tokenized = False
        except Exception:
            pass
    children = getattr(disp, 'children', None)
    if children:
        for child in children:
            _kill_text_recursive(child)


def _kill_all_text_layout():
    """遍历所有 layer 的 Text，kill_layout + dirty=True。

    restart_interaction 不会强制 Text 重新 layout（Text 复用，dirty=False）。
    必须手动 kill_layout + 设 dirty=True，render 时才会 update → 重新用 freetype 布局。
    """
    try:
        import renpy.display.core as _core
        sl = _core.scene_lists
        if sl is None:
            return
        for attr in ('overlay', 'transient', 'screens', 'master'):
            layer = getattr(sl, attr, None)
            if layer is not None:
                children = getattr(layer, 'children', None)
                if children:
                    for child in children:
                        _kill_text_recursive(child)
    except Exception:
        pass


def _force_font_relayout():
    """清空字体/文本缓存 + kill 所有 Text layout，然后主线程重绘一次。

    只在 hook 安装时调用一次：把已渲染的旧文本（英文/口口）刷新成
    新字体 + 译文。之后新文本自然用新字体，无需反复 kill/restart
    （反复全量重绘会导致游戏点击卡顿）。
    """
    _clear_text_caches()
    _kill_all_text_layout()

    def _restart():
        try:
            import renpy
            renpy.restart_interaction()
        except Exception:
            pass

    def _later():
        time.sleep(6)
        try:
            import renpy
            renpy.exports.invoke_in_main_thread(_restart)
        except Exception:
            pass

    threading.Thread(target=_later, daemon=True).start()


class _CJKFontReplaceDict(dict):
    """对任何字体名都返回中文字体（排除 emoji/符号字体）。

    比扫描 .rpy + gui 变量可靠得多：游戏可能用不带 .ttf 后缀的名字、
    自定义字体名、或字体名在 .rpa 里（扫描不到）。这个类对 get_font
    里的 font_replacement_map.get((fn, bold, italics)) 一律命中。
    """
    _EXCLUDE = ('twemoji', 'emoji', 'symbola', 'notoemoji', 'opensansemoji')

    def __init__(self, cjk):
        super(_CJKFontReplaceDict, self).__init__()
        self._cjk = cjk

    def get(self, key, default=None):
        if isinstance(key, tuple) and len(key) == 3 and isinstance(key[0], str):
            fn_lower = key[0].lower()
            if not any(ex in fn_lower for ex in self._EXCLUDE):
                return (self._cjk, key[1], key[2])
        return default


def install_fonts(relayout=True):
    """把游戏使用的全部字体替换为系统中文字体（修复中文显示成口口口）。

    用 _CJKFontReplaceDict 对任何字体名都返回中文字体（排除 emoji），
    不再依赖扫描 .rpy/gui 变量（之前 import gui 失败导致漏掉字体名 → 口口口）。
    返回选中的中文字体名；未找到系统字体或出错返回 None。
    """
    cjk = _find_cjk_font()
    if not cjk:
        return None
    try:
        import renpy
        renpy.config.font_replacement_map = _CJKFontReplaceDict(cjk)
        _force_freetype_shaper()
        if relayout:
            _force_font_relayout()
        return cjk
    except Exception:
        return None


def _force_freetype_shaper():
    """强制 get_font 使用 freetype shaper。

    Ren'Py 8 默认用 harfbuzz shaper，harfbuzz 加载系统 .ttf/.ttc 中文字体时
    会产出 .notdef 字形（中文渲染成口口口），改 style.shaper 又因 style 只读
    或 Text 缓存而不生效。直接在 get_font 层强制 shaper='freetype' 最可靠。
    """
    try:
        import renpy.text.font as _tf
        _orig_get_font = _tf.get_font

        # 用 *args 透传，只替换 shaper（第 10 个参数，index 9），
        # 兼容 Ren'Py 8.3（get_font 12 参数，无 features）与 8.4（13 参数，含 features）。
        def _patched(*args, **kwargs):
            args = list(args)
            if len(args) >= 10:
                args[9] = 'freetype'  # shaper 位置
            return _orig_get_font(*args, **kwargs)

        _tf.get_font = _patched
    except Exception:
        pass


# ---------------------------------------------------------------- TCP 通信

class _Conn(object):
    """持有一个 socket 与跨调用读缓冲（py2/3 兼容的简单封装）。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b''

    def send(self, data):
        self.sock.sendall(data)

    def recv_line(self):
        while b'\n' not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
        line, self.buf = self.buf.split(b'\n', 1)
        return line.decode('utf-8')


def connect():
    s = socket.create_connection((HOOK_HOST, HOOK_PORT), timeout=3)
    s.settimeout(TIMEOUT)
    return _Conn(s)


def get_conn():
    global _conn
    with _conn_lock:
        if _conn is not None:
            return _conn
        now = time.time()
        if now - _last_fail[0] < RECONNECT_COOLDOWN:
            return None
        try:
            _conn = connect()
            _last_fail[0] = 0.0
            return _conn
        except Exception:
            _last_fail[0] = now
            return None


def _drop_conn(c):
    """关闭并清除连接（幂等）。任何失败路径都必须调用，避免缓存失效 socket。"""
    try:
        c.sock.close()
    except Exception:
        pass
    with _conn_lock:
        if _conn is c:
            _conn = None
    _last_fail[0] = time.time()


def _read_response(c, req_id, deadline):
    """读取响应直到 id 匹配或超时。

    跳过陈旧响应（被放弃的快速查询的迟到回复），避免连接污染；
    返回 dict=匹配响应, 'closed'=连接关闭, None=超时。
    """
    while time.time() < deadline:
        try:
            line = c.recv_line()
        except Exception:
            return None
        if line is None:
            return 'closed'
        try:
            resp = json.loads(line)
        except Exception:
            continue
        if resp.get('type') == 'res' and resp.get('id') == req_id:
            return resp
    return None


def request_translation(text, prefetch=False):
    """向工具请求翻译，返回译文；失败返回 None。仅在后台线程调用。"""
    c = get_conn()
    if c is None:
        return None
    req_id = hashlib.md5(text.encode('utf-8')).hexdigest()[:16]
    with _req_lock:
        try:
            c.send(json.dumps({
                'type': 'req',
                'id': req_id,
                'text': text,
                'prefetch': prefetch,
            }).encode('utf-8') + b'\n')
        except Exception:
            _drop_conn(c)
            return None
        resp = _read_response(c, req_id, time.time() + TIMEOUT)
    if resp == 'closed':
        _drop_conn(c)
        return None
    if resp:
        return resp.get('text') or None
    return None  # 超时：连接可能仍健康，不丢；下次读取会跳过陈旧响应


# ---------------------------------------------------------------- 缓存

def cache_key(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def cache_get(text):
    with _cache_lock:
        return _cache.get(cache_key(text))


def cache_set(text, translated):
    with _cache_lock:
        _cache[cache_key(text)] = translated


# ---------------------------------------------------------------- 占位符保护

def _protect(text):
    """把 [var] / {var} 插值替换为 §§N§§ 占位符，返回 (保护后文本, 还原表)。"""
    table = []

    def _sub(m):
        table.append(m.group(0))
        return _PLACEHOLDER_TOKEN_FMT % (len(table) - 1)

    return _PLACEHOLDER.sub(_sub, text), table


def _restore(text, table):
    def _sub(m):
        idx = int(m.group(1))
        if 0 <= idx < len(table):
            return table[idx]
        return m.group(0)

    return _PLACEHOLDER_TOKEN_RE.sub(_sub, text)


# ---------------------------------------------------------------- Hook 实现

def _quick_lookup(key_text):
    """同步快速查询：服务器本地 SQLite 命中时 <10ms 返回译文。

    仅用于 replace_text 的显示路径（最多阻塞 0.3s，几乎无感）；
    需要 LLM 的句子会超时返回 None。超时不丢连接——迟到的响应
    由 _read_response 在后续请求时跳过，避免重连风暴与日志刷屏。
    """
    c = get_conn()
    if c is None:
        return None
    req_id = hashlib.md5(key_text.encode('utf-8')).hexdigest()[:16]
    with _req_lock:
        try:
            c.sock.settimeout(0.3)
            try:
                c.send(json.dumps({
                    'type': 'req',
                    'id': req_id,
                    'text': key_text,
                    'prefetch': False,
                }).encode('utf-8') + b'\n')
                resp = _read_response(c, req_id, time.time() + 0.3)
            finally:
                c.sock.settimeout(TIMEOUT)
        except Exception:
            _drop_conn(c)
            return None
    if resp == 'closed':
        _drop_conn(c)
        return None
    if resp:
        return resp.get('text') or None
    return None


# 后台翻译完成后的"合并窗口"刷新控制。
# _refresh_timer_active：是否已有一个延迟刷新线程在跑（窗口期）。
# _want_refresh：窗口期内是否出现过刷新请求（窗口结束必然执行一次刷新）。
_refresh_lock = threading.Lock()
_refresh_timer_active = [False]
_want_refresh = [False]


def _refresh_after_translate():
    """后台翻译完成后，合并窗口内延迟刷新屏幕（保证不漏刷）。

    窗口期（1s）内多次调用只安排一次延迟刷新；窗口结束必然执行一次，
    把窗口内所有已翻完的句子一次性刷出来。避免旧实现 1s 节流把后续
    触发直接丢弃 → 连续多句翻完时屏幕一直显示英文，用户只能回滚
    （重新渲染）才看到中文。

    只 restart_interaction 不够——已渲染的 Text 复用（dirty=False），不会
    重新走 replace_text/layout。必须先 _kill_all_text_layout()
    （kill_layout + dirty=True + tokenized=False），restart 后 Text 才会
    重新 tokenize + replace_text（缓存命中→中文）+ 用新字体 layout。
    """
    with _refresh_lock:
        _want_refresh[0] = True
        if _refresh_timer_active[0]:
            return
        _refresh_timer_active[0] = True

    def _go():
        time.sleep(1.0)  # 合并窗口：等窗口内其余翻译完成
        with _refresh_lock:
            _refresh_timer_active[0] = False
            if not _want_refresh[0]:
                return
            _want_refresh[0] = False
        try:
            _kill_all_text_layout()
            import renpy
            renpy.exports.invoke_in_main_thread(renpy.restart_interaction)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _spawn_translate(key_text, table):
    """后台翻译并缓存（不阻塞游戏线程）。同文去重、有上限。"""
    with _inflight_lock:
        if key_text in _inflight:
            return
        if _pending[0] >= MAX_PENDING:
            return
        _inflight.add(key_text)
        _pending[0] += 1

    def work():
        try:
            result = request_translation(key_text, prefetch=False)
            if result is not None:
                cache_set(key_text, result)
                out = _restore(result, table) if table else result
                _mark_known(out)
                _refresh_after_translate()
        finally:
            with _inflight_lock:
                _inflight.discard(key_text)
                _pending[0] -= 1

    threading.Thread(target=work, daemon=True).start()


def _prefetch_next_lines(count=6):
    """预取后续 count 句对话（沿 next 链线性遍历 Say 节点）。

    只预取 1 句不够：LLM 翻译要 2-8 秒，用户读完当前句可能只要 2-5 秒，
    下一句显示时缓存还没写入 → 先显示英文。一次预取后续 6 句，给 LLM
    足够的缓冲时间，多数情况下用户推进到下几句时译文已在缓存里。
    沿 next 链线性遍历；遇分支/结束节点自动停止，不阻塞任何线程。
    """
    try:
        import renpy
        current = getattr(renpy.game.context(), 'current', None)
        if current is None:
            return
        node = getattr(current, 'next', None)
        visited = set()
        for _ in range(max(1, count)):
            if not node or node in visited:
                break
            visited.add(node)
            try:
                node_obj = renpy.game.script.lookup(node)
                if node_obj is None:
                    break
                what = getattr(node_obj, 'what', None)
                if what:
                    protected, table = _protect(what)
                    key_text = protected if table else what
                    if cache_get(key_text) is None:
                        _spawn_translate(key_text, table)
                # 沿 next 链继续（menu/分支节点 what 为空，但仍可继续沿 next 走）
                node = getattr(node_obj, 'next', None)
            except Exception:
                break
    except Exception:
        pass


def replace_text(text):
    """config.replace_text 回调：文本显示前调用，返回替换后的文本。

    绝不阻塞游戏线程：缓存命中立即返回译文；未命中立即返回原文，
    由后台线程翻译并写入本地缓存（同句后续显示即命中，跨会话持久）。
    """
    try:
        if not text or not text.strip():
            return text
        if _is_known(text):
            return text  # 已是译文，避免二次翻译
        protected, table = _protect(text)
        key_text = protected if table else text
        hit = cache_get(key_text)
        if hit is None:
            # 游戏侧内存未命中：先同步查服务器本地缓存（SQLite 命中 <10ms），
            # 这样第二次打开游戏时所有已翻译句子都直接显示中文
            hit = _quick_lookup(key_text)
        if hit is not None:
            cache_set(key_text, hit)
            return _restore(hit, table) if table else hit
        _spawn_translate(key_text, table)
        return text
    except Exception:
        return text


def say_callback(who, what):
    """config.say_callback 回调：当前句显示时后台预取（当前句 + 后续多句）。"""
    try:
        if what:
            protected, table = _protect(what)
            key_text = protected if table else what
            if cache_get(key_text) is None:
                _spawn_translate(key_text, table)
        _prefetch_next_lines()
    except Exception:
        pass


# ---------------------------------------------------------------- 安装

_install_log = []


def _write_debug(msg):
    """把安装结果写入游戏根目录 zz_mt_hook_debug.txt（M0 诊断用）。"""
    _install_log.append(msg)
    try:
        import renpy as _r
        base = getattr(_r.config, 'basedir', None)
        if base:
            with open(os.path.join(base, 'zz_mt_hook_debug.txt'), 'w', encoding='utf-8') as f:
                f.write(msg + '\n')
    except Exception:
        pass


def install_hooks():
    """安装 Hook（假设 renpy 已就绪）。保留游戏原有的回调并串联。"""
    try:
        import renpy  # noqa
    except Exception:
        return False
    if renpy.config is None:
        return False

    old_rt = getattr(renpy.config, 'replace_text', None)
    old_sc = getattr(renpy.config, 'say_callback', None)

    def _rt(text):
        out = old_rt(text) if callable(old_rt) else text
        return replace_text(out)

    def _sc(who, what):
        if callable(old_sc):
            old_sc(who, what)
        say_callback(who, what)

    renpy.config.replace_text = _rt
    renpy.config.say_callback = _sc

    # 中文字体补丁：用 _CJKFontReplaceDict 对任何字体名都替换为 msyh.ttc，
    # 清缓存 + invoke_in_main_thread(restart_interaction) 让已渲染的 Text 重新 layout。
    cjk = install_fonts(relayout=True)
    _write_debug('install OK; fonts -> %s' % (cjk or 'NONE'))

    # TODO(M1): renpy.mintranslate 备选替换路径（Ren'Py 8.2+）
    return True


def _wait_and_install():
    """后台线程：等待游戏完全初始化（显示界面就绪）后安装 Hook。

    绝不在早期 init 阶段安装（改 config / 清字体缓存会与初始化竞态，
    间歇性导致游戏启动即退出——真机踩过 3 次）。
    """
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            import renpy
            if renpy.config is None:
                time.sleep(0.5)
                continue
            if getattr(renpy.game, 'interface', None) is None:
                time.sleep(0.5)
                continue
            # interface 创建后渲染器仍在初始化，再等 3 秒避开竞态窗口
            time.sleep(3)
            if install_hooks():
                _write_debug('install OK (post-init)')
                return
        except Exception:
            pass
        time.sleep(0.5)
    _write_debug('install timeout (120s)')


def main():
    # 统一走"等界面就绪再安装"，避免 init 期竞态
    threading.Thread(target=_wait_and_install, daemon=True).start()


# 无条件自动安装：兼容 init python 执行（__name__ 未必是 __main__）、
# PyRun_SimpleString 注入、以及模块导入三种加载方式。
main()
