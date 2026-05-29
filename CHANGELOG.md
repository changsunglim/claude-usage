# Changelog

## 2026-05-29

- Fix weekly model usage miscount: `_billable` now applies per-model cost weights (Opus ~5×, Sonnet 1×, Haiku ~0.25×) anchored to Sonnet-equivalent plan caps. Previously summed all model tokens 1:1, skewing the all-models bar and per-model breakdown on Opus-heavy weeks (cause of recurring manual edits)
- `cache_creation` now weighted 1.25× input; `cache_read` stays 0.1×
- Add accuracy disclaimer under the weekly card: estimate may drift ±1–2pp from Claude Settings → Usage; use Sync/Edit to recalibrate

## 2026-05-27

- Fix weekly all-models bar not reflecting Anthropic's per-user reset: the previous rolling 7d sum drifted after Anthropic's actual reset fired
- Anchor weekly window per install (persisted at `~/.claude/usage-weekly-anchor.json`), auto-advance after expiry by the first turn observed in the new window
- Add `Sync reset to now` button on the weekly card for manual override (use when Claude Settings → Usage shows a fresh 0%)
- Replace "7d rolling" footer text with a live countdown to the next reset, plus anchor source ("manual" / "auto")
- New POST `/api/weekly/sync-reset` endpoint

## 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
