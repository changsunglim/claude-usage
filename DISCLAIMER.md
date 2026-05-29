# Disclaimer

This is an **unofficial, community-maintained fork** of [phuryn/claude-usage](https://github.com/phuryn/claude-usage). It is **not affiliated with, endorsed by, or sponsored by Anthropic, PBC**.

## Trademarks

"Claude" and "Anthropic" are trademarks of Anthropic, PBC. Their use in this project is purely descriptive — to identify the product whose local usage logs this tool reads. No endorsement is implied.

## Plan budgets are approximations

Anthropic does not publish exact token caps for Pro, Max 5×, or Max 20× plans. The values in `limits.py` (`PLAN_BUDGETS`) are best-effort estimates calibrated against observed usage. They may be wrong. Do not rely on them for billing, capacity planning, or any decision with real financial impact.

If your real session is throttled before the bar shows full — or runs longer than the bar predicts — that is expected. The bar is a directional indicator, not a contract.

## API endpoint probing

`limits.py` attempts to read plan metadata from undocumented Anthropic endpoints (`/api/oauth/profile`, `/api/account`) using your locally-stored OAuth token. These calls:

- Are wrapped in try/except and fail silently if the endpoints change or refuse the request
- Make at most one request per 24 hours (cached at `~/.claude/usage-plan-cache.json`)
- Are skipped entirely if `CLAUDE_USAGE_PLAN` is set in your environment

If you prefer zero network calls to Anthropic, set `CLAUDE_USAGE_PLAN=pro` (or your plan) in your shell profile or launchd plist.

## No data leaves your machine

This tool reads only local files (`~/.claude/projects/*.jsonl`) and writes only to a local SQLite database (`~/.claude/usage.db`). The HTTP server binds to `localhost` by default. No telemetry. No analytics. No remote logging.

## Cost estimates

Costs shown are calculated using public Anthropic API list pricing. If you use Claude Code via a Pro or Max subscription, your actual cost is the flat subscription fee — the displayed "API equivalent cost" is informational only.

## No warranty

Provided as-is under the MIT License. See [LICENSE](LICENSE).
