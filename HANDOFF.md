# Claude Usage Dashboard — Setup Handoff

Self-contained guide to run this tracker on a fresh Mac. No build step, no
third-party packages — Python standard library only.

---

## 1. Prerequisites

- **macOS** with Claude Code installed and used at least once (so local JSONL
  logs exist under `~/.claude/`).
- **Python 3.8+** — check with:
  ```
  python3 --version
  ```
  Ships with macOS. If missing: `brew install python`.

That's it. No `pip install`, no virtualenv.

---

## 2. Get the code

### Option A — clone from GitHub (recommended, stays updatable)
```
git clone https://github.com/changsunglim/claude-usage.git
cd claude-usage
```

### Option B — from the handoff archive
Copy `claude-usage-handoff.tar.gz` to the new Mac, then:
```
tar -xzf claude-usage-handoff.tar.gz
cd claude-usage
```

---

## 3. Run it

```
python3 cli.py dashboard
```

- Scans `~/.claude/` logs → builds local SQLite DB → opens browser at
  `http://localhost:8080`.
- Custom port: `python3 cli.py dashboard --port 9000`
- Custom projects dir: `python3 cli.py dashboard --projects-dir /path/to/.claude/projects`

CLI-only views (no browser):
```
python3 cli.py week          # last 7 days, per-day + by-model
python3 cli.py today         # today's usage
```

---

## 4. Weekly limit accuracy (read this)

The **Weekly · All Models** card estimates your Anthropic plan usage from
local logs. It is a **heuristic**, not Anthropic's official meter:

- Token cost-weights are approximate: **Opus ~5×, Sonnet 1×, Haiku ~0.25×**
  (anchored to Sonnet-equivalent plan caps in `limits.py` → `MODEL_WEIGHTS`).
- `cache_creation` weighted 1.25× input; `cache_read` 0.1×.
- Expect **±1–2 percentage points** of drift vs **Claude Settings → Usage**.

### Recalibrate when it drifts
On the weekly card:
- **Sync reset to now** — click right after Claude Settings shows a fresh 0%.
  Sets the 7-day anchor to this moment.
- **Edit…** — manually enter the anchor time + the % Claude Settings shows
  right now, if you missed the reset moment.
- **Clear** — drop the manual override, return to auto-detection.

Anchor persists at `~/.claude/usage-weekly-anchor.json`.

### Tuning the weights (optional)
If your plan consistently reads high/low on Opus-heavy or Haiku-heavy weeks,
edit `MODEL_WEIGHTS` at the top of `limits.py`. Compare the dashboard % to
`claude /usage` over a known-model week and nudge the `"in"`/`"out"` factors.

---

## 5. Plan detection

Auto-detects Pro / Max 5× / Max 20× via macOS keychain OAuth token
(`Claude Code-credentials`). If detection fails it defaults to **Pro**
(23M weekly Sonnet-equivalent tokens). Caps live in `PLAN_BUDGETS` in
`limits.py`.

---

## 6. Updating later

```
cd claude-usage
git pull origin main
```
Then re-run `python3 cli.py dashboard` (auto-rescans). Use the **Rescan**
button in the UI to force a full DB rebuild.

---

## 7. Files

| File | Purpose |
|------|---------|
| `cli.py` | Entry point: `dashboard`, `week`, `today` commands |
| `dashboard.py` | HTTP server + HTML/JS dashboard + `/api/*` endpoints |
| `scanner.py` | Reads `~/.claude/` JSONL → SQLite DB |
| `limits.py` | Weekly window math, plan detection, cost weights |
| `tests/` | `python3 -m unittest discover tests` |

DB and anchor live under `~/.claude/` — **not** in this repo, so they carry
per-machine state and are safe to delete to reset.

---

*Unofficial fork of [phuryn/claude-usage](https://github.com/phuryn/claude-usage).
Not affiliated with Anthropic. See DISCLAIMER.md.*
