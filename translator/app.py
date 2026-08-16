# -*- coding: utf-8 -*-
"""RenPy 翻译工具 —— 桌面界面（全功能版）。

功能：
  · 实时翻译：启动游戏并自动注入 / 附加到运行中进程 / 停止
  · 批量预翻译：提取游戏全部文本 -> 批量翻译进本地缓存（跳过模式全中文）
  · 术语表管理：添加 / 删除专有名词词条
  · 引擎配置：mock / LLM（DeepSeek 等 OpenAI 兼容），内置服务商预设
  · 用量统计：token 消耗、缓存命中率
  · 缓存统计与清空
  · 实时日志

用法: python translator/app.py
"""

import os
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from translator import batch as batch_mod       # noqa: E402
from translator import cache as cache_mod       # noqa: E402
from translator import config as config_mod     # noqa: E402
from translator import engines                  # noqa: E402
from translator import launcher                 # noqa: E402
from translator import paths                    # noqa: E402
from translator import server as server_mod     # noqa: E402
from translator import setup_safe_io            # noqa: E402

setup_safe_io()

from PySide6 import QtCore, QtWidgets          # noqa: E402

# ---------------------------------------------------------------------------
# 服务商预设（均走 OpenAI 兼容接口，无需改动 engines）
# ---------------------------------------------------------------------------
PROVIDERS = [
    ('DeepSeek', 'https://api.deepseek.com', 'deepseek-chat'),
    ('OpenAI', 'https://api.openai.com/v1', 'gpt-4o-mini'),
    ('通义千问', 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus'),
    ('Kimi(Moonshot)', 'https://api.moonshot.cn/v1', 'moonshot-v1-8k'),
    ('Ollama(本地)', 'http://localhost:11434/v1', 'llama3'),
    ('自定义', '', ''),
]

# 批量翻译服务器固定端口：与实时翻译服务器（cfg['port']，默认 24567）错开，
# 因为游戏运行中实时服务器已占用 cfg port，batch 再绑同一端口会 bind 冲突。
BATCH_PORT = 24610


def est_tokens(text):
    """粗略估算一段文本的 token 数（粗估，实际以翻译时 API 返回 usage 为准）。

    中文字符（CJK）约 1.5~2 token 一个（取中点 1.75），英文/数字/符号约 4 字符
    一个 token：
      tokens ≈ cjk * 1.75 + ascii / 4
    仅用于弹窗估算，不参与实际翻译计费。
    """
    if not text:
        return 0
    cjk = 0
    ascii_ = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF       # CJK 统一表意文字
                or 0x3400 <= cp <= 0x4DBF   # CJK 扩展 A
                or 0x3040 <= cp <= 0x30FF   # 平假名/片假名
                or 0xAC00 <= cp <= 0xD7AF   # 谚文音节
                or 0xF900 <= cp <= 0xFAFF):  # CJK 兼容表意文字
            cjk += 1
        else:
            ascii_ += 1
    return max(1, round(cjk * 1.75 + ascii_ / 4))


# ---------------------------------------------------------------------------
# 目标语言下拉（显示名 -> 语言代码）
# ---------------------------------------------------------------------------
LANGUAGES = [
    ('中文（简体）', 'zh'),
    ('繁体中文', 'zh-TW'),
    ('英文', 'en'),
    ('日文', 'ja'),
    ('韩文', 'ko'),
    ('俄语', 'ru'),
    ('西班牙语', 'es'),
    ('法语', 'fr'),
    ('德语', 'de'),
]

# ---------------------------------------------------------------------------
# “翻译整个游戏”比例档位（10% 排第一，用户主要想先试小比例）与确认弹窗
# ---------------------------------------------------------------------------
RATIO_OPTIONS = [
    (10, '10%'),
    (25, '25%'),
    (50, '50%'),
    (75, '75%'),
    (100, '100%'),
]


