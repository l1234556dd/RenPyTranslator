# -*- coding: utf-8 -*-
"""zz_dump_texts.py — 路线 B 文本提取：注入游戏进程，用 Ren'Py 自身 AST 枚举全部文本。

遍历 renpy.game.script.all_stmts：
  - Say 节点（对话，含 [var] 插值原样保留）
  - Menu 节点（选项文本）
  - TranslateString 节点（_() 字符串）
跳过真翻译节点（language 非 None 的 TranslateSay/Translate，避免提取译文）。
结果写入游戏根目录 zz_texts.json：{"count": N, "texts": [...]}（已去重排序）。
"""

import json
import os


def _write(obj):
    try:
        import renpy
        base = getattr(renpy.config, 'basedir', None) or os.getcwd()
        path = os.path.join(base, 'zz_texts.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        return path
    except Exception:
        try:
            path = os.path.join(os.environ.get('TEMP', '.'), 'zz_texts.json')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(json.dumps(obj, ensure_ascii=False))
            return path
        except Exception:
            return None


try:
    import renpy
    from renpy import ast as _ast

    texts = set()
    nodes = getattr(renpy.game.script, 'all_stmts', None)
    if nodes is None:
        _write({'error': 'all_stmts is None'})
    else:
        for node in nodes:
            try:
                if isinstance(node, _ast.Say):
                    # TranslateSay 是 Say 的子类：language=None 表示原文对话（必须提取），
                    # language 非 None 才是真翻译节点（跳过，避免提取译文）。
                    if getattr(node, 'language', None) is not None:
                        continue
                    if node.what:
                        texts.add(node.what)
                elif isinstance(node, _ast.Menu):
                    for item in (node.items or []):
                        label = item[0] if item else None
                        if label:
                            texts.add(label)
                elif isinstance(node, _ast.TranslateString):
                    t = getattr(node, 'text', None)
                    if t:
                        texts.add(t)
            except Exception:
                pass
        result = {'count': len(texts), 'texts': sorted(texts)}
        p = _write(result)
        print('zz_dump_texts: %d texts -> %s' % (len(texts), p))
except Exception as e:
    _write({'error': repr(e)})
