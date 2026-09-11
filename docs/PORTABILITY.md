# 通用性：bookforge 能在哪些 agent 里跑？

一句话结论：

> **bookforge 的核心是一个普通 Python 命令行程序。凡是能执行 shell 命令、
> 能访问网络与本地文件系统的宿主，都能用它。**
> 各家 agent「技能格式」的差异，只影响你放哪份说明书，不影响能不能跑。

下面把「技能能不能通用」这件事拆成三层，这样遇到任何一个新宿主，
你都能自己判断该怎么做。

---

## 三层结构

```
┌─────────────────────────────────────────────────────────┐
│ ③ 宿主层   WorkBuddy / Claude Code / Codex / Cursor /    │
│            豆包 / 自建 agent …                            │
│            差异：认哪个"说明书"文件名、能不能跑命令        │
├─────────────────────────────────────────────────────────┤
│ ② 接入层   SKILL.md / AGENTS.md / .cursor/rules / MCP …   │
│            本质：同一份使用说明的不同包装（可复制）        │
├─────────────────────────────────────────────────────────┤
│ ① 能力层   bookforge CLI + Python 引擎                    │
│            抓取·提取·分组·排版·封面·打包 —— 全部确定性代码 │
│            这一层是真正通用的，不依赖任何宿主             │
└─────────────────────────────────────────────────────────┘
```

**关键点**：把能力放在 ①，宿主适配放在 ②。
很多"技能"之所以不通用，是因为把逻辑写进了 ②（比如写成某个平台专有的
工具调用），于是换宿主就废了。bookforge 刻意反过来 —— ② 里只有几行说明。

---

## 各宿主要做什么

| 宿主 | 能跑 shell？ | 要放什么 | 做法 |
|---|---|---|---|
| **WorkBuddy** | ✅ | `SKILL.md`（带 frontmatter） | 把 `docs/SKILL.md` 放进 `~/.workbuddy/skills/bookforge/`，或直接用 `AGENTS.md` |
| **Claude Code / Claude.ai Skills** | ✅ | `SKILL.md`（Agent Skills 规范） | 同上，`name` + `description` frontmatter |
| **Codex (OpenAI)** | ✅ | `AGENTS.md` | 把 `docs/AGENTS.md` 作为 `AGENTS.md` 放到仓库根或 `~/.codex/AGENTS.md` |
| **Cursor** | ✅ | `.cursor/rules/bookforge.mdc` | 内容同 `AGENTS.md` |
| **Gemini CLI / Aider / 其它编码 agent** | ✅ | `AGENTS.md` 或直接说"读 AGENTS.md" | 同一份 |
| **自建 / 脚本 / cron / CI** | ✅ | 什么都不用 | 直接调 `bookforge build ...`，读 `reports/summary.json` |
| **豆包等封闭对话产品** | ❌ 通常不行 | 需要变通 | 见下 |

### 为什么「说明书」能复制来复制去

`docs/SKILL.md` 和 `docs/AGENTS.md` 的正文几乎是同一段话，只有包装不同：

```markdown
---
name: bookforge
description: 把网站文章变成 EPUB 电子书。当用户想把某个网站/博客的文章
  整理成电子书、想做 EPUB、想批量抓取网页转 Markdown 时使用。
---
（正文：怎么装、怎么跑一条命令、踩坑提示）
```

Agent Skills 规范要求 `name` + `description` 的 frontmatter；
Codex/Cursor 只需要一段 Markdown。**所以维护一份正文、两个包装即可**，
不存在"为每个平台重写一遍"的成本。

---

## 豆包这类封闭平台怎么办

如果平台**不允许执行 shell / 访问文件系统 / 联网**，那它没法直接跑 bookforge。
这不是 bookforge 的问题，而是那类平台的能力边界。有三种现实的变通：

### 方案 A：包成 MCP server（推荐）

把 bookforge 包成一个 MCP 工具，暴露给支持 MCP 的宿主：

```python
# 示意：mcp_server.py
from mcp.server.fastmcp import FastMCP
from bookforge.cli import main

app = FastMCP("bookforge")

@app.tool()
def build_ebook(url: str, out: str = "book.epub") -> dict:
    """把一个网站的文章抓下来，打包成 EPUB 电子书。"""
    import json, subprocess
    subprocess.run(["bookforge", "build", url, "-o", out, "--json"], check=False)
    return json.load(open(...))   # 读 reports/summary.json
```

