<div align="center">
  <img src="banner.png" alt="RenPyTranslator" width="100%"/>
</div>

# 🎮 RenPyTranslator — Ren'Py 游戏实时汉化工具

> 面向 Ren'Py 引擎视觉小说的 **Windows 桌面汉化工具**：实时游戏内翻译 + 一键全文翻译 + 免费/AI 双引擎，支持多语言、百分比翻译、token 费用预估，类 MTool 的开源替代。

<div align="center">
  **中文** ｜ <a href="README_EN.md">English</a>
</div>

<p align="center">
  <a href="https://github.com/l1234556dd/RenPyTranslator/stargazers"><img src="https://img.shields.io/github/stars/l1234556dd/RenPyTranslator?style=for-the-badge&label=Star%20%E2%AD%90"/></a>
  <a href="https://github.com/l1234556dd/RenPyTranslator/forks"><img src="https://img.shields.io/github/forks/l1234556dd/RenPyTranslator?style=for-the-badge&label=Fork"/></a>
  <a href="https://github.com/l1234556dd/RenPyTranslator/releases"><img src="https://img.shields.io/github/v/release/l1234556dd/RenPyTranslator?style=for-the-badge&label=Release"/></a>
  <img src="https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/UI-PySide6-green?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge"/>
  <img src="https://img.shields.io/badge/Platform-Windows-0078d6?style=for-the-badge"/>
</p>

---

## ✨ 功能特性

| 功能 | 说明 |
|---|---|
| 🚀 **实时游戏内翻译** | 注入 Ren'Py 游戏进程，对话即时翻译显示，无需退出游戏 |
| 📚 **一键全文翻译** | 自动提取游戏全部文本 → 估算 token/费用 → 弹窗确认 → 全量翻译进缓存 |
| 🎚️ **百分比翻译** | 先翻译游戏开头的 10%/25%/50%… 看效果，满意再翻更多，省钱可控 |
| 🆓 **免费快速引擎** | Google 网页翻译（免费、约 1 秒/句、多行批量 17x 提速），MyMemory 自动兜底 |
| 🤖 **AI 高质量引擎** | DeepSeek / OpenAI / 通义 / Kimi / Ollama 等 OpenAI 兼容接口，翻译更自然 |
| 🔤 **多语言支持** | 中简 / 繁中 / 英 / 日 / 韩 / 俄 / 西 / 法 / 德 9 种目标语言 |
| 💾 **本地永久缓存** | 译文存本地 SQLite，翻译一次永久有效，换游戏/重启不丢，不重复花钱 |
| 📊 **用量统计** | 实时显示 prompt/completion token 消耗与缓存命中率 |
| 📖 **术语表** | 人名/地名等专有名词固定译法，翻译更一致 |
| 🖼️ **中文渲染修复** | 自动替换游戏字体为中文字体，解决译文显示成"口口口" |

## 📸 界面预览

*（暗色"游戏汉化工作台"主题：暖灰背景 + 琥珀橙主色，功能分区清晰）*

## 🚀 快速开始

### 方式一：直接使用打包版（推荐）

1. 下载 `RenPyTranslator.exe`（单文件，双击即用，无需安装 Python）
2. 首次打开：填写游戏目录 → 选择翻译引擎 → 点「启动游戏并翻译」
3. 用户数据（配置/翻译缓存）保存在 `%APPDATA%\RenPyTranslator\`，删除即卸载

### 方式二：从源码运行

```bash
# 依赖（Python 3.11+）
pip install PySide6 requests

# 启动桌面界面
python translator/app.py

