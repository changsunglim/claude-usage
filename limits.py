"""
limits.py - Compute live remaining usage for Claude Code plan windows.

Anthropic enforces two windows on Claude subscriptions:
  - 5-hour rolling session ("session limit")
  - 7-day rolling weekly window (separate caps per model family)

This module reads the local `turns` table (populated by scanner.py from
JSONL transcripts) and computes used/remaining/reset for both windows.

Plan caps are approximate and community-derived. Anthropic does not
publish exact token budgets, and they drift over time. The dashboard
labels values as estimates.

Claude.ai web usage shares the same plan quota but is NOT written to
local JSONL — these numbers may understate consumption for heavy web
users.
"""

import json
import os
import ssl
import subprocess
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path.home() / ".claude" / "usage.db"
PLAN_CACHE_PATH = Path.home() / ".claude" / "usage-plan-cache.json"
PLAN_CACHE_TTL_SECONDS = 24 * 3600
WEEKLY_ANCHOR_PATH = Path.home() / ".claude" / "usage-weekly-anchor.json"
WEEKLY_WINDOW = timedelta(days=7)

# Anthropic's own usage endpoint — same source `claude /usage` and the
# ClaudeKarma browser extension read. Returns ground-truth utilization
# percentages and reset times for the 5-hour and 7-day windows, so we no
# longer have to approximate them from local JSONL cost-weighting.
OFFICIAL_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OFFICIAL_USAGE_TTL_SECONDS = 60  # don't hammer the API on every 10s poll
_OFFICIAL_CACHE = {"data": None, "at": 0.0}
_OFFICIAL_LOCK = threading.Lock()

PLAN_BUDGETS = {
    "pro":    {"label": "Pro",     "weekly_all_tokens": 23_000_000},
    "max_5x": {"label": "Max 5×",  "weekly_all_tokens": 115_000_000},
    "max_20x":{"label": "Max 20×", "weekly_all_tokens": 460_000_000},
}

# Anthropic's session/weekly limits use cost-weighted tokens. Weights
# are anchored to Sonnet input = 1.0 (matches PLAN_BUDGETS Sonnet-equiv).
# Opus ~5× Sonnet, Haiku ~0.25×. cache_creation = 1.25× input,
# cache_read = 0.1× input. Reproduces `claude /usage` percentages.
CACHE_READ_WEIGHT = 0.1

MODEL_WEIGHTS = {
    "opus":   {"in": 5.0,  "out": 5.0,  "cc": 6.25,   "cr": 0.5},
    "sonnet": {"in": 1.0,  "out": 1.0,  "cc": 1.25,   "cr": 0.1},
    "haiku":  {"in": 0.25, "out": 0.25, "cc": 0.3125, "cr": 0.025},
    "other":  {"in": 1.0,  "out": 1.0,  "cc": 1.25,   "cr": 0.1},
}

DEFAULT_PLAN = "pro"


# ── Plan detection ─────────────────────────────────────────────────────────

_SSL_CTX = None


def _ssl_context():
    """SSL context that actually verifies api.anthropic.com.

    python.org / pyenv builds don't trust the macOS system keychain, so a
    plain urlopen raises CERTIFICATE_VERIFY_FAILED (which is why earlier
    API probes silently returned None). Prefer certifi's CA bundle; fall
    back to the stdlib default. Verification is never disabled — this hits
    an OAuth-bearing endpoint.
    """
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import certifi
        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


