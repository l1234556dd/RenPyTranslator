# -*- coding: utf-8 -*-
"""翻译引擎适配层。

- LLMEngine：OpenAI 兼容 Chat Completions（DeepSeek / OpenAI / 通义 / 本地 Ollama 等），
  国内可达、质量好、成本低，是推荐主力引擎（AI 高质量模式）。
- FreeTranslateEngine：免费快速引擎（Google 网页翻译接口为主 + MyMemory 兜底），
  无 API key、无 token 计费、单句 0.4-0.8s，占位符 §§N§§ 原样保留（2026 实测可用）。
- MockEngine：离线联调用，返回【译】+原文（M0 验证同款）。
- Registry：按配置创建引擎。

注意：有道 / 百度 / 必应 / MS Edge 等网页接口在本机网络实测均被反爬拦截，
不可作为免费兜底（2026 实测），故不内置。
"""

import html
import re
import time

# requests 仅在 LLM 引擎中需要，按需导入（mock 引擎零第三方依赖）

SYSTEM_PROMPT = (
    '你是一名专业的游戏本地化翻译。把用户给出的游戏文本从{src}翻译成{dst}。\n'
    '规则：\n'
    '1) 只输出译文本身，不要解释、不要引号、不要反问；\n'
    '2) 即使原文只是单个单词或短标签（如按钮名、菜单项），也直接给出最常见的对应译法，'
    '绝不询问或要求提供更多内容；\n'
    '3) 严格保留以下所有占位符原样不动：\n'
    '   - [[N]]（N为非负整数，如 [[0]]、[[1]]）\n'
    '   - §§N§§（N为非负整数，两个 section sign 夹数字）\n'
    '   - [xxx] 和 {{xxx}} 形式的变量插槽\n'
    '   绝不删除、翻译、重排、包裹引号或拆分——它们是游戏变量插槽，丢失会让游戏崩溃；\n'
    '4) 保持对话语气与人物口吻，符合目标语言习惯；\n'
    '5) 专有名词首次出现时优先音译并保留原文。'
)

# 常见"模型反问"回复特征：视为翻译失败，直接返回原文
_META_PATTERNS = ('请提供', '需要翻译', '请给出', '翻译成中文', '原文是', '你好，请')

_PLACEHOLDER_RE = re.compile(r'(\[[^\]]*\]|\{[^}]*\})')
# 与 gamehook 的 \x00N\x00 令牌互转；LLM 对空字节不可靠，统一用 ASCII 的 [[N]]
_NULL_RE = re.compile(r'\x00(\d+)\x00')
_TOKEN_RE = re.compile(r'\[\[(\d+)\]\]')


def _to_llm_tokens(text):
    return _NULL_RE.sub(lambda m: '[[%s]]' % m.group(1), text)


def _from_llm_tokens(text):
    return _TOKEN_RE.sub(lambda m: '\x00%s\x00' % m.group(1), text)


def _is_meta_response(out, src):
    """判断 LLM 输出是否为"反问/拒绝翻译"的元回复。"""
    for pat in _META_PATTERNS:
        if pat in out:
            return True
    # 短输入却输出超长"回答"（如 '3' -> 一段说明），视为异常
    if len(src) <= 12 and len(out) > len(src) * 3 + 6:
        return True
    return False


class EngineError(Exception):
    pass


class BaseEngine(object):
    name = 'base'

    def translate(self, text, src='auto', dst='zh'):
        raise NotImplementedError


class MockEngine(BaseEngine):
    """离线假翻译：返回【译】+原文。用于无密钥时的链路联调。"""

    name = 'mock'
    last_usage = None   # mock 无 token 消耗，恒为 None

    def translate(self, text, src='auto', dst='zh'):
        return '【译】' + text


