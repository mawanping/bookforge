# AGENTS.md

给 coding agent（Codex / Cursor / Gemini CLI / Aider / Claude Code …）看的说明。
分两部分：**A. 用这个工具** 和 **B. 改这个仓库**。

---

## A. 用这个工具

### 唯一的主命令

```bash
bookforge build <网址> -o 书名.epub
```

一条命令跑完：站点探测 → 目录结构 → 抓正文与图片 → 封面 → 打包 EPUB3。
不需要判断站点类型，不需要手写 URL 清单，书名/作者会自动识别。

### 干活流程（照这个顺序，最省时）

```bash
bookforge doctor                                     # ① 环境体检，按提示修
bookforge build <网址> --max 20 -o 试读.epub --quiet   # ② 小跑验效果
bookforge build <网址> -o 书名.epub --quiet --json     # ③ 全量，stdout 只出 JSON
```

**读结果只读一份文件**：`<归档包>/reports/summary.json`。
里面有 `ok` / `articles` / `groups` / `epub` / `warnings` / `next_actions`，
`next_actions` 已经写好了下一步建议。不要为了诊断去翻几千行日志。

### 关键约束

* **不要一上来就全量跑**。先 `--max 20`（跨组轮流取，小跑也能验证目录结构）。
* **`--json` 时 stdout 只有 JSON**，进度在 stderr，直接解析 stdout。
* **不要用 `| head` 截断长输出** —— 会触发 SIGPIPE，让脚本在写文件中途崩掉。
  需要看长输出就重定向到文件。
* **`.cache/` 不要删** —— 里面是原始 HTML，改提取逻辑后 `--force` 重跑靠它秒回。
* **别绕开 `manifest.json` 改归档包结构** —— 所有阶段都围绕它读写。
* 抓取**默认尊重 robots.txt**、默认限速。用 `--no-robots` 前先确认合规。

### 详细文档

| 想知道 | 看 |
|---|---|
| 完整用法、开关、输出结构 | `README.md` |
| 能在哪些 agent / 平台跑，豆包这类封闭平台怎么办 | `docs/PORTABILITY.md` |
| 给新站点写适配器（不用改 Python） | `docs/ADDING_A_SITE.md` |
| 内部架构与数据流 | `docs/ARCHITECTURE.md` |
| Agent Skills 格式的说明书（WorkBuddy / Claude Code 用） | `docs/SKILL.md` |

---

## B. 改这个仓库

### 环境

```bash
pip install -e ".[dev]"
pytest -q
```

### 约定

* **对外只暴露 `bookforge/cli.py` 的 `main()`** 和少数几个高层函数
  （`fetch_stage.main()`、`publish.publish()`、`cover.render_to_archive()`、
  `groups.build_plan()`、`detect.detect()`）。新增能力优先挂到这些入口上，
  不要新增一堆并行脚本。
* **所有模块用包内相对导入**（`from .utils import log`）。不要加 `sys.path` 技巧 ——
  这个包要能被 `pip install` 后从任何目录调用。
* **包内资源用 `Path(__file__).resolve().parent / ...` 定位**，
  不要写相对 CWD 的路径（主题 CSS、适配器示例都这么处理）。
* **任何"批量修改归档包"的操作必须先整体校验再动手**，中途失败不能留下
  半成品（参考 `reorder.py` 的 `.reorder-tmp` 自愈模式）。
* **进度输出统一走 `utils.log()`**，这样 `--quiet` / `--json` 才能统一控制。
  最终结果摘要用普通 `print`，不要被 quiet 吞掉。
* **给 agent 用的新功能，要同时提供机器可读产物**（写进 `reports/*.json`），
  不要只打日志。

### 加一个站点适配器

**先考虑 YAML，不要先写 Python。** 只有在 YAML 表达不了（需要改 DOM、需要
复杂正则清洗）时才写 Python 适配器类。

* YAML：`bookforge-adapters/*.yaml`，字段见 `docs/ADDING_A_SITE.md`
* Python：`bookforge/sites.py` 里加一个 `@register` 的 `SiteAdapter` 子类

### 改提取逻辑时的正确姿势

```bash
# HTML 走缓存，秒回；不会再去打扰站点
bookforge fetch <网址> --archive <归档包> --force
```

对照 `reports/quality-report.json` 的 `failed` / `low_confidence` /
`extract_strategies` 三个字段判断改动是否有效。

### 提交前自查

```bash
pytest -q
bookforge doctor
# 拿两个真实站点各跑一遍 --max 8，确认没退步：
#   1) 有适配器的站点（如 https://1230.la/）
#   2) 没有任何适配器的站点（通用路径 —— 最容易漏测）
```

**第 2 条最容易漏。** 历史教训：`extract.py` 里 `adapter.heading_offset`
在没有适配器时是 `None.heading_offset`，直接抛异常 —— 也就是说，
**所有没有专用适配器的站点都抓不了**，而这个 bug 一直没被发现，
就是因为之前只在有适配器的站点上测过。

### 版本与兼容

* 归档包格式版本记在 `bookforge/__init__.py` 的 `ARCHIVE_SPEC_VERSION`。
  改归档包结构时必须递增，并在 `CHANGELOG.md` 里说明迁移方式。
* 保持 Python 3.9 兼容（`from __future__ import annotations` 已在用，
  但不要用 3.10+ 的 `match` / `X | Y` 运行期求值）。
