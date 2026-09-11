# Changelog

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.0.0] — 通用化改造

把原来只能在中文 Windows + 手写 Python 适配器环境下使用的流水线，
改造成**任何宿主、任何平台、任何站点**都能用的独立工具。

### 新增

* **`bookforge` 单入口 CLI**：`bookforge build <网址> -o book.epub` 一条命令
  跑完「探测 → 分组 → 抓取 → 封面 → 打包」。
  子命令：`build` / `info` / `group` / `fetch` / `cover` / `pack` /
  `reorder` / `translate` / `doctor` / `adapters` / `themes`。
* **`detect.py` 站点自动探测**：按「适配器 → WordPress REST → RSS/Atom →
  sitemap → HTML 列表页」的顺序判断，并自动抽取书名 / 作者 / 简介 / 语言。
* **`groups.py` 通用分组**：策略 `category` / `date` / `path` / `flat` / `auto`；
  主题顺序与归属优先级**自动推导**（更深、更窄的分类优先）；
  `rescue_orphans()` 保证没有文章会因分组规则而丢失。
* **`site_config.py` 声明式适配器**：用 YAML/JSON 适配新站点，不必改 Python。
* **`fonts.py` 跨平台字体**：Windows / macOS / Linux / 容器全平台字体发现，
  `fc-match` 兜底，可选下载 Noto CJK（下载后会校验能否真的渲染汉字），
  找不到时给出平台对应的安装命令。
* `bookforge doctor`：依赖 / 字体 / 适配器体检，附可执行的修复指令。
* `--json`（stdout 只出 JSON）、`--quiet`（只留警告）、`--max N`（跨组抽样小跑）。
* `reports/summary.json`：给 agent 读的构建摘要，含 `warnings` 与 `next_actions`。
* 图片失败原因分类（robots / 网络 / 过大 / 过小），
  `robots.txt` 拒绝会明确提示可用 `--no-robots`。
* 标题清洗：剥掉「文章标题 - 站名」后缀。优先用探测到的站名精确匹配；
  未知站名时用**整批标题的公共尾巴**推断（不依赖 og:site_name）。
* 单元测试与 GitHub Actions CI。

### 修复

* **`extract.py` 中的 `adapter.heading_offset` 在无适配器时直接抛
  `AttributeError`** —— 也就是说**所有没有专用适配器的站点都无法抓取**。
  这个 bug 一直存在，只是因为此前只在少数手写适配器的站点上测试过而没暴露。
  已改为 `getattr(adapter, "heading_offset", 0)`。
* `sites.register()` 现在同时接受类（装饰器用法）和实例（声明式适配器）。
* `publish.publish()` 现在同时接受归档包路径与已打开的 `Archive` 对象。
* 全部模块改用包内相对导入，包内资源用 `__file__` 定位 —— 不再依赖 CWD 与
  `sys.path` 技巧，`pip install` 后可从任意目录运行。

### 变更（不兼容）

* 目录结构由 `scripts/*.py` + `forgelib/` 改为可安装的 `bookforge` 包；
  原来的 `stage1_fetch.py` 等脚本入口改为 `bookforge` 子命令。
* 站点适配器路径改为包内 `bookforge/sites.py` + `bookforge/adapters/*.yaml`。
* `--quiet` 语义修正：从"关掉颜色"改为"真正安静"。

### 保持兼容

* 归档包格式不变（`ARCHIVE_SPEC_VERSION` 仍为 `1.0`），
  旧归档包可以直接 `bookforge pack`。
* 回归验证：717 篇文章的既有归档包重新打包，得到 717 章 / 4 部 / 9 个细分类型 /
  101.7 MB / 0 张外链图 / 0 个结构问题 —— 与改造前完全一致。