class _PercentDialog(QtWidgets.QDialog):
    """“翻译整个游戏”确认弹窗：选择翻译引擎与翻译比例，预计算各档位的条数 / token / 费用。

    - 引擎下拉：AI 高质量（llm，按 token 计费）或 谷歌免费快速（free，不消耗 token）；
      默认跟随全局配置 engine，本次选择只影响本次“翻译整个游戏”，不改全局配置；
    - 比例按去重后文本列表（sorted，≈游戏脚本顺序）的前 N% 计算，即“游戏开头部分”；
    - token / 费用估算只统计子集内“未在缓存”的条目——已在缓存的句子不重复扣费；
      free 引擎不估算 token，费用显示为“免费”；
    - 确认后返回 (百分比, 引擎名)，前 K 条条数由 selected_count() 获取，
      由调用方以 max_items=K + engine_name 只翻译前 K 条。
    """

    def __init__(self, parent, keys, cached_keys, prompt_price, completion_price,
                 engine='llm'):
        super().__init__(parent)
        self.setWindowTitle('翻译整个游戏')
        self.setMinimumWidth(540)
        self._keys = list(keys)
        self._cached_keys = set(cached_keys)
        self._prompt_price = prompt_price
        self._completion_price = completion_price
        # 每档预计算: (pct, K, subset_cached, subset_todo, est_in, est_out, cost)
        self._options = []

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 16, 18, 16)

        total = len(self._keys)
        cached = len(self._cached_keys)
        todo = total - cached
        info = QtWidgets.QLabel('共 %d 条文本（其中 %d 条已在缓存），去重后 %d 条待翻译'
                                % (total, cached, todo))
        info.setObjectName('subLabel')
        info.setWordWrap(True)
        layout.addWidget(info)

        # 翻译引擎下拉：本次“翻译整个游戏”使用的引擎（llm=AI 高质量 / free=谷歌免费）
        engine_row = QtWidgets.QHBoxLayout()
        engine_row.addWidget(QtWidgets.QLabel('翻译引擎：'))
        self.engine_combo = QtWidgets.QComboBox()
        self.engine_combo.addItem('AI 高质量（DeepSeek 等）', 'llm')
        self.engine_combo.addItem('谷歌免费快速（全文翻译）', 'free')
        # 默认选中全局 engine（llm/free 对应项；mock 等未知引擎落到 llm）
        engine_idx = self.engine_combo.findData(engine)
        self.engine_combo.setCurrentIndex(max(0, engine_idx))
        self.engine_combo.currentIndexChanged.connect(self._refresh_detail)
        engine_row.addWidget(self.engine_combo, 1)
        layout.addLayout(engine_row)

        combo_row = QtWidgets.QHBoxLayout()
        combo_row.addWidget(QtWidgets.QLabel('翻译比例：'))
        self.combo = QtWidgets.QComboBox()
        for pct, label in RATIO_OPTIONS:
            K = min(total, max(1, total * pct // 100))
            subset = self._keys[:K]
            todo_keys = [k for k in subset if k not in self._cached_keys]
            est_in = sum(est_tokens(t) for t in todo_keys) + len(todo_keys) * 30
            est_out = int(est_in * 0.7)
            cost = (est_in / 1e6 * self._prompt_price
                    + est_out / 1e6 * self._completion_price)
            self._options.append((pct, K, len(subset) - len(todo_keys),
                                  len(todo_keys), est_in, est_out, cost))
            self.combo.addItem('%s · 约 %d 条 · 输入≈%d tokens · 费用≈￥%.2f'
                               % (label, K, est_in, cost), (pct, K))
        # 默认选中 50%（10% 固定在首位，容易找）
        default_idx = 0
        for i, (pct, _label) in enumerate(RATIO_OPTIONS):
            if pct == 50:
                default_idx = i
                break
        self.combo.setCurrentIndex(default_idx)
        self.combo.currentIndexChanged.connect(self._refresh_detail)
        combo_row.addWidget(self.combo, 1)
        layout.addLayout(combo_row)

        self.detail_label = QtWidgets.QLabel()
        self.detail_label.setWordWrap(True)
        layout.addWidget(self.detail_label)

        self.note_label = QtWidgets.QLabel()
        self.note_label.setObjectName('subLabel')
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        button_box = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        ok_btn = button_box.button(QtWidgets.QDialogButtonBox.Ok)
        ok_btn.setText('确定')
        ok_btn.setDefault(True)
        cancel_btn = button_box.button(QtWidgets.QDialogButtonBox.Cancel)
        cancel_btn.setText('取消')
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

        # 按钮下方说明：引擎选择对速度 / 成本的影响（固定一行，不随选择变化）
        self.footer_label = QtWidgets.QLabel(
            '免费引擎翻译快且不花 token（机翻质量）；AI 引擎质量好但按 token 计费。')
        self.footer_label.setObjectName('subLabel')
        self.footer_label.setWordWrap(True)
        layout.addWidget(self.footer_label)

        self._refresh_detail(default_idx)

    def _refresh_detail(self, *args):
        """随翻译引擎与比例下拉框选择更新详细估算与说明文字（双联动刷新）。"""
        idx = self.combo.currentIndex()
        engine = self.engine_combo.currentData()
        pct, K, subset_cached, subset_todo, est_in, est_out, cost = self._options[idx]
        lines = [
            '将翻译前 %d 条（游戏开头部分，占全部 %d%%）' % (K, pct),
            '其中 %d 条已在缓存（跳过，不重复扣费），实际待译 %d 条'
            % (subset_cached, subset_todo),
        ]
        if engine == 'free':
            # 免费引擎不消耗 token，费用显示为“免费”
            lines.append('费用：免费（Google 网页翻译，不消耗 token）')
        else:
            est_total = est_in + est_out
            lines.append('预计：输入 ≈ %d tokens，输出 ≈ %d tokens，合计 ≈ %d tokens'
                         % (est_in, est_out, est_total))
            lines.append('参考费用：约 ￥%.2f（输入 %.1f / 输出 %.1f 元每百万 token）'
                         % (cost, self._prompt_price, self._completion_price))
        self.detail_label.setText('\n'.join(lines))
        self.note_label.setText(
            '翻译游戏开头的 %d%% 文本先看效果，满意再翻译更多。已翻译的句子不会重复扣费。'
            % pct)

    def selected(self):
        """返回用户选择的 (百分比, 引擎名)。前 K 条条数用 selected_count() 获取。"""
        return self.combo.currentData()[0], self.engine_combo.currentData()

    def selected_count(self):
        """返回用户选择比例对应的前 K 条条数。"""
        return self.combo.currentData()[1]

# ---------------------------------------------------------------------------
# 暗色主题 QSS：暖灰背景 + 暖琥珀橙主色，克制精致
# ---------------------------------------------------------------------------
QSS = """
* { outline: none; }
QWidget {
    background: #1e1b18;
    color: #e8e2d8;
    font-family: "Microsoft YaHei", "微软雅黑", "PingFang SC", sans-serif;
    font-size: 13px;
}
QToolTip {
    background: #282420;
    color: #e8e2d8;
    border: 1px solid #3a352f;
}

QLabel { background: transparent; color: #e8e2d8; }
QLabel#subLabel { color: #9a938a; }
QLabel#value { font-weight: bold; color: #f0e9dd; }

QGroupBox {
    background: #282420;
    border: 1px solid #3a352f;
    border-radius: 8px;
    margin-top: 16px;
    padding: 14px 12px 12px 12px;
    font-weight: bold;
    font-size: 14px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    top: 2px;
    padding: 0 6px;
    color: #e8a33d;
    font-weight: bold;
}

QLineEdit, QComboBox {
    background: #1e1b18;
    border: 1px solid #3a352f;
    border-radius: 5px;
    padding: 5px 8px;
    color: #e8e2d8;
    selection-background-color: #e8a33d;
    selection-color: #1e1b18;
}
QLineEdit:hover, QComboBox:hover { border: 1px solid #4a443d; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #e8a33d; }
QLineEdit:disabled, QComboBox:disabled { color: #6a635a; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #282420;
    border: 1px solid #3a352f;
    color: #e8e2d8;
    selection-background-color: #3a352f;
    selection-color: #e8a33d;
}

QPushButton {
    background: #2f2a25;
    border: 1px solid #3a352f;
    border-radius: 5px;
    padding: 6px 14px;
    color: #e8e2d8;
}
QPushButton:hover { background: #3a352f; }
QPushButton:pressed { background: #241f1b; }
QPushButton:disabled { color: #6a635a; background: #242019; border-color: #2c2823; }
QPushButton#primary {
    background: #e8a33d;
    color: #1e1b18;
    border: none;
    font-weight: bold;
    padding: 10px 22px;
    font-size: 14px;
}
QPushButton#primary:hover { background: #f0b455; }
QPushButton#primary:pressed { background: #d18f2d; }
QPushButton#primary:disabled { background: #574828; color: #8a7a5a; }

QProgressBar {
    background: #1e1b18;
    border: 1px solid #3a352f;
    border-radius: 5px;
    text-align: center;
    color: #e8e2d8;
    height: 16px;
}
QProgressBar::chunk {
    background: #e8a33d;
    border-radius: 4px;
}

QTableWidget {
    background: #1e1b18;
    alternate-background-color: #23201c;
    border: 1px solid #3a352f;
    border-radius: 5px;
    gridline-color: #3a352f;
    color: #e8e2d8;
}
QTableWidget::item { padding: 3px; }
QTableWidget::item:selected { background: #3a352f; color: #e8a33d; }
QHeaderView::section {
    background: #282420;
    color: #9a938a;
    border: none;
    border-bottom: 1px solid #3a352f;
    padding: 5px 6px;
    font-weight: bold;
}

QPlainTextEdit {
    background: #0f0d0a;
    border: 1px solid #3a352f;
    border-radius: 5px;
    color: #c8c2b8;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
}

QScrollBar:vertical { background: #1e1b18; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #3a352f; border-radius: 5px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #4a443d; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: #1e1b18; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #3a352f; border-radius: 5px; min-width: 24px; }
QScrollBar::handle:horizontal:hover { background: #4a443d; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
"""


class Worker(QtCore.QObject):
    """后台线程：游戏服务器/批量翻译/文本提取。"""

    log = QtCore.Signal(str)
    state = QtCore.Signal(str)
    progress = QtCore.Signal(int, int, int, str)   # done, total, fail, note
    done = QtCore.Signal(str)                       # 'game' | 'batch' | 'extract'
    extract_ok = QtCore.Signal(str)                 # dump 文件路径

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._srv = None
        self._cache = None
        self._proc = None
        self.busy = False

    def log_msg(self, m):
        self.log.emit(m)

    # ---------------- 实时翻译 ----------------

    @QtCore.Slot(dict, object)
    def run_game(self, cfg, attach_pid):
        self.busy = True
        try:
            self._cache = cache_mod.Cache(paths.cache_path(cfg.get('game_dir')))
            self._cache.seed_ui_glossary()
            engine = engines.make_engine(cfg)
            self._srv = server_mod.TranslatorServer(
                engine, self._cache, host=cfg['host'], port=int(cfg['port']),
                src=cfg['src'], dst=cfg['dst'], log=self.log_msg, verbose=False)
            port = self._srv.start()
            self.state.emit('服务器已启动 127.0.0.1:%d（引擎=%s）' % (port, engine.name))
            if attach_pid:
                ok, msg = launcher.inject_into(attach_pid)
                self.log_msg(msg)
                self.state.emit('已附加进程 %d' % attach_pid)
                while not self._stop.wait(1.0):
                    pass
            else:
                exe = cfg.get('game_exe') or None
                self._proc = launcher.launch_game(cfg['game_dir'], cfg['host'], port, exe=exe)
                self.log_msg('游戏进程 pid=%d，等待注入...' % self._proc.pid)
                ok, msg = launcher.inject_into(self._proc.pid)
                self.log_msg(msg)
                self.state.emit('游戏运行中，翻译已生效')
                while not self._stop.wait(1.0):
                    if self._proc.poll() is not None:
                        break
                self.state.emit('游戏已退出')
        except Exception as e:
            self.log_msg('错误: %r' % (e,))
        finally:
            if self._srv:
                self._srv.stop()
            if self._cache:
                self._cache.close()
            self.busy = False
            self.done.emit('game')

    # ---------------- 批量预翻译 ----------------

    @QtCore.Slot(str, int, int, object, object, object)
    def run_batch(self, dump_path, port, workers, max_items=None, subset_label=None,
                  engine_name=None):
        self.busy = True
        try:
            def cb(done, total, fail, note):
                self.progress.emit(done, total, fail, note or '')
            r = batch_mod.run_batch(dump_path, port=port, workers=workers,
                                    progress_cb=cb, max_items=max_items,
                                    engine_name=engine_name)
            head = (subset_label + '；') if subset_label else ''
            self.state.emit('%s批量翻译完成：成功 %d，失败 %d，本地已有 %d，用时 %.0f 秒' % (
                head, r['done'] - r['fail'], r['fail'], r['cached'], r['seconds']))
        except Exception as e:
            self.log_msg('批量翻译错误: %r' % (e,))
        finally:
            self.busy = False
            self.done.emit('batch')

    # ---------------- 文本提取 ----------------

    @QtCore.Slot(str)
    def run_extract(self, game_dir):
        self.busy = True
        try:
            exe = launcher.find_game_exe(game_dir)
            if not exe:
                self.state.emit('未找到游戏 exe：%s' % game_dir)
                return
            name = os.path.splitext(os.path.basename(exe))[0]
            pid = launcher.find_process_by_name(name + '.exe')
            if not pid:
                self.state.emit('游戏未运行。请先点击"启动游戏并翻译"，再提取文本。')
                return
            self.log_msg('注入文本提取脚本到 pid=%d ...' % pid)
            ok, msg = launcher.inject_into(pid, hook_path=launcher._DUMP_PATH)
            self.log_msg(msg)
            if not ok:
                self.state.emit('提取脚本注入失败')
                return
            dump = os.path.join(game_dir, 'zz_texts.json')
            deadline = time.time() + 20
            while time.time() < deadline:
                if os.path.exists(dump):
                    try:
                        import json
                        with open(dump, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        count = data.get('count', len(data.get('texts', [])))
                        self.state.emit('提取完成：%d 条文本 -> %s' % (count, dump))
                        self.extract_ok.emit(dump)
                        return
                    except Exception as e:
                        pass
                time.sleep(1)
            self.state.emit('等待提取结果超时（%s）' % dump)
        except Exception as e:
            self.log_msg('提取错误: %r' % (e,))
        finally:
            self.busy = False
            self.done.emit('extract')

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass


class MainWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('游戏汉化工作台 · RenPy 翻译工具')
        self.resize(960, 700)
        self.setMinimumSize(760, 560)
        self._thread = None
        self._worker = None
        self._dump_path = None
        # “翻译整个游戏”一键流程状态：_full_pending 覆盖提取阶段（True 表示提取完成后
        # 需继续估算+弹窗+批量）；_full_busy 覆盖整个流程（含批量翻译阶段），防止重复进入。
        self._full_pending = False
        self._full_busy = False
        # 用量统计每 10 秒自动刷新（手动"刷新用量"按钮仍保留）
        self._usage_timer = QtCore.QTimer(self)
        self._usage_timer.timeout.connect(self._refresh_usage)
        self._usage_timer.start(10000)
        self._build_ui()
        # 统一初始化按钮状态（start/attach/stop/extract/batch/full 全部对齐）：
        # 游戏未运行，提取与"翻译整个游戏"均应禁用。
        self._set_running(False)
        self._load_config()

    # ---------------- UI ----------------

    @staticmethod
    def _caption(text):
        """次要说明文字（暖灰）。"""
        lbl = QtWidgets.QLabel(text)
        lbl.setObjectName('subLabel')
        return lbl

    @staticmethod
    def _value(text='0'):
        """统计数值文字（加粗暖白）。"""
        lbl = QtWidgets.QLabel(text)
        lbl.setObjectName('value')
        return lbl

    def _build_ui(self):
        # 顶层布局：滚动区域填满窗口，底部固定状态栏
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # 主内容区包进 QScrollArea：小屏窗口高度不足时可滚动查看，内容永不挤压
        self.scroll_area = QtWidgets.QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._content = QtWidgets.QWidget()
        content = QtWidgets.QVBoxLayout(self._content)
        content.setSpacing(10)
        content.setContentsMargins(16, 16, 16, 16)
        self.scroll_area.setWidget(self._content)
        root.addWidget(self.scroll_area, 1)

        # --- 基本配置 ---
        group_basic = QtWidgets.QGroupBox('基本配置')
        form = QtWidgets.QFormLayout(group_basic)
        form.setLabelAlignment(QtCore.Qt.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.game_edit = QtWidgets.QLineEdit()
        browse = QtWidgets.QPushButton('浏览…')
        browse.clicked.connect(self._browse)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.game_edit, 1)
        row.addWidget(browse)
        form.addRow('游戏目录', row)

        self.exe_label = QtWidgets.QLabel('（未检测）')
        self.exe_label.setObjectName('subLabel')
        detect_btn = QtWidgets.QPushButton('重新检测')
        detect_btn.clicked.connect(self._detect_exe)
        pick_btn = QtWidgets.QPushButton('指定启动程序…')
        pick_btn.clicked.connect(self._pick_exe)
        row2 = QtWidgets.QHBoxLayout()
        row2.addWidget(self.exe_label, 1)
        row2.addWidget(detect_btn)
        row2.addWidget(pick_btn)
        form.addRow('启动程序', row2)

        self.engine_combo = QtWidgets.QComboBox()
        self.engine_combo.addItem('AI 高质量（DeepSeek 等）', 'llm')
        self.engine_combo.addItem('免费快速（Google 网页翻译）', 'free')
        self.engine_combo.addItem('mock（离线联调）', 'mock')
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        form.addRow('翻译引擎', self.engine_combo)

        self.provider_combo = QtWidgets.QComboBox()
        for name, _url, _model in PROVIDERS:
            self.provider_combo.addItem(name, name)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow('服务商预设', self.provider_combo)

        self.base_url_edit = QtWidgets.QLineEdit()
        form.addRow('Base URL', self.base_url_edit)
        self.api_key_edit = QtWidgets.QLineEdit()
        self.api_key_edit.setEchoMode(QtWidgets.QLineEdit.Password)
        form.addRow('API Key', self.api_key_edit)
        self.model_edit = QtWidgets.QLineEdit()
        form.addRow('模型', self.model_edit)
        self.dst_combo = QtWidgets.QComboBox()
        for _name, _code in LANGUAGES:
            self.dst_combo.addItem(_name, _code)
        form.addRow('目标语言', self.dst_combo)
        self.port_edit = QtWidgets.QLineEdit('24567')
        form.addRow('端口', self.port_edit)
        content.addWidget(group_basic)

        # --- 实时翻译控制 ---
        group_ctrl = QtWidgets.QGroupBox('实时翻译控制')
        btns = QtWidgets.QHBoxLayout(group_ctrl)
        self.start_btn = QtWidgets.QPushButton('启动游戏并翻译')
        self.start_btn.setObjectName('primary')
        self.start_btn.clicked.connect(self._start)
        self.attach_btn = QtWidgets.QPushButton('附加到进程…')
        self.attach_btn.clicked.connect(self._attach)
        self.stop_btn = QtWidgets.QPushButton('停止')
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop)
        btns.addWidget(self.start_btn)
        btns.addWidget(self.attach_btn)
        btns.addWidget(self.stop_btn)
        btns.addStretch(1)
        content.addWidget(group_ctrl)

        # --- 批量预翻译 ---
        group_batch = QtWidgets.QGroupBox('批量预翻译（跳过模式全中文）')
        vb = QtWidgets.QVBoxLayout(group_batch)
        row3 = QtWidgets.QHBoxLayout()
        self.extract_btn = QtWidgets.QPushButton('提取文本（需游戏运行中）')
        self.extract_btn.setEnabled(False)     # 初始未运行游戏，提取禁用
        self.extract_btn.clicked.connect(self._extract)
        self.batch_btn = QtWidgets.QPushButton('批量翻译…')
        self.batch_btn.setEnabled(True)        # 批量翻译始终可用
        self.batch_btn.clicked.connect(self._batch)
        self.full_btn = QtWidgets.QPushButton('翻译整个游戏（先估算 token）')
        self.full_btn.setObjectName('primary') # 主按钮，与“启动游戏并翻译”同级突出
        self.full_btn.setEnabled(False)        # 初始未运行游戏，与 extract_btn 一致
        self.full_btn.clicked.connect(self._full_translate)
        self.batch_info = QtWidgets.QLabel('未提取')
        self.batch_info.setObjectName('subLabel')
        row3.addWidget(self.extract_btn)
        row3.addWidget(self.batch_btn)
        row3.addWidget(self.full_btn)
        row3.addWidget(self.batch_info, 1)
        vb.addLayout(row3)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        vb.addWidget(self.progress_bar)
        content.addWidget(group_batch)

        # --- 术语表与缓存 ---
        group_glossary = QtWidgets.QGroupBox('术语表（专有名词固定译法）与缓存')
        vb2 = QtWidgets.QVBoxLayout(group_glossary)
        self.glossary_table = QtWidgets.QTableWidget(0, 2)
        self.glossary_table.setHorizontalHeaderLabels(['原文', '译文'])
        self.glossary_table.horizontalHeader().setStretchLastSection(True)
        self.glossary_table.setAlternatingRowColors(True)
        self.glossary_table.setMaximumHeight(130)
        vb2.addWidget(self.glossary_table)
        row4 = QtWidgets.QHBoxLayout()
        self.add_term_btn = QtWidgets.QPushButton('添加词条')
        self.add_term_btn.clicked.connect(self._add_term)
        self.del_term_btn = QtWidgets.QPushButton('删除选中')
        self.del_term_btn.clicked.connect(self._del_term)
        self.refresh_btn = QtWidgets.QPushButton('刷新')
        self.refresh_btn.clicked.connect(self._refresh_glossary)
        self.cache_label = QtWidgets.QLabel('')
        self.cache_label.setObjectName('subLabel')
        self.clear_cache_btn = QtWidgets.QPushButton('清空翻译缓存')
        self.clear_cache_btn.clicked.connect(self._clear_cache)
        row4.addWidget(self.add_term_btn)
        row4.addWidget(self.del_term_btn)
        row4.addWidget(self.refresh_btn)
        row4.addWidget(self.cache_label, 1)
        row4.addWidget(self.clear_cache_btn)
        vb2.addLayout(row4)
        content.addWidget(group_glossary)

        # --- 用量统计 ---
        group_usage = QtWidgets.QGroupBox('用量统计')
        grid = QtWidgets.QGridLayout(group_usage)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)

        self.usage_prompt_label = self._value()
        self.usage_completion_label = self._value()
        self.usage_total_label = self._value()
        self.usage_hit_label = self._value()
        self.usage_miss_label = self._value()
        self.usage_rate_label = self._value('0%')

        grid.addWidget(self._caption('Prompt tokens'), 0, 0)
        grid.addWidget(self.usage_prompt_label, 0, 1)
        grid.addWidget(self._caption('Completion tokens'), 0, 2)
        grid.addWidget(self.usage_completion_label, 0, 3)
        grid.addWidget(self._caption('总 tokens'), 1, 0)
        grid.addWidget(self.usage_total_label, 1, 1)
        grid.addWidget(self._caption('缓存命中'), 2, 0)
        grid.addWidget(self.usage_hit_label, 2, 1)
        grid.addWidget(self._caption('未命中'), 2, 2)
        grid.addWidget(self.usage_miss_label, 2, 3)
        grid.addWidget(self._caption('命中率'), 3, 0)
        grid.addWidget(self.usage_rate_label, 3, 1)
        self.usage_refresh_btn = QtWidgets.QPushButton('刷新用量')
        self.usage_refresh_btn.clicked.connect(self._refresh_usage)
        grid.addWidget(self.usage_refresh_btn, 3, 3)
        grid.setColumnStretch(3, 1)
        content.addWidget(group_usage)

        # --- 日志 ---
        group_log = QtWidgets.QGroupBox('日志')
        vlog = QtWidgets.QVBoxLayout(group_log)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        # 滚动容器内日志高度固定为可读高度，随内容一起滚动
        self.log_view.setMinimumHeight(140)
        # 防止日志刷爆 GUI 线程：限行数 + 批量刷盘
        self.log_view.setMaximumBlockCount(500)
        self._log_buffer = []
        self._log_timer = QtCore.QTimer(self)
        self._log_timer.setInterval(300)
        self._log_timer.timeout.connect(self._flush_logs)
        self._log_timer.start()
        vlog.addWidget(self.log_view)
        content.addWidget(group_log)

        self.status_label = QtWidgets.QLabel('就绪')
        self.status_label.setObjectName('subLabel')
        # 状态栏固定在窗口底部（不随内容滚动）
        root.addSpacing(4)
        root.addWidget(self.status_label)

        self._refresh_glossary()
        self._refresh_usage()

    # ---------------- 配置 ----------------

    def _load_config(self):
        cfg = config_mod.load()
        self.game_edit.setText(cfg.get('game_dir', ''))
        idx = self.engine_combo.findData(cfg.get('engine'))
        self.engine_combo.setCurrentIndex(max(0, idx))
        self._on_engine_changed()   # 按加载的引擎同步 LLM 配置区可用/禁用状态
        self.base_url_edit.setText(cfg.get('base_url', ''))
        self.api_key_edit.setText(cfg.get('api_key', ''))
        self.model_edit.setText(cfg.get('model', ''))
        idx = self.dst_combo.findData(cfg.get('dst', 'zh'))
        self.dst_combo.setCurrentIndex(max(0, idx))
        self.port_edit.setText(str(cfg.get('port', 24567)))
        self._select_provider(cfg.get('base_url', ''))
        if cfg.get('game_exe'):
            self.exe_label.setText(cfg['game_exe'])
            self._set_exe_color(True)
            # exe 所在目录即游戏根目录：修正历史配置里可能选错的子目录
            root = os.path.dirname(cfg['game_exe'])
            cur = cfg.get('game_dir', '')
            if cur and os.path.normpath(root) != os.path.normpath(cur):
                self.game_edit.setText(root)
                self.status_label.setText('游戏目录已自动修正到：%s' % root)
        else:
            self._detect_exe()
        self._refresh_usage()

    def _collect_cfg(self):
        exe = self.exe_label.text()
        if exe.startswith('（') or exe.startswith('('):
            exe = ''
        return {
            'game_dir': os.path.normpath(self.game_edit.text().strip() or ''),
            'game_exe': os.path.normpath(exe) if exe else '',
            'engine': self.engine_combo.currentData(),
            'base_url': self.base_url_edit.text().strip(),
            'api_key': self.api_key_edit.text().strip(),
            'model': self.model_edit.text().strip(),
            'dst': self.dst_combo.currentData() or 'zh',
            'port': self.port_edit.text().strip() or '24567',
            'host': '127.0.0.1',
            'src': 'auto',
        }

    def _cache_path(self):
        """当前游戏目录对应的缓存库路径。"""
        return paths.cache_path(self.game_edit.text().strip())

    def _select_provider(self, base_url):
        """根据 base_url 反查预设项，选中匹配项，否则落到"自定义"。"""
        base_url = (base_url or '').rstrip('/')
        for i, (name, url, _model) in enumerate(PROVIDERS):
            if name != '自定义' and url.rstrip('/') == base_url:
                self.provider_combo.setCurrentIndex(i)
                return
        self.provider_combo.setCurrentIndex(len(PROVIDERS) - 1)

    def _on_provider_changed(self):
        name = self.provider_combo.currentData()
        if name == '自定义':
            return
        for pname, url, model in PROVIDERS:
            if pname == name:
                self.base_url_edit.setText(url)
                self.model_edit.setText(model)
                return

    def _on_engine_changed(self):
        """引擎切换联动：仅 LLM 模式需要服务商/Base URL/API Key/模型配置。

        free（Google 网页翻译）与 mock（离线）模式用不到这些输入框，统一置灰，
        避免用户误以为免费模式还需要填 key。
        """
        is_llm = self.engine_combo.currentData() == 'llm'
        self.provider_combo.setEnabled(is_llm)
        self.base_url_edit.setEnabled(is_llm)
        self.api_key_edit.setEnabled(is_llm)
        self.model_edit.setEnabled(is_llm)

    # ---------------- 启动程序检测 ----------------

    def _set_exe_color(self, ok):
        if ok:
            self.exe_label.setStyleSheet('color: #7fbf6a;')
        else:
            self.exe_label.setStyleSheet('color: #d97a5f;')

    def _detect_exe(self):
        game_dir = self.game_edit.text().strip()
        if not game_dir:
            self.exe_label.setText('（未检测）')
            self.exe_label.setStyleSheet('color: #9a938a;')
            return
        exe = launcher.find_game_exe(game_dir)
        if exe:
            self.exe_label.setText(exe)
            self._set_exe_color(True)
            # 若检测到的 exe 不在当前填写的目录里（用户选了子目录），
            # 自动把游戏目录修正到 exe 所在根目录
            root = os.path.dirname(exe)
            if os.path.normpath(root) != os.path.normpath(game_dir):
                self.game_edit.setText(root)
                self.status_label.setText('已自动定位到游戏根目录：%s' % root)
        else:
            self.exe_label.setText('未检测到 exe，请点"指定启动程序…"')
            self._set_exe_color(False)

    def _pick_exe(self):
        exe, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择游戏启动程序', self.game_edit.text().strip() or '',
            '程序 (*.exe)')
        if not exe:
            return
        # exe 所在目录就是游戏根目录
        self.game_edit.setText(os.path.dirname(exe))
        self.exe_label.setText(exe)
        self._set_exe_color(True)

    # ---------------- 动作 ----------------

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, '选择游戏根目录')
        if d:
            self.game_edit.setText(d)
            self._detect_exe()

    def _begin(self, cfg, attach_pid):
        self._run_task(lambda: self._worker.run_game(cfg, attach_pid))

    def _start(self):
        if not self.game_edit.text().strip():
            QtWidgets.QMessageBox.warning(self, '提示', '请先选择游戏目录')
            return
        cfg = self._collect_cfg()
        config_mod.save(cfg)
        self._begin(cfg, None)

    def _attach(self):
        pid, ok = QtWidgets.QInputDialog.getInt(self, '附加进程', '输入游戏进程 PID:')
        if not ok:
            return
        cfg = self._collect_cfg()
        config_mod.save(cfg)
        self._begin(cfg, pid)

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self.status_label.setText('正在停止…')

    def _extract(self):
        game_dir = self.game_edit.text().strip()
        if not game_dir:
            QtWidgets.QMessageBox.warning(self, '提示', '请先选择游戏目录')
            return
        self._run_task(lambda: self._worker.run_extract(game_dir))

    def _batch(self):
        start = self._dump_path
        if not start or not os.path.exists(start):
            start, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, '选择提取的文本 JSON', '', 'JSON (*.json)')
        if not start:
            return
        self._dump_path = start
        self._run_task(lambda: self._worker.run_batch(start, BATCH_PORT, 8))
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

    # ---------------- 翻译整个游戏（一键流程） ----------------

    def _full_translate(self):
        """翻译整个游戏：检查游戏运行 -> 提取文本 -> 估算 token -> 弹窗确认 -> 批量翻译。

        复用现有能力：提取复用 Worker.run_extract（注入 dump 脚本 + 轮询
        zz_texts.json），批量复用 Worker.run_batch（run_batch 内部含旧缓存迁移）。
        """
        if self._full_busy:
            return  # 已有一次完整流程在进行中，忽略重复点击
        game_dir = self.game_edit.text().strip()
        if not game_dir:
            QtWidgets.QMessageBox.warning(self, '提示', '请先选择游戏目录')
            return
        cfg = self._collect_cfg()
        config_mod.save(cfg)
        # a. 检查游戏运行（提取文本需要游戏进程在跑，注入 dump 脚本枚举全部文本）
        exe = launcher.find_game_exe(game_dir)
        if not exe:
            QtWidgets.QMessageBox.warning(
                self, '提示', '未找到游戏 exe：%s' % game_dir)
            return
        name = os.path.splitext(os.path.basename(exe))[0]
        pid = launcher.find_process_by_name(name + '.exe')
        if not pid:
            QtWidgets.QMessageBox.warning(
                self, '提示', '游戏未运行。请先点『启动游戏并翻译』再翻译整个游戏。')
            return
        # b. 提取文本（Worker 线程中注入 dump 脚本，最多轮询 20 秒）
        self._full_pending = True
        self._full_busy = True
        self._run_task(lambda: self._worker.run_extract(game_dir))

    def _full_confirm_and_run(self, dump):
        """提取成功后（主线程回调）：读文本 -> 估算 token/费用 -> 弹窗选比例确认 -> 批量翻译。"""
        self._full_pending = False
        try:
            raw_count, keys = batch_mod.load_texts(dump)
        except Exception as e:
            # 读文本失败：_full_pending 已复位、_on_done('extract') 兜底不会再触发，
            # 必须在这里同步复位 _full_busy，否则流程永久卡在 busy 状态。
            self._full_busy = False
            QtWidgets.QMessageBox.warning(self, '提取失败', '读取提取文本失败: %r' % (e,))
            return
        cfg = config_mod.load()
        dst = cfg.get('dst', 'zh')
        engine = cfg.get('engine', 'mock')
        prompt_price = float(cfg.get('prompt_price', 1.0))
        completion_price = float(cfg.get('completion_price', 2.0))
        # 统计已在本地缓存的键（与 run_batch 的 todo 判定一致，估算与翻译都跳过）
        cached_keys = set()
        try:
            c = cache_mod.Cache(self._cache_path())
            for k in keys:
                if c.get(k, dst, engine) is not None:
                    cached_keys.add(k)
            c.close()
        except Exception:
            cached_keys = set()
        cached = len(cached_keys)
        todo = len(keys) - cached
        # c. 弹窗：选翻译引擎与翻译比例并确认（QDialog 自绘，各档位条数/token/费用已预计算）
        dlg = _PercentDialog(self, keys, cached_keys, prompt_price, completion_price,
                             engine=engine)
        if dlg.exec() != QtWidgets.QDialog.Accepted:
            self._full_busy = False
            self.log_view.appendPlainText('已取消翻译整个游戏')
            return
        pct, engine_name = dlg.selected()
        K = min(dlg.selected_count(), len(keys))
        subset_cached = len([k for k in keys[:K] if k in cached_keys])
        subset_todo = K - subset_cached
        # d. 批量翻译（固定 BATCH_PORT，与实时翻译服务器错开端口；只翻译前 K 条；
        #    引擎用弹窗所选 engine_name：free 走谷歌免费全文翻译，llm 走 AI）
        engine_display = {'free': '谷歌免费引擎（不消耗 token）',
                          'llm': 'AI 引擎（按 token 计费）'}.get(
            engine_name, '引擎=%s' % engine_name)
        self.log_view.appendPlainText(
            '开始翻译（%s）：游戏开头前 %d 条（占全部 %d%%，其中 %d 条已在缓存，实际待译 %d 条）'
            % (engine_display, K, pct, subset_cached, subset_todo))
        subset_label = '本次翻译了游戏开头前 %d 条（占全部 %d%%）' % (K, pct)
        self._run_task(lambda: self._worker.run_batch(
            dump, BATCH_PORT, 8, max_items=K, subset_label=subset_label,
            engine_name=engine_name))
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)

    def _add_term(self):
        src, ok1 = QtWidgets.QInputDialog.getText(self, '添加词条', '原文（英文）:')
        if not ok1 or not src.strip():
            return
        dst, ok2 = QtWidgets.QInputDialog.getText(self, '添加词条', '译文:')
        if not ok2:
            return
        c = cache_mod.Cache(self._cache_path())
        c.set_glossary(src.strip(), dst.strip())
        c.close()
        self._refresh_glossary()

    def _del_term(self):
        row = self.glossary_table.currentRow()
        if row < 0:
            return
        src = self.glossary_table.item(row, 0).text()
        c = cache_mod.Cache(self._cache_path())
        c.remove_glossary(src)
        c.close()
        self._refresh_glossary()

    def _refresh_glossary(self):
        try:
            c = cache_mod.Cache(self._cache_path())
            terms = c.glossary_terms()
            stats = c.stats()
            c.close()
        except Exception:
            terms, stats = [], {'translations': 0, 'glossary': 0}
        self.glossary_table.setRowCount(len(terms))
        for i, (s, d) in enumerate(terms):
            self.glossary_table.setItem(i, 0, QtWidgets.QTableWidgetItem(s))
            self.glossary_table.setItem(i, 1, QtWidgets.QTableWidgetItem(d))
        self.cache_label.setText('译文缓存 %d 条 | 术语 %d 条' % (
            stats.get('translations', 0), stats.get('glossary', 0)))

    def _refresh_usage(self):
        try:
            c = cache_mod.Cache(self._cache_path())
            us = c.usage_stats()
            st = c.stats()
            c.close()
        except Exception:
            us = {'prompt_tokens': 0, 'completion_tokens': 0, 'count': 0, 'by_engine': []}
            st = {'hit_count': 0, 'miss_count': 0, 'hit_rate': 0.0}
        pt = int(us.get('prompt_tokens', 0) or 0)
        ct = int(us.get('completion_tokens', 0) or 0)
        self.usage_prompt_label.setText(str(pt))
        self.usage_completion_label.setText(str(ct))
        self.usage_total_label.setText(str(pt + ct))
        hit = int(st.get('hit_count', 0) or 0)
        miss = int(st.get('miss_count', 0) or 0)
        self.usage_hit_label.setText(str(hit))
        self.usage_miss_label.setText(str(miss))
        self.usage_rate_label.setText('%.1f%%' % (float(st.get('hit_rate', 0.0) or 0.0) * 100))

    def _clear_cache(self):
        if self._worker and self._worker.busy:
            QtWidgets.QMessageBox.warning(self, '提示', '翻译运行中，请先停止')
            return
        if QtWidgets.QMessageBox.question(
                self, '确认', '清空全部翻译缓存？（已翻译句子需重新调用 LLM）') \
                != QtWidgets.QMessageBox.Yes:
            return
        try:
            for suf in ('', '-wal', '-shm'):
                p = self._cache_path() + suf
                if os.path.exists(p):
                    os.remove(p)
            self.log_view.appendPlainText('已清空翻译缓存')
            self._refresh_glossary()
            self._refresh_usage()
        except Exception as e:
            self.log_view.appendPlainText('清空缓存失败: %r' % (e,))

    # ---------------- 线程管理 ----------------

    def _on_log_msg(self, msg):
        # 先入缓冲（廉价），由定时器批量写入视图
        self._log_buffer.append(msg)

    def _flush_logs(self):
        if not self._log_buffer:
            return
        msgs = self._log_buffer
        self._log_buffer = []
        self.log_view.appendPlainText('\n'.join(msgs))

    def _run_task(self, fn):
        # 任务在普通 Python daemon 线程中执行。
        # 注意：不要用 QThread.started.connect(纯函数)——纯函数无线程归属，
        # PySide 会投递回主线程执行，阻塞式任务会把 GUI 冻结（此前未响应 bug 的根因）。
        # Worker 留在主线程，其信号（log/state/progress/done）从工作线程
        # 跨线程投递到主线程，是 Qt 的可靠机制。
        self._worker = Worker()
        self._worker.log.connect(self._on_log_msg)
        self._worker.state.connect(self.status_label.setText)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_done)
        self._worker.extract_ok.connect(self._on_extract)
        threading.Thread(target=fn, daemon=True).start()
        self._set_running(True)

    def _on_progress(self, done, total, fail, note):
        if total > 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(done)
        if note:
            self.batch_info.setText(note)
        else:
            self.batch_info.setText('%d/%d  失败 %d' % (done, total, fail))

    def _on_extract(self, dump):
        self._dump_path = dump
        self.batch_info.setText('已提取：' + os.path.basename(dump))
        if self._full_pending:
            self._full_confirm_and_run(dump)

    def _on_done(self, kind):
        if kind == 'batch':
            self.progress_bar.setVisible(False)
            self._full_busy = False  # 完整流程的批量翻译阶段结束
            self._refresh_glossary()
            self._refresh_usage()
        if kind == 'game':
            self._refresh_glossary()
            self._refresh_usage()
        if kind == 'extract' and self._full_pending:
            # 完整流程的提取阶段结束但没走到 _on_extract（成功回调未触发）：
            # 说明提取失败/超时，弹窗报错并复位，避免流程卡在 pending 状态。
            self._full_pending = False
            self._full_busy = False
            QtWidgets.QMessageBox.warning(
                self, '提取失败',
                self.status_label.text() or '文本提取失败/超时，请查看日志')
        if self._full_busy:
            # 完整流程仍在进行（提取成功后的确认弹窗 / 批量翻译阶段）：
            # 不要用陈旧 extract 的 done 信号把 running 状态重置掉，避免批量
            # 翻译运行中按钮被错误重新启用。
            return
        self._set_running(False)

    def _set_running(self, running):
        self.start_btn.setEnabled(not running)
        self.attach_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.extract_btn.setEnabled(running)   # 提取文本需要游戏运行中
        self.batch_btn.setEnabled(True)        # 批量翻译不依赖运行状态，始终可用
        self.full_btn.setEnabled(running)      # 翻译整个游戏同样需要游戏运行中（先提取）

    def closeEvent(self, event):
        if self._worker:
            self._worker.stop()
        event.accept()