class FreeTranslateEngine(BaseEngine):
    """免费快速翻译引擎：Google 网页翻译接口（主）+ MyMemory（兜底）。

    无 API key、无 token 计费（last_usage 恒为 None）、单句 0.4-0.8s。
    占位符 §§N§§ 原样保留（已实测）。翻译质量：机翻水平。
    """

    name = 'free'

    # 语言代码映射（dst -> Google/MyMemory 兼容代码；zh 简体统一用 zh-CN）
    _LANG_CODES = {
        'zh': 'zh-CN',
        'zh-TW': 'zh-TW',
        'en': 'en',
        'ja': 'ja',
        'ko': 'ko',
        'ru': 'ru',
        'es': 'es',
        'fr': 'fr',
        'de': 'de',
    }

    def __init__(self, timeout=6):
        self.timeout = timeout

    @property
    def last_usage(self):
        return None   # 免费接口无 token 计费

    def translate(self, text, src='auto', dst='zh'):
        """翻译单条文本。

        1) 尝试 Google 网页翻译接口（client=gtx），占位符 §§N§§ 原样保留；
        2) Google 失败/空译文 -> MyMemory 兜底；
        3) 两者都失败 -> 抛 EngineError。
        """
        if not text:
            return ''
        google_src = self._LANG_CODES.get(
            src, 'auto' if src == 'auto' or not src else src)
        google_dst = self._LANG_CODES.get(dst, 'zh-CN')
        errors = []
        try:
            out = self._google_translate(google_src, google_dst, text)
            if out:
                return out
            errors.append('Google 返回空译文')
        except Exception as e:
            errors.append('Google: %r' % (e,))
        # Google 失败/空 -> MyMemory 兜底（src 无法 auto，'auto' 兜底为 'en'）
        my_src = self._LANG_CODES.get(
            src, 'en' if src == 'auto' or not src else src)
        my_dst = self._LANG_CODES.get(dst, 'zh-CN')
        try:
            out = self._mymemory_translate(my_src, my_dst, text)
            if out:
                return out
            errors.append('MyMemory 返回空译文')
        except Exception as e:
            errors.append('MyMemory: %r' % (e,))
        raise EngineError('免费翻译接口不可用: %s' % '; '.join(errors))

    def translate_batch(self, texts, src='auto', dst='zh'):
        """多行批量翻译（Google 一次请求翻多句，大幅提速）。

        已实测：20 条/次请求 ~1.1s（单条 20 次 ~15s），加速 ~14x；
        §§N§§ 占位符逐行保留；行数与输入一致。
        返回与 texts 等长的译文列表；某行翻译失败/为空时该行回退为原文。
        任何异常抛 EngineError（调用方兜底逐条 translate）。
        """
        if not texts:
            return []
        # 空串不参与合并（避免 '\n' 拼接导致行错位），对应位置直接返回 ''
        non_empty = [(i, t) for i, t in enumerate(texts) if t]
        if not non_empty:
            return [''] * len(texts)
        google_src = self._LANG_CODES.get(
            src, 'auto' if src == 'auto' or not src else src)
        google_dst = self._LANG_CODES.get(dst, 'zh-CN')
        try:
            lines = self._google_translate_batch(
                google_src, google_dst, [t for _, t in non_empty])
        except Exception as e:
            raise EngineError('Google 批量翻译失败: %r' % (e,))
        result = [''] * len(texts)
        for idx, (pos, original) in enumerate(non_empty):
            line = lines[idx] if idx < len(lines) else ''
            # 空译文 / 原样返回 -> 回退原文（Google 偶发漏译）
            result[pos] = line if line and line != original else original
        return result

    def _google_translate_batch(self, src, dst, texts):
        """调用 Google 免费网页翻译接口批量翻译多行文本。

        q='\n'.join(texts) 一次请求，响应所有 seg 拼接后按 '\n' 拆回逐行译文。
        行数少于输入时缺失行补 ''（由 translate_batch 回退原文），多于输入时截断。
        """
        import requests  # 按需导入
        r = requests.get(
            'https://translate.googleapis.com/translate_a/single',
            params={'client': 'gtx', 'sl': src, 'tl': dst, 'dt': 't',
                    'q': '\n'.join(texts)},
            timeout=self.timeout,
        )
        r.raise_for_status()
        j = r.json()
        segments = j[0] if isinstance(j, list) and j else []
        joined = ''.join(seg[0] for seg in segments if seg and seg[0])
        lines = joined.split('\n') if joined else []
        if len(lines) < len(texts):
            lines = lines + [''] * (len(texts) - len(lines))
        else:
            lines = lines[:len(texts)]
        return lines

    def _google_translate(self, src, dst, text):
        """调用 Google 免费网页翻译接口（client=gtx），返回译文或空字符串。"""
        import requests  # 按需导入
        r = requests.get(
            'https://translate.googleapis.com/translate_a/single',
            params={'client': 'gtx', 'sl': src, 'tl': dst, 'dt': 't', 'q': text},
            timeout=self.timeout,
        )
        r.raise_for_status()
        j = r.json()
        segments = j[0] if isinstance(j, list) and j else []
        return ''.join(seg[0] for seg in segments if seg and seg[0])

    def _mymemory_translate(self, src, dst, text):
        """调用 MyMemory 免费翻译接口（langpair=src|dst），返回译文或空字符串。"""
        import requests  # 按需导入
        r = requests.get(
            'https://api.mymemory.translated.net/get',
            params={'q': text, 'langpair': '%s|%s' % (src, dst)},
            timeout=self.timeout,
        )
        r.raise_for_status()
        j = r.json()
        data = j.get('responseData') or {}
        # MyMemory 偶尔返回 HTML 实体（如 &amp;），统一反转义
        return html.unescape(data.get('translatedText') or '')


