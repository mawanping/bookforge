# bookforge

**Give it a URL, get an EPUB.**

```bash
bookforge build https://example.com -o my-book.epub
```

One command runs the whole pipeline: **detect the site → build the table of
contents → fetch articles and images → generate a cover → typeset → package EPUB3.**
No LLM involved, no code to write, no files to shuffle by hand.

> 中文文档见 [README.md](README.md) · Portability analysis: [docs/PORTABILITY.md](docs/PORTABILITY.md)

---

## Why it's portable

| Concern | Approach |
|---|---|
| **Not tied to one agent** | The core is a plain Python CLI. Any host that can run shell commands can use it: WorkBuddy, Claude Code, Codex, Cursor, Gemini CLI, custom agents, cron, CI |
| **No LLM dependency** | Fetching, extraction, grouping, typesetting and covers are deterministic code. **Token cost is independent of site size** — 700 articles costs about the same as 7 |
| **Not tied to a platform** | Fonts are discovered on Windows / macOS / Linux / containers; if no CJK font exists you get an actionable install command |
| **Not tied to a site** | Auto-detects WordPress REST / RSS / sitemap / HTML index pages. New sites are adapted with **declarative YAML** — no code |
| **Not tied to a language** | CJK-aware word counting and line breaking; trailing `" - Site Name"` is stripped from titles automatically |

## Install

```bash
git clone https://github.com/<you>/bookforge.git
cd bookforge
pip install -e ".[all]"     # [all] adds PyYAML + fonttools (optional features)
bookforge doctor            # checks deps, fonts, adapters — prints fixes
```

Or run without installing:

```bash
PYTHONPATH=. python -m bookforge build https://example.com -o book.epub
```

Requires Python ≥ 3.9 and `requests`, `lxml`, `beautifulsoup4`, `markdown`,
`EbookLib`, `Pillow`, `numpy`.

## Usage

```bash
# The one command you need
bookforge build https://blog.example.com -o book.epub

# Smoke-test first (20 articles, cross-group sampled, ~1 minute)
bookforge build https://blog.example.com --max 20 -o preview.epub

# For agents/scripts: stdout is pure JSON, progress goes to stderr
bookforge build https://blog.example.com -o book.epub --quiet --json

# Different structures
bookforge build <url> --by category   # site taxonomy (default for WordPress)
bookforge build <url> --by date       # group by year
bookforge build <url> --by path       # group by first URL segment
bookforge build <url> --by flat       # chronological, no grouping

# Step by step
bookforge info <url>                            # probe only
bookforge group <url> --urls-out urls.txt       # inspect the TOC plan
bookforge fetch <url> --archive ./mybook        # fetch only
bookforge cover --archive ./mybook --style editorial
bookforge pack  --archive ./mybook --theme modern -o out.epub
bookforge reorder --archive ./mybook --order date-asc
```

## What the EPUB looks like

`--by category` on a WordPress site produces a nested TOC:

```
Part I · Projects              ← a real, clickable divider page (article + word counts)
  Chapter 1 · Web Promotion
    Some article title
    Another article title
  Chapter 2 · E-commerce
Part II · Resources
```

* Parts and chapters are **real pages**, not dead TOC labels
* A part with a single sub-type (e.g. "Essays") skips the chapter layer
* Spine order: `cover → title page → colophon → TOC → body`
* Also writes `index.md` — a grouped bibliography you can hand to someone

## Adapting a new site

An adapter only answers one question: *where is the article body in this HTML?*
Without one the generic heuristic still works, but an adapter is far more reliable.
**You don't need Python** — drop in a YAML file:

```yaml
name: myblog
hosts: [myblog.com, www.myblog.com]
content_xpath:
  - "//div[contains(@class,'entry-content')]"
title_selector: "h1.entry-title"
remove: [".sidebar", ".comments", "nav"]
discover:
  feed: https://myblog.com/feed
```

Put it in `./bookforge-adapters/`, `~/.config/bookforge/adapters/`, or
`$BOOKFORGE_ADAPTERS`. See [docs/ADDING_A_SITE.md](docs/ADDING_A_SITE.md).

## Output layout

```
<archive>/
├── content/NNN-slug.md    articles (YAML frontmatter + Markdown, number = book order)
├── assets/                downloaded, resized, deduplicated images
├── cover/                 cover.jpg / cover.png / PROMPT.txt
├── metadata/              book.json / groups.json / book-uuid.txt
├── manifest.json          single source of truth
├── index.md               grouped bibliography
├── .cache/                raw HTML cache (don't delete — makes re-runs instant)
└── reports/summary.json   ← the only file an agent needs to read
```

## License

MIT. Please only fetch content you have the right to access, and respect
`robots.txt` (the default; `--no-robots` opts out explicitly).