def _install_excepthook():
    """未捕获异常写入数据目录 error.log（打包版无控制台，便于排查）。"""
    import traceback

    def hook(exc_type, exc_value, exc_tb):
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            p = os.path.join(paths.data_dir(), 'error.log')
            with open(p, 'a', encoding='utf-8') as f:
                f.write(msg + '\n')
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def _install_watchdog():
    """看门狗：每 3 秒把全部线程栈写入数据目录 threads.log（冻结时抓现场）。"""

    def loop():
        import traceback
        while True:
            try:
                p = os.path.join(paths.data_dir(), 'threads.log')
                frames = sys._current_frames()
                lines = ['==== dump %s ====' % time.strftime('%H:%M:%S')]
                for tid, frame in frames.items():
                    name = str(tid)
                    try:
                        import threading
                        name = next((t.name for t in threading.enumerate()
                                     if t.ident == tid), str(tid))
                    except Exception:
                        pass
                    lines.append('--- thread %s ---' % name)
                    lines.extend(traceback.format_stack(frame))
                with open(p, 'a', encoding='utf-8') as f:
                    f.write('\n'.join(lines) + '\n')
            except Exception:
                pass
            time.sleep(3)

    threading.Thread(target=loop, daemon=True).start()


def main():
    _install_excepthook()
    _install_watchdog()
    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(QSS)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
