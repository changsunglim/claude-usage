# Claude Code Usage Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](LICENSE)
[![claude-code](https://img.shields.io/badge/claude--code-black?style=flat-square)](https://claude.ai/code)

> **Fork notice:** This is an unofficial fork of [phuryn/claude-usage](https://github.com/phuryn/claude-usage). Not affiliated with Anthropic. See [DISCLAIMER.md](DISCLAIMER.md). Added features:
> - Live limits widget (weekly stacked bar, per-model breakdown, plan auto-detect)
> - Session Health card with new-session recommendations
> - Multi-metric Token Efficiency grader (Cache Hit + Reuse Ratio + Output Discipline + Cost Efficiency)
> - Per-message live session view (click row → see each prompt with token totals)
> - Session titles extracted from first user prompt (replaces opaque project IDs)
> - Local timezone auto-detect (was UTC-only)
> - Noise filter: skill invocations / system reminders excluded from live view
> - Bug fix: `cutoff` → `start`/`end` ReferenceError that killed charts

**Pro and Max subscribers get a progress bar. This gives you the full picture.**

Claude Code writes detailed usage logs locally — token counts, models, sessions, projects — regardless of your plan. This dashboard reads those logs and turns them into charts and cost estimates. Works on API, Pro, and Max plans.

![Claude Usage Dashboard](docs/screenshot.png)

**Created by:** [The Product Compass Newsletter](https://www.productcompass.pm)

---

## What this tracks

Works on **API, Pro, and Max plans** — Claude Code writes local usage logs regardless of subscription type. This tool reads those logs and gives you visibility that Anthropic's UI doesn't provide.

Captures usage from:
- **Claude Code CLI** (`claude` command in terminal)
- **VS Code extension** (Claude Code sidebar)
- **Dispatched Code sessions** (sessions routed through Claude Code)

**Not captured:**
- **Cowork sessions** — these run server-side and do not write local JSONL transcripts

---

## Requirements

- Python 3.8+
- No third-party packages — uses only the standard library (`sqlite3`, `http.server`, `json`, `pathlib`)

> Anyone running Claude Code already has Python installed.

## Quick Start

No `pip install`, no virtual environment, no build step.

### Windows
```
git clone https://github.com/phuryn/claude-usage
cd claude-usage
python cli.py dashboard
```

### macOS / Linux
```
git clone https://github.com/phuryn/claude-usage
cd claude-usage
python3 cli.py dashboard
```

---

## Usage

> On macOS/Linux, use `python3` instead of `python` in all commands below.

```
# Scan JSONL files and populate the database (~/.claude/usage.db)
python cli.py scan

# Show today's usage summary by model (in terminal)
python cli.py today

# Show the last 7 days (per-day breakdown + by-model totals)
python cli.py week

# Show all-time statistics (in terminal)
python cli.py stats

# Scan + open browser dashboard at http://localhost:8080
python cli.py dashboard

# Custom host and port via environment variables
HOST=0.0.0.0 PORT=9000 python cli.py dashboard

# Scan a custom projects directory
python cli.py scan --projects-dir /path/to/transcripts
```

The scanner is incremental — it tracks each file's path and modification time, so re-running `scan` is fast and only processes new or changed files.

By default, the scanner checks both `~/.claude/projects/` and the Xcode Claude integration directory (`~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig/projects/`), skipping any that don't exist. Use `--projects-dir` to scan a custom location instead.

---

## How it works

Claude Code writes one JSONL file per session to `~/.claude/projects/`. Each line is a JSON record; `assistant`-type records contain:
- `message.usage.input_tokens` — raw prompt tokens
- `message.usage.output_tokens` — generated tokens
- `message.usage.cache_creation_input_tokens` — tokens written to prompt cache
- `message.usage.cache_read_input_tokens` — tokens served from prompt cache
- `message.model` — the model used (e.g. `claude-sonnet-4-6`)

`scanner.py` parses those files and stores the data in a SQLite database at `~/.claude/usage.db`.

`dashboard.py` serves a single-page dashboard on `localhost:8080` with Chart.js charts (loaded from CDN). It auto-refreshes every 30 seconds and supports model filtering with bookmarkable URLs. The bind address and port can be overridden with `HOST` and `PORT` environment variables (defaults: `localhost`, `8080`).

---

## Cost estimates

Costs are calculated using **Anthropic API pricing as of April 2026** ([claude.com/pricing#api](https://claude.com/pricing#api)).

**Only models whose name contains `opus`, `sonnet`, or `haiku` are included in cost calculations.** Local models, unknown models, and any other model names are excluded (shown as `n/a`).

| Model | Input | Output | Cache Write | Cache Read |
|-------|-------|--------|------------|-----------|
| claude-opus-4-7 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-opus-4-6 | $5.00/MTok | $25.00/MTok | $6.25/MTok | $0.50/MTok |
| claude-sonnet-4-6 | $3.00/MTok | $15.00/MTok | $3.75/MTok | $0.30/MTok |
| claude-haiku-4-5 | $1.00/MTok | $5.00/MTok | $1.25/MTok | $0.10/MTok |

> **Note:** These are API prices. If you use Claude Code via a Max or Pro subscription, your actual cost structure is different (subscription-based, not per-token).

---

## Fork features

### Live limits widget
Top banner shows a **weekly all-models stacked bar** (Opus orange, Sonnet blue, Haiku green) with per-model breakdown beneath: `opus X% · 5.5M  sonnet Y% · 550k  haiku Z%`.

Plan auto-detection order:
1. macOS keychain → OAuth token → `api.anthropic.com/api/oauth/profile` (undocumented, may return None)
2. `CLAUDE_USAGE_PLAN` env var
3. Default `pro`

Plan budgets (approximate — Anthropic does not publish exact caps):

| Plan | Session 5h tokens | Weekly all-models |
|------|-------------------|-------------------|
| `pro` | 613k | 23M |
| `max_5x` | 3.07M | 115M |
| `max_20x` | 12.26M | 460M |

Override via `CLAUDE_USAGE_PLAN=max_5x python cli.py dashboard` or `/api/limits?plan=max_5x`.

Cache-read tokens weighted `0.1×` in billable totals to match Anthropic's cost-equivalent counting (~25% deviation in calibration).

### Session Health card
Heuristic warnings — "start new session" surfaces when any of:
- Context > 150k tokens
- Cache hit rate < 40%
- Avg > 60k tokens/turn
- Session age > 3h

States: `ok` / `info` / `warn` with colored border.

### Token Efficiency grader
A/B/C/D grade from weighted formula:
- Cache Hit 40%
- Reuse Ratio 25%
- Cost Efficiency 20%
- Output Discipline 15%

`?` info button in chart header reveals the formula. Recalcs every 30s with `/api/data`, respects date-range filter.

### Per-message live view
Click any session row → polls `/api/session/<id>` every 5s. Each user prompt shows token totals (input / output / cache_read / cache_creation), model, turn count, and tools used. Skill invocations and system reminders are filtered out so the list shows real human turns only.

### New endpoints
- `GET /api/limits?plan=<pro|max_5x|max_20x>` — weekly + session 5h budgets, plan, used/remaining
- `GET /api/session/<id>` — per-message breakdown for a session

Server uses `ThreadingHTTPServer` so concurrent `/api/data` + `/api/limits` calls don't block.

---

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Parses JSONL transcripts, writes to `~/.claude/usage.db` |
| `dashboard.py` | HTTP server + single-page HTML/JS dashboard |
| `limits.py` | Plan detection, session 5h window, rolling weekly budgets |
| `cli.py` | `scan`, `today`, `stats`, `dashboard` commands |
