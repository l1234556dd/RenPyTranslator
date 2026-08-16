# -*- coding: utf-8 -*-
"""SQLite 缓存、术语表与用量统计。

- translations：原文 hash -> 译文（去重、复用，降低引擎调用量）。
- glossary：术语表（专有名词/人名，优先于机器翻译）。
- usage：每次 LLM 翻译的 token 消耗记录（用于费用统计）。
"""

import hashlib
import os
import sqlite3
import threading
import time

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS translations(
  hash TEXT PRIMARY KEY,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  lang TEXT NOT NULL,
  engine TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS glossary(
  src TEXT PRIMARY KEY,
  dst TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  engine TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  created REAL NOT NULL
);
'''

# Ren'Py 常见界面术语默认词典：单词语义歧义大（Off/Start/On 等），
# 走术语表可在翻译前精确替换，避免 LLM 误译。
UI_GLOSSARY = [
    ('Start', '开始'), ('Load', '读取'), ('Save', '保存'), ('Quit', '退出'),
    ('Settings', '设置'), ('Gallery', '图鉴'), ('Back', '返回'), ('Return', '返回'),
    ('Main', '主菜单'), ('Help', '帮助'), ('Skip', '跳过'), ('Auto', '自动'),
    ('Hide', '隐藏'), ('Show', '显示'), ('On', '开'), ('Off', '关'),
    ('None', '无'), ('All', '全部'), ('Yes', '是'), ('No', '否'),
    ('Continue', '继续'), ('History', '历史'), ('New Game', '新游戏'),
    ('Fullscreen', '全屏'), ('Window', '窗口'), ('Transitions', '过渡'),
    ('Text Speed', '文本速度'), ('Soundtrack', '原声带'), ('Sound Effects', '音效'),
    ('Next', '下一页'), ('Previous', '上一页'), ('Autosaves', '自动存档'),
    ('Quick-saves', '快速存档'), ('Normal', '正常'), ('Minigames', '小游戏'),
]


class Cache(object):
    # 类级计数器：跨实例共享。GUI 用临时实例也能读到服务器会话累计的命中/未命中数。
    hit_count = 0
    miss_count = 0
    _counter_lock = threading.Lock()

    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    @staticmethod
    def _h(text):
        return hashlib.md5(text.encode('utf-8')).hexdigest()

    @classmethod
    def _bump_counter(cls, hit):
        """线程安全地累加命中/未命中计数（类级共享）。"""
        with cls._counter_lock:
            if hit:
                cls.hit_count += 1
            else:
                cls.miss_count += 1

    def get(self, src_text, lang, engine):
        with self._lock:
            row = self._conn.execute(
                'SELECT dst FROM translations WHERE hash=? AND lang=? AND engine=?',
                (self._h(src_text), lang, engine)).fetchone()
            if row is not None:
                self._bump_counter(True)
                return row[0]
            self._bump_counter(False)
            return None

    def set(self, src_text, dst_text, lang, engine):
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO translations(hash, src, dst, lang, engine, created) '
                'VALUES(?,?,?,?,?,?)',
                (self._h(src_text), src_text, dst_text, lang, engine, time.time()))
            self._conn.commit()

    def migrate_old_placeholder_keys(self):
        """迁移旧 \x00N\x00 占位符格式的缓存为 \u00A7\u00A7N\u00A7\u00A7（§§N§§），
        使批量翻译写入的旧缓存能被游戏内 hook 命中（hook 使用 §§N§§ 格式查缓存）。

        旧版本 translator/batch.py 的 protect() 用 '\x00N\x00' 保护占位符，而
        gamehook/hook.py 用 '\u00A7\u00A7N\u00A7\u00A7'，导致同一原文生成不同 md5
        缓存 key，批量翻译的缓存永远无法被游戏内命中。本方法把旧格式缓存行改写为
        新格式：src 中的每个 \x00 替换为两个 \u00A7（\x00N\x00 -> §§N§§），
        重新计算 hash 写入新行，再删除旧行。返回迁移的行数。
        """
        with self._lock:
            rows = self._conn.execute(
                'SELECT hash, src, dst, lang, engine, created FROM translations').fetchall()
            changed = 0
            for h, src, dst, lang, engine, created in rows:
                if '\x00' not in src:
                    continue
                new_src = src.replace('\x00', '\u00A7\u00A7')  # \x00N\x00 -> §§N§§
                new_h = self._h(new_src)
                if new_h != h:
                    self._conn.execute(
                        'INSERT OR REPLACE INTO translations(hash, src, dst, lang, engine, created) '
                        'VALUES(?,?,?,?,?,?)',
                        (new_h, new_src, dst, lang, engine, created))
                    self._conn.execute('DELETE FROM translations WHERE hash=?', (h,))
                    changed += 1
            self._conn.commit()
            return changed

    # ---------------- 术语表 ----------------

    def glossary_terms(self):
        with self._lock:
            rows = self._conn.execute('SELECT src, dst FROM glossary').fetchall()
            return list(rows)

    def set_glossary(self, src, dst):
        with self._lock:
            self._conn.execute(
                'INSERT OR REPLACE INTO glossary(src, dst, created) VALUES(?,?,?)',
                (src, dst, time.time()))
            self._conn.commit()

    def remove_glossary(self, src):
        with self._lock:
            self._conn.execute('DELETE FROM glossary WHERE src=?', (src,))
            self._conn.commit()

    def apply_glossary(self, text):
        """把术语表里的词条应用到原文（精确匹配替换）。"""
        out = text
        for src, dst in self.glossary_terms():
            if src and src in out:
                out = out.replace(src, dst)
        return out

    def seed_ui_glossary(self):
        """写入 Ren'Py 常见界面术语（仅缺失时插入，不覆盖用户自定义）。"""
        with self._lock:
            for src, dst in UI_GLOSSARY:
                self._conn.execute(
                    'INSERT OR IGNORE INTO glossary(src, dst, created) VALUES(?,?,?)',
                    (src, dst, time.time()))
            self._conn.commit()

    # ---------------- 用量统计 ----------------

    def record_usage(self, engine, model, prompt_tokens, completion_tokens):
        """记录一次 LLM 翻译的 token 消耗。"""
        with self._lock:
            self._conn.execute(
                'INSERT INTO usage(engine, model, prompt_tokens, completion_tokens, created) '
                'VALUES(?,?,?,?,?)',
                (engine, model, int(prompt_tokens or 0),
                 int(completion_tokens or 0), time.time()))
            self._conn.commit()

    def usage_stats(self):
        """token 消耗汇总：总 prompt/completion tokens、总条数、按 engine/model 分组。"""
        with self._lock:
            total = self._conn.execute(
                'SELECT COALESCE(SUM(prompt_tokens),0), COALESCE(SUM(completion_tokens),0), '
                'COUNT(*) FROM usage').fetchone()
            grouped = self._conn.execute(
                'SELECT engine, model, COALESCE(SUM(prompt_tokens),0), '
                'COALESCE(SUM(completion_tokens),0), COUNT(*) '
                'FROM usage GROUP BY engine, model ORDER BY engine, model').fetchall()
        return {
            'prompt_tokens': total[0],
            'completion_tokens': total[1],
            'count': total[2],
            'by_engine': [
                {'engine': e, 'model': m, 'prompt_tokens': p,
                 'completion_tokens': c, 'count': n}
                for e, m, p, c, n in grouped
            ],
        }

    def stats(self):
        with self._lock:
            t = self._conn.execute('SELECT COUNT(*) FROM translations').fetchone()[0]
            g = self._conn.execute('SELECT COUNT(*) FROM glossary').fetchone()[0]
        total = self.hit_count + self.miss_count
        hit_rate = (self.hit_count / float(total)) if total else 0.0
        return {
            'translations': t, 'glossary': g,
            'hit_count': self.hit_count, 'miss_count': self.miss_count,
            'hit_rate': hit_rate,
        }