宿主只要支持 MCP，就能像调内置工具一样调它。**MCP 是目前跨 agent
最通用的一条路**，WorkBuddy / Claude Desktop / Cursor / 部分国产 IDE 都支持。

### 方案 B：包成 HTTP 服务

在你自己有 shell 的机器上跑一个小服务（或用我们**已经带好的**本地通道）：

```bash
bookforge build <url> -o book.epub --json
```

然后让封闭平台通过 HTTP 触发、拿回 `summary.json` 和 `.epub` 的下载链接。
适合"平台不能跑代码、但我自己有服务器"的情况。

### 方案 C：把它当成"给有能力的 agent 用的工具"

最实用的做法：**在有 shell 的宿主里做，把成品丢给封闭平台**。
电子书是最终产物，`.epub` 文件本身在任何平台都是可读的。

---

## 让它在你的机器上跑起来的必要条件

| 需要 | 说明 | 缺了会怎样 |
|---|---|---|
| Python ≥ 3.9 | 任何发行版都行 | 跑不了 |
| 能 `pip install` | 装 7 个依赖 | `bookforge doctor` 会告诉你缺哪个、怎么装 |
| 能访问目标站点 | 抓取需要联网 | 抓不到 |
| 一个中文字体 | 封面渲染中文需要 | 封面会降级；`doctor` 给平台对应的安装命令 |

`bookforge doctor` 就是为这个设计的 —— 它会把上面每一项的状态、
以及**针对你当前平台的具体修复命令**打印出来，不需要人去猜。

---

## 跨平台实测

| 平台 | 字体发现 | 状态 |
|---|---|---|
| Windows | `C:/Windows/Fonts` + `%LOCALAPPDATA%/Microsoft/Windows/Fonts` | ✅ 实测通过 |
| macOS | `/System/Library/Fonts`、`/Library/Fonts`、`~/Library/Fonts`（PingFang / Hiragino） | 已实现，走同一套发现逻辑 |
| Linux / 容器 | `/usr/share/fonts` 递归、`~/.local/share/fonts`、fontconfig `fc-match` 兜底 | 已实现；缺中文字体会提示 `apt install fonts-noto-cjk` |

字体全部找不到时还会尝试从 `BOOKFORGE_FONT_URL` 下载，并**校验下载到的文件
确实能渲染汉字**（画一个"中"字看位图是否为空），不合格就丢弃并降级排版，
不会假装成功。

---

## 为什么它"省 token"

这一条对 agent 场景特别重要，单独说明：

| 做法 | 效果 |
|---|---|
| **零 LLM 环节** | 抓取/提取/分组/排版/封面全是确定性代码。跑 700 篇和 7 篇，模型花费几乎一样 |
| **一条命令** | agent 不需要理解内部阶段，不用拼 5 条命令、不用传十几个参数 |
| **自动探测** | 站点类型、书名、作者、简介都自动拿，agent 不用先问用户一堆信息 |
| **机器可读摘要** | 只读 `<归档包>/reports/summary.json`（二三十行），不必翻阅几千行日志 |
| **`--json` / `--quiet`** | 前者让 stdout 只剩 JSON；后者连进度都不打 |
| **`--max N` 小跑** | 几十秒验证一遍，避免"全量跑完才发现抓错了"的返工 |
| **断点续传** | 失败后重跑同一条命令，已抓的跳过；`--force` 也走 HTML 缓存 |
| **摘要自带 `next_actions`** | 出错时直接告诉 agent 下一步该怎么修，减少一轮试探 |

对比一下典型场景：

```
❌ 传统做法
用户 → agent：帮我做电子书
agent → 用户：哪个站？什么类型？要哪些页？（3 轮对话）
agent → 用户：我抓了，但正文有导航…（再 3 轮）
（每次都把长网页内容塞进上下文）

✅ bookforge
用户 → agent：把 https://x.com 做成电子书
agent：bookforge build https://x.com -o x.epub --quiet --json
agent：读 summary.json（20 行）→ 报告成品
```

---

## 小结

* **能力层通用** —— 普通 Python CLI，谁都能调。这是 bookforge 的核心。
* **接入层可复制** —— `SKILL.md` / `AGENTS.md` 是同一份说明的不同包装。
* **封闭平台需要搭桥** —— MCP server 或 HTTP 服务；不搭桥就只能手工把成品递过去。
* **`doctor` 兜底** —— 环境问题自己会说清楚该怎么修，不让人猜。