# 命令行启动游戏并翻译
python main.py --game "D:\game\你的RenPy游戏" --engine llm
```

## 📖 使用流程（推荐玩法）

```
1. 启动游戏并翻译          → 游戏进入主菜单，翻译器自动注入
2. 翻译整个游戏            → 自动提取全部文本，弹窗显示估算 token/费用
3. 选引擎 + 选比例         → 谷歌免费快速（0 费用）/ AI 高质量；10%~100%
4. 确认翻译                → 全量翻译进缓存（谷歌批量约几分钟/万条）
5. 重新进游戏              → 全中文秒出，零等待零回滚
```

> 💡 **省钱技巧**：先用「谷歌免费快速」全文翻译（不花 token），个别机翻质量差的句子再切「AI 高质量」精翻。已翻译的句子永久缓存，不会重复扣费。

## ⚙️ 配置说明

### 翻译引擎

| 引擎 | 速度 | 费用 | 质量 | 说明 |
|---|---|---|---|---|
| 免费快速（Google） | ~1s/句 | 免费 | 机翻 | 无需 API Key，网络通畅即可 |
| AI 高质量（LLM） | 2-8s/句 | 按 token | 自然 | DeepSeek/OpenAI/通义/Kimi/Ollama |
| mock | 即时 | 免费 | — | 离线联调用 |

### 支持的服务商（OpenAI 兼容）

DeepSeek / OpenAI / 通义千问 / Kimi(Moonshot) / Ollama(本地) / 自定义

## 🏗️ 技术架构

```
┌─────────────────────────────────────────────────┐
│  RenPyTranslator（PySide6 桌面端）                │
│  ├─ 引擎层  engines.py    (llm / free / mock)    │
│  ├─ 缓存层  cache.py      (SQLite + 术语表)      │
│  ├─ 服务器  server.py     (TCP 多线程翻译服务)    │
│  ├─ 注入器  injector.py   (ctypes 进程注入)      │
│  └─ 启动器  launcher.py   (启动游戏 + 注入)      │
└──────────────┬──────────────────────────────────┘
               │ localhost TCP（JSON 行协议）
┌──────────────▼──────────────────────────────────┐
│  游戏进程（Ren'Py）                               │
│  └─ gamehook/hook.py                              │
│     ├─ config.replace_text   显示前替换           │
│     ├─ config.say_callback   下一句预取           │
│     ├─ 占位符保护 §N§         变量/标签不破坏      │
│     └─ 字体替换              中文渲染修复         │
└─────────────────────────────────────────────────┘
```

## 📁 目录结构

```
renpy-translator/
├── main_gui.py              # 打包入口
├── main.py                  # CLI 入口
├── translator/
│   ├── app.py               # PySide6 桌面界面
│   ├── engines.py           # 引擎：llm / free(Google) / mock
│   ├── cache.py             # SQLite 缓存 + 术语表 + 用量统计
│   ├── server.py            # TCP 翻译服务器
│   ├── batch.py             # 批量翻译（多行合并加速）
│   ├── launcher.py          # 游戏启动 + 注入
│   ├── paths.py             # 路径管理（按游戏分目录缓存）
│   └── config.py            # 配置
├── gamehook/hook.py         # 游戏侧 Hook（注入到游戏进程）
├── injector/injector.py     # ctypes 注入器
├── tools/                   # 辅助工具（文本提取等）
└── 使用说明书.md            # 详细使用说明
```

## 🔨 从源码构建 exe

```bash
pip install pyinstaller PySide6-Essentials
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --icon icon.ico --name RenPyTranslator \
  --add-data "gamehook;gamehook" --add-data "injector;injector" \
  --add-data "tools;tools" --hidden-import requests main_gui.py
```

> ⚠️ 软件包含"向游戏进程注入翻译"能力，杀毒软件可能误报，请添加信任/排除项。

## 📄 许可证

本项目基于 [MIT License](LICENSE) 开源。

## ⚠️ 免责声明

- 本工具仅用于**个人学习与合法汉化**用途
- 请勿用于绕过 DRM / 反作弊系统
- 汉化文本版权归原游戏厂商所有，请勿传播翻译后的游戏内容
- 使用 Google 免费翻译接口属个人行为，请遵守相关服务条款