def _read_keychain_oauth():
    """Read Claude Code OAuth credentials from macOS keychain."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s",
             "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout.strip())
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None


def _fetch_plan_from_api(access_token, timeout=4):
    """Call Anthropic profile endpoint, return raw plan string or None.

    Endpoint is undocumented and may change. We try a couple of known
    paths and return the first that gives an organization_type-like field.
    """
    candidates = [
        "https://api.anthropic.com/api/oauth/profile",
        "https://api.anthropic.com/api/account",
    ]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-usage-dashboard/1.0",
    }
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                req, timeout=timeout, context=_ssl_context()
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for path in (
                ("organization", "rate_limit_tier"),
                ("organization", "organization_type"),
                ("subscription", "tier"),
                ("plan",),
                ("account", "plan"),
            ):
                cur = data
                ok = True
                for k in path:
                    if isinstance(cur, dict) and k in cur:
                        cur = cur[k]
                    else:
                        ok = False
                        break
                if ok and isinstance(cur, str):
                    return cur
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError, OSError):
            continue
    return None


def _normalize_plan(raw):
    if not raw:
        return None
    r = raw.lower().replace("-", "_").replace(" ", "_")
    if "20" in r and "max" in r:
        return "max_20x"
    if "5" in r and "max" in r:
        return "max_5x"
    if "max" in r:
        return "max_5x"
    if "pro" in r or "claude_pro" in r:
        return "pro"
    return None


def _load_cached_plan():
    try:
        with open(PLAN_CACHE_PATH) as f:
            data = json.load(f)
        if time.time() - data.get("fetched_at", 0) < PLAN_CACHE_TTL_SECONDS:
            return data.get("plan")
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_cached_plan(plan):
    try:
        PLAN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PLAN_CACHE_PATH, "w") as f:
            json.dump({"plan": plan, "fetched_at": time.time()}, f)
    except OSError:
        pass


def detect_plan():
    """Resolve the active Claude plan.

    Priority:
      1. CLAUDE_USAGE_PLAN env var (manual override)
      2. 24h-cached lookup result
      3. Live keychain + API lookup
      4. DEFAULT_PLAN

    Returns dict with keys: plan, label, source, budgets, detected_raw.
    """
    override = os.environ.get("CLAUDE_USAGE_PLAN", "").strip().lower()
    if override in PLAN_BUDGETS:
        return _plan_response(override, "env_override", raw=override)

    cached = _load_cached_plan()
    if cached in PLAN_BUDGETS:
        return _plan_response(cached, "cache", raw=cached)

    creds = _read_keychain_oauth()
    raw = None
    if creds:
        token = (creds.get("claudeAiOauth") or {}).get("accessToken")
        if token:
            raw = _fetch_plan_from_api(token)

    normalized = _normalize_plan(raw)
    if normalized in PLAN_BUDGETS:
        _save_cached_plan(normalized)
        return _plan_response(normalized, "api", raw=raw)

    return _plan_response(DEFAULT_PLAN, "default", raw=raw)


def _plan_response(plan, source, raw=None):
    b = PLAN_BUDGETS[plan]
    return {
        "plan": plan,
        "label": b["label"],
        "source": source,
        "detected_raw": raw,
        "budgets": b,
    }


# ── Official usage (Anthropic ground truth) ────────────────────────────────

def _parse_window(node):
    """Normalize one usage window (five_hour / seven_day / *_sonnet) into
    {percent, reset_at, severity}. Returns None if the node is absent."""
    if not isinstance(node, dict):
        return None
    util = node.get("utilization")
    if util is None and "percent" in node:
        util = node.get("percent")
    if util is None:
        return None
    return {
        "percent": round(float(util), 1),
        "reset_at": node.get("resets_at") or node.get("reset_at"),
        "severity": node.get("severity"),
    }


def _fetch_official_usage_uncached(timeout=8):
    """Hit Anthropic's real usage endpoint with the Claude Code OAuth token.

    Returns a dict with `available` plus normalized 5h / 7-day / scoped
    windows, or {available: False, reason: ...} on any failure (no token,
    expired token, network error, schema drift). Callers fall back to the
    local JSONL estimate when unavailable.
    """
    creds = _read_keychain_oauth()
    token = (creds or {}).get("claudeAiOauth", {}).get("accessToken") if creds else None
    if not token:
        return {"available": False, "reason": "no_oauth_token"}

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-usage-dashboard/1.0",
    }
    try:
        req = urllib.request.Request(OFFICIAL_USAGE_URL, headers=headers)
        with urllib.request.urlopen(
            req, timeout=timeout, context=_ssl_context()
        ) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reason = "token_expired" if e.code in (401, 403) else f"http_{e.code}"
        return {"available": False, "reason": reason}
    except (urllib.error.URLError, json.JSONDecodeError,
            TimeoutError, OSError) as e:
        return {"available": False, "reason": f"fetch_error:{type(e).__name__}"}

    five_hour = _parse_window(raw.get("five_hour"))
    seven_day = _parse_window(raw.get("seven_day"))
    sonnet = _parse_window(raw.get("seven_day_sonnet"))
    opus = _parse_window(raw.get("seven_day_opus"))

    # Fallback: pull from the typed `limits` array if top-level keys drift.
    if seven_day is None or five_hour is None:
        for lim in raw.get("limits") or []:
            kind = lim.get("kind")
            parsed = _parse_window(lim)
            if kind == "weekly_all" and seven_day is None:
                seven_day = parsed
            elif kind == "session" and five_hour is None:
                five_hour = parsed

    if seven_day is None:
        return {"available": False, "reason": "no_weekly_in_response"}

    return {
        "available": True,
        "five_hour": five_hour,
        "seven_day": seven_day,
        "seven_day_sonnet": sonnet,
        "seven_day_opus": opus,
    }


def fetch_official_usage(force=False):
    """Cached wrapper around the official usage endpoint (60s TTL).

    The dashboard polls /api/limits every 10s; without caching that would
    fire 6 API calls/min. One process-wide cache, guarded by a lock so the
    ThreadingHTTPServer's worker threads don't stampede the endpoint.
    """
    with _OFFICIAL_LOCK:
        now = time.time()
        cached = _OFFICIAL_CACHE["data"]
        fresh = cached and (now - _OFFICIAL_CACHE["at"]) < OFFICIAL_USAGE_TTL_SECONDS
        if fresh and not force:
            return cached
        data = _fetch_official_usage_uncached()
        # Keep serving a previously-good payload if a refresh transiently
        # fails — better a 60s-stale real number than dropping to estimate.
        if not data.get("available") and cached and cached.get("available"):
            stale = dict(cached)
            stale["stale"] = True
            stale["refresh_reason"] = data.get("reason")
            return stale
        _OFFICIAL_CACHE["data"] = data
        _OFFICIAL_CACHE["at"] = now
        return data


# ── Window calculations ────────────────────────────────────────────────────

def _connect(db_path=DB_PATH):
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _model_family(model):
    if not model:
        return "other"
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "other"


def _billable(row):
    # Per-model cost-weighted formula. PLAN_BUDGETS caps are in
    # Sonnet-equivalent units, so Opus tokens count ~5× and Haiku ~0.25×.
    w = MODEL_WEIGHTS[_model_family(row["model"])]
    return int(
        (row["input_tokens"] or 0) * w["in"]
        + (row["output_tokens"] or 0) * w["out"]
        + (row["cache_creation_tokens"] or 0) * w["cc"]
        + (row["cache_read_tokens"] or 0) * w["cr"]
    )


def _load_weekly_anchor():
    """Read persisted weekly anchor. Returns dict or None."""
    try:
        with open(WEEKLY_ANCHOR_PATH, "r") as f:
            data = json.load(f)
        if "anchor_at" in data:
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_weekly_anchor(anchor_at, source, baseline_used=0, save_at=None):
    """Persist weekly anchor to disk. `save_at` is the moment the baseline
    was captured; tokens at-or-after save_at accumulate ON TOP of baseline.
    Without save_at, baseline_used double-counts turns between anchor_at
    and the save moment."""
    try:
        WEEKLY_ANCHOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "anchor_at": anchor_at.isoformat(),
            "source": source,
            "baseline_used": int(baseline_used or 0),
        }
        if save_at is not None:
            payload["save_at"] = save_at.isoformat()
        with open(WEEKLY_ANCHOR_PATH, "w") as f:
            json.dump(payload, f)
    except OSError:
        pass


def set_weekly_anchor(anchor_at=None, baseline_used=0, source="manual",
                      save_at=None):
    """Manually set weekly anchor. `baseline_used` is the token count
    already consumed at the moment of `save_at`. When `save_at` is set,
    delta accumulates from save_at forward to avoid double-counting turns
    between anchor_at and save_at. When `save_at` is None, delta starts
    from anchor_at (legacy behavior — callers managing manual percent
    overrides should pass save_at=now() to prevent double-count)."""
    anchor_at = anchor_at or datetime.now(timezone.utc)
    _save_weekly_anchor(anchor_at, source, baseline_used, save_at)
    return anchor_at


def clear_weekly_anchor():
    """Remove manual weekly override; recomputes auto-anchored on next call."""
    try:
        WEEKLY_ANCHOR_PATH.unlink()
    except OSError:
        pass


def _earliest_turn_ts(conn, since):
    """Earliest turn timestamp at or after `since`. None if no rows."""
    row = conn.execute(
        "SELECT MIN(timestamp) AS t FROM turns WHERE timestamp >= ?",
        (since.isoformat(),),
    ).fetchone()
    if not row or not row["t"]:
        return None
    try:
        return datetime.fromisoformat(row["t"].replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _resolve_weekly_anchor(conn, now):
    """Return (anchor_at, source, baseline_used, save_at).

    Strategy:
      1. Load persisted anchor. If still within window (anchor + 7d > now),
         use it as-is including baseline_used.
      2. If expired, auto-advance: find first turn at-or-after anchor + 7d
         and treat as new anchor; baseline_used resets to 0.
      3. If no persisted anchor, fall back to earliest turn in last 7d.
    """
    persisted = _load_weekly_anchor()
    anchor = None
    source = "auto"
    baseline = 0
    save_at = None

    if persisted:
        try:
            anchor = datetime.fromisoformat(
                persisted["anchor_at"].replace("Z", "+00:00")
            )
            source = persisted.get("source", "auto")
            baseline = int(persisted.get("baseline_used", 0) or 0)
        except (ValueError, AttributeError, KeyError):
            anchor = None
        sa_raw = persisted.get("save_at") if persisted else None
        if sa_raw:
            try:
                save_at = datetime.fromisoformat(
                    sa_raw.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                save_at = None

    if anchor is None:
        fallback = _earliest_turn_ts(conn, now - WEEKLY_WINDOW)
        anchor = fallback or now
        _save_weekly_anchor(anchor, "auto", 0)
        return anchor, "auto", 0, None

    advanced = False
    while anchor + WEEKLY_WINDOW <= now:
        next_window_start = anchor + WEEKLY_WINDOW
        next_anchor = _earliest_turn_ts(conn, next_window_start)
        anchor = next_anchor if next_anchor else next_window_start
        source = "auto"
        baseline = 0
        save_at = None
        advanced = True

    if advanced:
        _save_weekly_anchor(anchor, source, baseline)
    return anchor, source, baseline, save_at


def compute_weekly(conn, now=None):
    """Compute weekly usage anchored to Anthropic's per-user reset.

    Anthropic's weekly window opens with the user's first message of
    the cycle and closes 7 days later. The anchor is per-account, so we
    can't hardcode it — we persist it locally and auto-advance when it
    expires.
    """
    now = now or datetime.now(timezone.utc)
    anchor_at, anchor_source, baseline, save_at = _resolve_weekly_anchor(conn, now)
    reset_at = anchor_at + WEEKLY_WINDOW
    delta_since = save_at or anchor_at

    rows = conn.execute(
        "SELECT model, input_tokens, output_tokens, cache_creation_tokens, "
        "       cache_read_tokens "
        "FROM turns WHERE timestamp >= ?",
        (delta_since.isoformat(),),
    ).fetchall()

    delta = 0
    by_model = {"opus": 0, "sonnet": 0, "haiku": 0, "other": 0}
    for r in rows:
        t = _billable(r)
        delta += t
        by_model[_model_family(r["model"])] += t

    total = baseline + delta

    return {
        "window_start": anchor_at.isoformat(),
        "window_end": now.isoformat(),
        "anchor_at": anchor_at.isoformat(),
        "anchor_source": anchor_source,
        "baseline_used": baseline,
        "reset_at": reset_at.isoformat(),
        "total": total,
        "by_model": by_model,
    }


# ── Public API ─────────────────────────────────────────────────────────────

def compute_efficiency_warning(conn, now=None):
    """Inspect the most-recent active session and decide whether the user
    should consider starting a fresh conversation.

    Inefficiency signals (any one triggers a warning):
      - Latest turn input_tokens > 150k → context near 200k limit
      - Last 5 turns avg cache hit rate < 40% → cache keeps invalidating
      - Last 5 turns avg billable tokens per turn > 60k → bloated context
      - Session active > 3h → diminishing returns from a single thread
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=30)).isoformat()

    row = conn.execute(
        "SELECT session_id, MAX(timestamp) AS last_ts "
        "FROM turns WHERE timestamp >= ? "
        "GROUP BY session_id ORDER BY last_ts DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    if not row:
        return {"active": False, "level": "ok", "message": None}

    session_id = row["session_id"]
    turns = conn.execute(
        "SELECT timestamp, model, input_tokens, output_tokens, "
        "       cache_creation_tokens, cache_read_tokens "
        "FROM turns WHERE session_id = ? "
        "ORDER BY timestamp DESC LIMIT 10",
        (session_id,),
    ).fetchall()
    if not turns:
        return {"active": False, "level": "ok", "message": None}

    latest = turns[0]
    latest_input = latest["input_tokens"] or 0
    latest_cache_read = latest["cache_read_tokens"] or 0
    context_size = latest_input + latest_cache_read

    recent = turns[:5]
    total_cache_read = sum((t["cache_read_tokens"] or 0) for t in recent)
    total_input = sum((t["input_tokens"] or 0) for t in recent)
    total_cache_creation = sum((t["cache_creation_tokens"] or 0) for t in recent)
    total_billable = sum(_billable(t) for t in recent)
    cache_hit_rate = (
        total_cache_read / (total_cache_read + total_input + total_cache_creation)
        if (total_cache_read + total_input + total_cache_creation) else 0
    )
    avg_billable = total_billable / len(recent)

    first_ts = conn.execute(
        "SELECT MIN(timestamp) AS t FROM turns WHERE session_id = ?",
        (session_id,),
    ).fetchone()["t"]
    try:
        anchor = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        session_age_hours = (now - anchor).total_seconds() / 3600
    except (ValueError, AttributeError):
        session_age_hours = 0

    reasons = []
    level = "ok"
    if context_size > 150_000:
        level = "warn"
        reasons.append(
            f"Context at ~{context_size//1000}k tokens (near 200k limit). "
            "Start a new session to reclaim headroom."
        )
    if cache_hit_rate < 0.40 and total_cache_creation > 50_000:
        level = "warn"
        reasons.append(
            f"Cache hit rate only {cache_hit_rate*100:.0f}% over last 5 "
            "turns — context keeps invalidating. New session will "
            "rebuild a clean cache."
        )
    if avg_billable > 60_000:
        if level == "ok":
            level = "info"
        reasons.append(
            f"Avg {int(avg_billable/1000)}k tokens/turn over last 5 "
            "turns. Conversation is heavy — consider summarizing into "
            "a new session."
        )
    if session_age_hours > 3:
        if level == "ok":
            level = "info"
        reasons.append(
            f"Session running {session_age_hours:.1f}h. Long threads "
            "drift and re-process the same history repeatedly."
        )

    return {
        "active": True,
        "session_id": session_id[:8],
        "level": level,
        "context_size": context_size,
        "cache_hit_rate": round(cache_hit_rate, 3),
        "avg_billable_per_turn": int(avg_billable),
        "session_age_hours": round(session_age_hours, 2),
        "turns_inspected": len(recent),
        "reasons": reasons,
        "message": (
            "Healthy session — keep going." if level == "ok"
            else " ".join(reasons)
        ),
    }


def get_limits(db_path=DB_PATH):
    """Return the full limits payload for the dashboard."""
    plan_info = detect_plan()
    budgets = plan_info["budgets"]

    try:
        conn = _connect(db_path)
        weekly = compute_weekly(conn)
        warning = compute_efficiency_warning(conn)
        conn.close()
    except sqlite3.Error as e:
        return {"error": str(e), "plan": plan_info}

    def pct(used, cap):
        if not cap:
            return None
        return min(100.0, round(100.0 * used / cap, 1))

    weekly_cap = budgets["weekly_all_tokens"]
    weekly_used = weekly["total"]

    weekly_all = {
        "used": weekly_used,
        "cap": weekly_cap,
        "remaining": max(0, weekly_cap - weekly_used),
        "percent": pct(weekly_used, weekly_cap),
        "by_model": weekly["by_model"],
        "by_model_pct": {
            k: pct(v, weekly_cap) for k, v in weekly["by_model"].items()
        },
        "anchor_at": weekly["anchor_at"],
        "anchor_source": weekly["anchor_source"],
        "baseline_used": weekly.get("baseline_used", 0),
        "reset_at": weekly["reset_at"],
        "source": "local_estimate",
    }

    # Prefer Anthropic's ground-truth number when the OAuth endpoint
    # answers. The local cost-weighted estimate stays as `local_percent`
    # for comparison and as the fallback when offline / token expired.
    official = fetch_official_usage()
    if official.get("available") and official.get("seven_day"):
        sd = official["seven_day"]
        weekly_all["local_percent"] = weekly_all["percent"]
        weekly_all["percent"] = sd["percent"]
        if sd.get("reset_at"):
            weekly_all["reset_at"] = sd["reset_at"]
        weekly_all["severity"] = sd.get("severity")
        weekly_all["source"] = "official_api"
        if official.get("stale"):
            weekly_all["source"] = "official_api_stale"

    return {
        "plan": plan_info,
        "weekly_all": weekly_all,
        "official": official,
        "session_health": warning,
        "weekly_claude_design": {
            "trackable": False,
            "note": "Claude Design usage is a Claude.ai web feature and "
                    "is not written to local Claude Code JSONL. Check "
                    "Settings → Usage in the Claude desktop app.",
        },
        "weekly_window_start": weekly["window_start"],
        "note": (
            "Estimates only. Anthropic does not publish exact token "
            "budgets. Claude.ai web usage (incl. Claude Design) shares "
            "the same plan quota but is not tracked locally — heavy "
            "web use will under-report here."
        ),
    }


if __name__ == "__main__":
    print(json.dumps(get_limits(), indent=2))