class LLMEngine(BaseEngine):
    """OpenAI 兼容 Chat Completions 翻译引擎。"""

    name = 'llm'

    def __init__(self, base_url='https://api.deepseek.com', api_key='',
                 model='deepseek-chat', timeout=30):
        # 对 None/空值做兜底，避免直接 LLMEngine(base_url=None) 时 rstrip 崩溃
        self.base_url = (base_url or 'https://api.deepseek.com').rstrip('/')
        self.api_key = api_key or ''
        self.model = model or 'deepseek-chat'
        self.timeout = timeout
        self.last_usage = None   # 最近一次翻译的 token 消耗（dict 或 None）

    def translate(self, text, src='auto', dst='zh'):
        if not self.api_key:
            raise EngineError('LLM 引擎缺少 api_key，请在 config.json 或环境变量 RT_API_KEY 配置')
        self.last_usage = None   # 每次调用重置，失败时保持 None
        protected, table = self._protect(text)
        prompt = SYSTEM_PROMPT.format(src=src, dst=dst)
        try:
            import requests  # 按需导入：mock 引擎零第三方依赖
            r = requests.post(
                self.base_url + '/chat/completions',
                headers={'Authorization': 'Bearer ' + self.api_key,
                         'Content-Type': 'application/json'},
                json={
                    'model': self.model,
                    'messages': [
                        {'role': 'system', 'content': prompt},
                        {'role': 'user', 'content': _to_llm_tokens(protected or text)},
                    ],
                    'temperature': 0.3,
                    'stream': False,
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            out = data['choices'][0]['message']['content'].strip()
            # 记录本次 token 消耗（供费用统计）
            usage = data.get('usage') or {}
            if isinstance(usage, dict):
                self.last_usage = {
                    'prompt_tokens': usage.get('prompt_tokens', 0),
                    'completion_tokens': usage.get('completion_tokens', 0),
                    'total_tokens': usage.get('total_tokens', 0),
                }
        except EngineError:
            raise
        except Exception as e:
            raise EngineError('LLM 请求失败: %r' % (e,))
        # 顺序重要：先还原引擎自己的 [[N]] 表，再把剩余的（hook 传来的）[[N]] 转回 \x00 令牌
        if table:
            out = self._restore(out, table)
        out = _from_llm_tokens(out)
        # 兜底守卫：模型反问/拒绝翻译时，返回原文（避免缓存垃圾译文）
        if not out or _is_meta_response(out, text):
            return text
        return out

    @staticmethod
    def _protect(text):
        table = []

        def _sub(m):
            table.append(m.group(0))
            return '[[%d]]' % (len(table) - 1)

        return _PLACEHOLDER_RE.sub(_sub, text), table

    @staticmethod
    def _restore(text, table):
        def _sub(m):
            idx = int(m.group(1))
            if 0 <= idx < len(table):
                return table[idx]
            return m.group(0)

        return _TOKEN_RE.sub(_sub, text)


_REGISTRY = {
    'mock': MockEngine,
    'llm': LLMEngine,
    'free': FreeTranslateEngine,
}


def make_engine(cfg):
    name = cfg.get('engine', 'mock')
    cls = _REGISTRY.get(name)
    if cls is None:
        raise EngineError('未知引擎: %s（可选: %s）' % (name, ', '.join(_REGISTRY)))
    if name == 'llm':
        # 兜底默认值：公共 API 用最小 dict 调用时也不会因缺键传 None 而崩溃
        return cls(base_url=cfg.get('base_url') or 'https://api.deepseek.com',
                   api_key=cfg.get('api_key') or '',
                   model=cfg.get('model') or 'deepseek-chat')
    return cls()


if __name__ == '__main__':
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else 'Hello world'
    for eng in (MockEngine(), LLMEngine(api_key=__import__('os').environ.get('RT_API_KEY', ''))):
        t0 = time.time()
        try:
            print('%s: %r (%.2fs)' % (eng.name, eng.translate(text), time.time() - t0))
        except EngineError as e:
            print('%s: %s' % (eng.name, e))
