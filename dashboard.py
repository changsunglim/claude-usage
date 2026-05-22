"""
dashboard.py - Local web dashboard served on localhost:8080.
"""

import json
import os
import re
import glob
import sqlite3
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timedelta, timezone

DB_PATH = Path.home() / ".claude" / "usage.db"


def _detect_tz_offset_hours():
    """Auto-detect system timezone offset in hours. Override with env TZ_OFFSET_HOURS."""
    env = os.environ.get("TZ_OFFSET_HOURS")
    if env not in (None, ""):
        try:
            return int(env)
        except ValueError:
            pass
    try:
        off = datetime.now().astimezone().utcoffset()
        if off is None:
            return 0
        return int(off.total_seconds() // 3600)
    except Exception:
        return 0


TZ_OFFSET_HOURS = _detect_tz_offset_hours()
TZ_NAME = datetime.now().astimezone().tzname() or f"UTC{TZ_OFFSET_HOURS:+d}"
SQL_TZ_SHIFT = f"'+{TZ_OFFSET_HOURS} hours'" if TZ_OFFSET_HOURS >= 0 else f"'{TZ_OFFSET_HOURS} hours'"

PROJECTS_DIRS = [
    Path.home() / ".claude" / "projects",
    Path.home() / "Library" / "Developer" / "Xcode" / "CodingAssistant" / "ClaudeAgentConfig" / "projects",
]


def _shift_iso(ts, hours=TZ_OFFSET_HOURS):
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return ts


_TITLE_CACHE = {}  # session_id -> (mtime, title, project_path, jsonl_path)


def _find_session_jsonl(session_id):
    for d in PROJECTS_DIRS:
        if not d.exists():
            continue
        for p in d.rglob(f"{session_id}.jsonl"):
            return p
    return None


def _extract_title(jsonl_path):
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "user":
                    continue
                c = msg.get("content")
                text = None
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    for it in c:
                        if isinstance(it, dict) and it.get("type") == "text":
                            text = it.get("text")
                            break
                if not text:
                    continue
                t = text.strip()
                if t.startswith("<") or t.startswith("Caveat:") or t.startswith("[Request"):
                    continue
                t = " ".join(t.split())
                return t[:80] + ("..." if len(t) > 80 else "")
    except Exception:
        pass
    return ""


def get_session_titles(session_ids):
    out = {}
    for sid in session_ids:
        p = _find_session_jsonl(sid)
        if not p:
            out[sid] = ""
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            out[sid] = ""
            continue
        cached = _TITLE_CACHE.get(sid)
        if cached and cached[0] == mtime:
            out[sid] = cached[1]
        else:
            title = _extract_title(p)
            _TITLE_CACHE[sid] = (mtime, title, str(p.parent), str(p))
            out[sid] = title
    return out


def get_dashboard_data(db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found. Run: python cli.py scan"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── All models (for filter UI) ────────────────────────────────────────────
    model_rows = conn.execute("""
        SELECT COALESCE(model, 'unknown') as model
        FROM turns
        GROUP BY model
        ORDER BY SUM(input_tokens + output_tokens) DESC
    """).fetchall()
    all_models = [r["model"] for r in model_rows]

    # ── Daily per-model, ALL history (client filters by range) ────────────────
    daily_rows = conn.execute(f"""
        SELECT
            substr(datetime(timestamp, {SQL_TZ_SHIFT}), 1, 10)   as day,
            COALESCE(model, 'unknown') as model,
            SUM(input_tokens)          as input,
            SUM(output_tokens)         as output,
            SUM(cache_read_tokens)     as cache_read,
            SUM(cache_creation_tokens) as cache_creation,
            COUNT(*)                   as turns
        FROM turns
        GROUP BY day, model
        ORDER BY day, model
    """).fetchall()

    daily_by_model = [{
        "day":            r["day"],
        "model":          r["model"],
        "input":          r["input"] or 0,
        "output":         r["output"] or 0,
        "cache_read":     r["cache_read"] or 0,
        "cache_creation": r["cache_creation"] or 0,
        "turns":          r["turns"] or 0,
    } for r in daily_rows]

    # ── Hourly per-day per-model (client filters by range + TZ-shifts) ────────
    # Timestamps are ISO8601 UTC (e.g. "2026-04-08T09:30:00Z"); chars 12-13 = hour.
    hourly_rows = conn.execute(f"""
        SELECT
            substr(datetime(timestamp, {SQL_TZ_SHIFT}), 1, 10)                  as day,
            CAST(substr(datetime(timestamp, {SQL_TZ_SHIFT}), 12, 2) AS INTEGER) as hour,
            COALESCE(model, 'unknown')                as model,
            SUM(output_tokens)                        as output,
            COUNT(*)                                  as turns
        FROM turns
        WHERE timestamp IS NOT NULL AND length(timestamp) >= 13
        GROUP BY day, hour, model
        ORDER BY day, hour, model
    """).fetchall()

    hourly_by_model = [{
        "day":    r["day"],
        "hour":   r["hour"] if r["hour"] is not None else 0,
        "model":  r["model"],
        "output": r["output"] or 0,
        "turns":  r["turns"] or 0,
    } for r in hourly_rows]

    # ── All sessions (client filters by range and model) ──────────────────────
    session_rows = conn.execute("""
        SELECT
            session_id, project_name, first_timestamp, last_timestamp,
            total_input_tokens, total_output_tokens,
            total_cache_read, total_cache_creation, model, turn_count,
            git_branch
        FROM sessions
        ORDER BY last_timestamp DESC
    """).fetchall()

    sessions_all = []
    raw_ids = [r["session_id"] for r in session_rows]
    titles = get_session_titles(raw_ids[:50])  # title lookup limited to recent 50
    for r in session_rows:
        try:
            t1 = datetime.fromisoformat(r["first_timestamp"].replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(r["last_timestamp"].replace("Z", "+00:00"))
            duration_min = round((t2 - t1).total_seconds() / 60, 1)
        except Exception:
            duration_min = 0
        last_shifted = _shift_iso(r["last_timestamp"])
        sessions_all.append({
            "session_id":    r["session_id"][:8],
            "session_id_full": r["session_id"],
            "title":         titles.get(r["session_id"], ""),
            "project":       r["project_name"] or "unknown",
            "branch":        r["git_branch"] or "",
            "last":          last_shifted[:16].replace("T", " "),
            "last_date":     last_shifted[:10],
            "duration_min":  duration_min,
            "model":         r["model"] or "unknown",
            "turns":         r["turn_count"] or 0,
            "input":         r["total_input_tokens"] or 0,
            "output":        r["total_output_tokens"] or 0,
            "cache_read":    r["total_cache_read"] or 0,
            "cache_creation": r["total_cache_creation"] or 0,
        })

    conn.close()

    # Project label map: prefer cleaner names. For opaque project IDs (UUIDs/paths
    # ending in random hex), use the most recent session title.
    project_labels = {}
    import re as _re
    uuid_re = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", _re.I)
    for s in sessions_all:
        proj = s["project"]
        if proj in project_labels:
            continue
        looks_opaque = (
            uuid_re.search(proj) is not None
            or proj.count("/") > 1
            or proj == "unknown"
        )
        if looks_opaque and s.get("title"):
            project_labels[proj] = s["title"][:50]
        else:
            # Use basename of path
            base = proj.rstrip("/").split("/")[-1] if "/" in proj else proj
            project_labels[proj] = base or proj

    return {
        "all_models":      all_models,
        "daily_by_model":  daily_by_model,
        "hourly_by_model": hourly_by_model,
        "sessions_all":    sessions_all,
        "project_labels":  project_labels,
        "tz_name":         TZ_NAME,
        "tz_offset_hours": TZ_OFFSET_HOURS,
        "generated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _is_real_user_message(msg):
    """True if message is an actual human prompt (not tool_result)."""
    if not isinstance(msg, dict):
        return False
    if msg.get("role") != "user":
        return False
    c = msg.get("content")
    if isinstance(c, str):
        return c.strip() != ""
    if isinstance(c, list):
        for it in c:
            if isinstance(it, dict):
                if it.get("type") == "tool_result":
                    return False
                if it.get("type") == "text" and it.get("text", "").strip():
                    return True
        return False
    return False


def _msg_text(msg):
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for it in c:
            if isinstance(it, dict) and it.get("type") == "text":
                parts.append(it.get("text", ""))
        return " ".join(parts)
    return ""


_NOISE_TAG_RE = re.compile(
    r"<(command-message|command-name|command-args|command-stdout|command-output|"
    r"local-command-stdout|local-command-output|system-reminder|bash-input|bash-stdout|"
    r"bash-stderr|user-prompt-submit-hook)\b[^>]*>.*?</\1>",
    re.DOTALL,
)


def _is_noise_prompt(text):
    """True if prompt body is only Claude Code wrapper tags (skill invocations, hook injections, system reminders)."""
    stripped = _NOISE_TAG_RE.sub("", text or "").strip()
    return stripped == ""


def parse_session_messages(jsonl_path):
    """Group turns by user message. Returns list of {prompt, timestamp, input, output, cache_read, cache_creation, model, turn_count}."""
    groups = []
    current = None
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                ts = d.get("timestamp", "")
                msg = d.get("message")
                if t == "user" and _is_real_user_message(msg):
                    text = _msg_text(msg).strip()
                    if _is_noise_prompt(text):
                        # Skill invocations / system reminders aren't real user turns —
                        # let following assistant tokens accumulate on prior prompt.
                        continue
                    text = " ".join(text.split())
                    current = {
                        "prompt": text[:200] + ("..." if len(text) > 200 else ""),
                        "full_prompt": text,
                        "timestamp": _shift_iso(ts),
                        "input": 0,
                        "output": 0,
                        "cache_read": 0,
                        "cache_creation": 0,
                        "model": "",
                        "turn_count": 0,
                        "tools": [],
                    }
                    groups.append(current)
                elif t == "assistant" and current is not None and isinstance(msg, dict):
                    usage = msg.get("usage") or {}
                    current["input"] += usage.get("input_tokens", 0) or 0
                    current["output"] += usage.get("output_tokens", 0) or 0
                    current["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                    current["cache_creation"] += usage.get("cache_creation_input_tokens", 0) or 0
                    current["turn_count"] += 1
                    if not current["model"]:
                        current["model"] = msg.get("model", "") or ""
                    c = msg.get("content")
                    if isinstance(c, list):
                        for it in c:
                            if isinstance(it, dict) and it.get("type") == "tool_use":
                                name = it.get("name", "")
                                if name and name not in current["tools"]:
                                    current["tools"].append(name)
    except Exception:
        pass
    return groups


def get_session_live(session_id, db_path=DB_PATH):
    if not db_path.exists():
        return {"error": "Database not found"}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Resolve full id from prefix
    row = conn.execute(
        "SELECT session_id, project_name, model, turn_count, first_timestamp, last_timestamp, "
        "total_input_tokens, total_output_tokens, total_cache_read, total_cache_creation, git_branch "
        "FROM sessions WHERE session_id = ? OR session_id LIKE ? LIMIT 1",
        (session_id, session_id + "%"),
    ).fetchone()
    if not row:
        conn.close()
        return {"error": f"Session not found: {session_id}"}
    full_id = row["session_id"]
    conn.close()
    jsonl = _find_session_jsonl(full_id)
    messages = parse_session_messages(jsonl) if jsonl else []
    titles = get_session_titles([full_id])
    return {
        "session_id": full_id,
        "title": titles.get(full_id, ""),
        "project": row["project_name"] or "unknown",
        "branch": row["git_branch"] or "",
        "model": row["model"] or "unknown",
        "turn_count": row["turn_count"] or 0,
        "first": _shift_iso(row["first_timestamp"]),
        "last": _shift_iso(row["last_timestamp"]),
        "totals": {
            "input": row["total_input_tokens"] or 0,
            "output": row["total_output_tokens"] or 0,
            "cache_read": row["total_cache_read"] or 0,
            "cache_creation": row["total_cache_creation"] or 0,
        },
        "messages": messages,
        "generated_at": (datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)).strftime("%Y-%m-%d %H:%M:%S"),
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Usage Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2d3a;
    --text: #e2e8f0;
    --muted: #8892a4;
    --accent: #d97757;
    --blue: #4f8ef7;
    --green: #4ade80;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 14px; }

  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--accent); }
  header .meta { color: var(--muted); font-size: 12px; }
  #rescan-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 4px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 4px; }
  #rescan-btn:hover { color: var(--text); border-color: var(--accent); }

  /* Live limits banner */
  #limits-banner { background: var(--card); border-bottom: 1px solid var(--border); padding: 12px 24px; display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; align-items: stretch; }
  .limit-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; position: relative; }
  .limit-card .lc-head { display: flex; justify-content: space-between; align-items: baseline; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .limit-card .lc-pct { color: var(--text); font-weight: 600; font-size: 12px; letter-spacing: 0; text-transform: none; }
  .limit-card .lc-bar { height: 8px; background: rgba(255,255,255,0.05); border-radius: 4px; overflow: hidden; display: flex; }
  .limit-card .lc-fill { height: 100%; width: 0%; background: var(--green); transition: width 0.4s ease, background 0.4s; }
  .limit-card .lc-fill.warn { background: #f59e0b; }
  .limit-card .lc-fill.danger { background: #ef4444; }
  .limit-card .lc-seg { height: 100%; transition: width 0.4s ease; }
  .limit-card .lc-foot { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); margin-top: 6px; }
  .limit-card .lc-foot strong { color: var(--text); font-weight: 600; }
  .lc-models { display: flex; gap: 10px; margin-top: 6px; font-size: 10px; color: var(--muted); flex-wrap: wrap; }
  .lc-models .lc-model { display: inline-flex; align-items: center; gap: 4px; }
  .lc-models .lc-swatch { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
  #lc-health { padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; font-size: 12px; line-height: 1.4; }
  #lc-health.health-ok { border-color: rgba(74,222,128,0.3); }
  #lc-health.health-info { border-color: #f59e0b; background: rgba(245,158,11,0.06); }
  #lc-health.health-warn { border-color: #ef4444; background: rgba(239,68,68,0.08); }
  #lc-health .lc-head { display: flex; justify-content: space-between; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  #lc-health .lc-msg { color: var(--text); }
  #lc-health.health-ok .lc-msg { color: var(--muted); }
  #lc-health .lc-stats { font-size: 10px; color: var(--muted); margin-top: 6px; display: flex; gap: 10px; flex-wrap: wrap; }
  #limits-meta { padding: 6px 24px 0; font-size: 11px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; align-items: center; }
  #plan-select { background: var(--card); border: 1px solid var(--border); color: var(--text); border-radius: 4px; padding: 2px 6px; font-size: 11px; }
  .lc-disabled { opacity: 0.5; }
  #rescan-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  #filter-bar { background: var(--card); border-bottom: 1px solid var(--border); padding: 10px 24px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .filter-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); white-space: nowrap; }
  .filter-sep { width: 1px; height: 22px; background: var(--border); flex-shrink: 0; }
  #model-checkboxes { display: flex; flex-wrap: wrap; gap: 6px; }
  .model-cb-label { display: flex; align-items: center; gap: 5px; padding: 3px 10px; border-radius: 20px; border: 1px solid var(--border); cursor: pointer; font-size: 12px; color: var(--muted); transition: border-color 0.15s, color 0.15s, background 0.15s; user-select: none; }
  .model-cb-label:hover { border-color: var(--accent); color: var(--text); }
  .model-cb-label.checked { background: rgba(217,119,87,0.12); border-color: var(--accent); color: var(--text); }
  .model-cb-label input { display: none; }
  .filter-btn { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: transparent; color: var(--muted); font-size: 11px; cursor: pointer; white-space: nowrap; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .range-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; flex-shrink: 0; }
  .range-btn { padding: 4px 13px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 12px; cursor: pointer; transition: background 0.15s, color 0.15s; }
  .range-btn:last-child { border-right: none; }
  .range-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .range-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); font-weight: 600; }

  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 24px; }
  .stat-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .stat-card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }
  .stat-card .value { font-size: 22px; font-weight: 700; }
  .stat-card .sub { color: var(--muted); font-size: 11px; margin-top: 4px; }

  .charts-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
  .chart-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
  .chart-card.wide { grid-column: 1 / -1; }
  .chart-card h2 { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .chart-wrap { position: relative; height: 240px; }
  .chart-wrap.tall { height: 300px; }
  .chart-header { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; }
  .chart-header h2 { margin-bottom: 0; }
  .chart-header-right { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chart-day-count { font-size: 11px; color: var(--muted); }
  .tz-group { display: flex; border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
  .tz-btn { padding: 3px 10px; background: transparent; border: none; border-right: 1px solid var(--border); color: var(--muted); font-size: 11px; cursor: pointer; transition: background 0.15s, color 0.15s; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  .tz-btn:last-child { border-right: none; }
  .tz-btn:hover { background: rgba(255,255,255,0.04); color: var(--text); }
  .tz-btn.active { background: rgba(217,119,87,0.15); color: var(--accent); }
  .peak-legend { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
  .peak-swatch { width: 10px; height: 10px; background: rgba(248,113,113,0.8); border-radius: 2px; display: inline-block; }

  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); border-bottom: 1px solid var(--border); white-space: nowrap; }
  th.sortable { cursor: pointer; user-select: none; }
  th.sortable:hover { color: var(--text); }
  .sort-icon { font-size: 9px; opacity: 0.8; }
  td { padding: 10px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }
  .model-tag { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px; background: rgba(79,142,247,0.15); color: var(--blue); }
  .cost { color: var(--green); font-family: monospace; }
  .cost-na { color: var(--muted); font-family: monospace; font-size: 11px; }
  .num { font-family: monospace; }
  .muted { color: var(--muted); }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .section-header .section-title { margin-bottom: 0; }
  .export-btn { background: var(--card); border: 1px solid var(--border); color: var(--muted); padding: 3px 10px; border-radius: 5px; cursor: pointer; font-size: 11px; }
  .export-btn:hover { color: var(--text); border-color: var(--accent); }
  .table-card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 24px; overflow-x: auto; }

  footer { border-top: 1px solid var(--border); padding: 20px 24px; margin-top: 8px; }
  .footer-content { max-width: 1400px; margin: 0 auto; }
  .footer-content p { color: var(--muted); font-size: 12px; line-height: 1.7; margin-bottom: 4px; }
  .footer-content p:last-child { margin-bottom: 0; }
  .footer-content a { color: var(--blue); text-decoration: none; }
  .footer-content a:hover { text-decoration: underline; }

  @media (max-width: 768px) { .charts-grid { grid-template-columns: 1fr; } .chart-card.wide { grid-column: 1; } }
</style>
</head>
<body>
<header>
  <h1>Claude Code Usage Dashboard</h1>
  <div class="meta" id="meta">Loading...</div>
  <button id="rescan-btn" onclick="triggerRescan()" title="Rebuild the database from scratch by re-scanning all JSONL files. Use if data looks stale or costs seem wrong.">&#x21bb; Rescan</button>
</header>

<div id="limits-meta">
  <div>
    Plan:
    <select id="plan-select" onchange="onPlanOverride()">
      <option value="">Auto-detect</option>
      <option value="pro">Pro</option>
      <option value="max_5x">Max 5×</option>
      <option value="max_20x">Max 20×</option>
    </select>
    <span id="plan-source" style="margin-left:8px"></span>
  </div>
  <div title="Only weekly all-models usage syncs reliably with Claude Settings → Usage. 5h session and Claude Design are computed server-side by Anthropic with an undocumented formula and can't be reproduced from local JSONL.">
    Weekly all-models only · session &amp; Claude Design not trackable locally
  </div>
</div>
<div id="limits-banner">
  <div class="limit-card" id="lc-weekly">
    <div class="lc-head"><span>Weekly · All Models</span><span class="lc-pct">—</span></div>
    <div class="lc-bar"><div class="lc-fill"></div></div>
    <div class="lc-foot"><span class="lc-used">— / —</span><span class="lc-reset">7d rolling</span></div>
    <div class="lc-models" id="lc-weekly-models"></div>
  </div>
  <div id="lc-health" class="health-ok">
    <div class="lc-head"><span>Session Health</span><span id="health-session-id" style="text-transform:none;letter-spacing:0">—</span></div>
    <div class="lc-msg" id="health-msg">Loading…</div>
    <div class="lc-stats" id="health-stats"></div>
  </div>
</div>

<div id="filter-bar">
  <div class="filter-label">Models</div>
  <div id="model-checkboxes"></div>
  <button class="filter-btn" onclick="selectAllModels()">All</button>
  <button class="filter-btn" onclick="clearAllModels()">None</button>
  <div class="filter-sep"></div>
  <div class="filter-label">Range</div>
  <div class="range-group">
    <button class="range-btn" data-range="week" onclick="setRange('week')">This Week</button>
    <button class="range-btn" data-range="month" onclick="setRange('month')">This Month</button>
    <button class="range-btn" data-range="prev-month" onclick="setRange('prev-month')">Prev Month</button>
    <button class="range-btn" data-range="7d"  onclick="setRange('7d')">7d</button>
    <button class="range-btn" data-range="30d" onclick="setRange('30d')">30d</button>
    <button class="range-btn" data-range="90d" onclick="setRange('90d')">90d</button>
    <button class="range-btn" data-range="all" onclick="setRange('all')">All</button>
  </div>
</div>

<div class="container">
  <div class="stats-row" id="stats-row"></div>
  <div class="charts-grid">
    <div class="chart-card wide">
      <h2 id="daily-chart-title">Daily Token Usage</h2>
      <div class="chart-wrap tall"><canvas id="chart-daily"></canvas></div>
    </div>
    <div class="chart-card wide">
      <div class="chart-header">
        <h2 id="hourly-chart-title">Average Hourly Distribution</h2>
        <div class="chart-header-right">
          <span class="peak-legend" title="Mon–Fri 05:00–11:00 PT — Anthropic peak-hour throttling window"><span class="peak-swatch"></span>Peak hours (PT)</span>
          <span class="chart-day-count" id="hourly-day-count"></span>
          <div class="tz-group">
            <button class="tz-btn" data-tz="local" onclick="setHourlyTZ('local')">Local</button>
            <button class="tz-btn" data-tz="utc"   onclick="setHourlyTZ('utc')">UTC</button>
          </div>
        </div>
      </div>
      <div class="chart-wrap"><canvas id="chart-hourly"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Token Efficiency
        <button onclick="toggleEffHelp()" title="How is this graded?" style="background:none;border:1px solid var(--border);color:var(--muted);width:18px;height:18px;border-radius:50%;font-size:11px;cursor:pointer;margin-left:6px;line-height:1;padding:0">?</button>
        <span id="efficiency-grade" style="float:right;font-size:14px"></span>
      </h2>
      <div id="eff-help" style="display:none;background:rgba(255,255,255,0.03);border:1px solid var(--border);border-radius:6px;padding:10px;margin:6px 0;font-size:11px;color:var(--muted);line-height:1.5">
        <strong style="color:var(--text)">Grade = weighted score of 4 sub-metrics over selected range:</strong>
        <ul style="margin:6px 0 0 16px;padding:0">
          <li><strong>Cache Hit Rate (40%):</strong> cache_read / (cache_read + cache_creation + fresh_input). Higher = more reuse.</li>
          <li><strong>Reuse Ratio (25%):</strong> cache_read / cache_creation. 4×+ = full credit. &lt;1× = wasted writes.</li>
          <li><strong>Output Discipline (15%):</strong> penalty if output / total_input &gt; ~30%. Chatty responses hurt.</li>
          <li><strong>Cost Efficiency (20%):</strong> 1 - actual_cost / no_cache_cost. Uses Anthropic prices (cache_read 0.1×, cache_write 1.25×).</li>
        </ul>
        <div style="margin-top:6px">Updates on every dashboard refresh (every 30s). Pinned to the date range filter above — change the range, score recalcs.</div>
      </div>
      <div class="chart-wrap"><canvas id="chart-efficiency"></canvas></div>
    </div>
    <div class="chart-card">
      <h2>Top Projects by Tokens</h2>
      <div class="chart-wrap"><canvas id="chart-project"></canvas></div>
    </div>
  </div>
  <div class="table-card" id="efficiency-card">
    <div class="section-title">Efficiency Breakdown</div>
    <div id="efficiency-body" style="padding:14px;display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px"></div>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Recent Sessions <span style="color:var(--muted);font-weight:400;font-size:11px">(click row to watch live)</span></div><button class="export-btn" onclick="exportSessionsCSV()" title="Export all filtered sessions to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Session</th>
        <th>Title</th>
        <th>Project</th>
        <th class="sortable" onclick="setSessionSort('last')">Last Active <span class="sort-icon" id="sort-icon-last"></span></th>
        <th class="sortable" onclick="setSessionSort('duration_min')">Duration <span class="sort-icon" id="sort-icon-duration_min"></span></th>
        <th>Model</th>
        <th class="sortable" onclick="setSessionSort('turns')">Turns <span class="sort-icon" id="sort-icon-turns"></span></th>
        <th class="sortable" onclick="setSessionSort('input')">Input <span class="sort-icon" id="sort-icon-input"></span></th>
        <th class="sortable" onclick="setSessionSort('output')">Output <span class="sort-icon" id="sort-icon-output"></span></th>
        <th class="sortable" onclick="setSessionSort('cost')">Est. Cost <span class="sort-icon" id="sort-icon-cost"></span></th>
      </tr></thead>
      <tbody id="sessions-body"></tbody>
    </table>
  </div>

  <div class="table-card" id="live-session-card" style="display:none">
    <div class="section-header">
      <div class="section-title">Live Session: <span id="live-title" style="color:var(--muted);font-weight:400"></span></div>
      <div>
        <span id="live-status" style="color:var(--green);font-size:11px;margin-right:8px">&#x25CF; polling 5s</span>
        <button class="export-btn" onclick="stopLiveSession()">Close</button>
      </div>
    </div>
    <div id="live-summary" style="padding:10px 14px;color:var(--muted);font-size:12px;border-bottom:1px solid var(--border)"></div>
    <table>
      <thead><tr>
        <th style="width:50px">#</th>
        <th>Time <span id="live-tz-label" class="muted" style="font-weight:normal">(local)</span></th>
        <th>Your Message</th>
        <th class="num">Turns</th>
        <th class="num">Tools</th>
        <th class="num">Input</th>
        <th class="num">Output</th>
        <th class="num">Cache R</th>
        <th class="num">Cache W</th>
        <th class="num">Cost</th>
      </tr></thead>
      <tbody id="live-turns-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project</div><button class="export-btn" onclick="exportProjectsCSV()" title="Export all projects to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th class="sortable" onclick="setProjectSort('sessions')">Sessions <span class="sort-icon" id="psort-sessions"></span></th>
        <th class="sortable" onclick="setProjectSort('turns')">Turns <span class="sort-icon" id="psort-turns"></span></th>
        <th class="sortable" onclick="setProjectSort('input')">Input <span class="sort-icon" id="psort-input"></span></th>
        <th class="sortable" onclick="setProjectSort('output')">Output <span class="sort-icon" id="psort-output"></span></th>
        <th class="sortable" onclick="setProjectSort('cost')">Est. Cost <span class="sort-icon" id="psort-cost"></span></th>
      </tr></thead>
      <tbody id="project-cost-body"></tbody>
    </table>
  </div>
  <div class="table-card">
    <div class="section-header"><div class="section-title">Cost by Project &amp; Branch</div><button class="export-btn" onclick="exportProjectBranchCSV()" title="Export project+branch breakdown to CSV">&#x2913; CSV</button></div>
    <table>
      <thead><tr>
        <th>Project</th>
        <th>Branch</th>
        <th class="sortable" onclick="setProjectBranchSort('sessions')">Sessions <span class="sort-icon" id="pbsort-sessions"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('turns')">Turns <span class="sort-icon" id="pbsort-turns"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('input')">Input <span class="sort-icon" id="pbsort-input"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('output')">Output <span class="sort-icon" id="pbsort-output"></span></th>
        <th class="sortable" onclick="setProjectBranchSort('cost')">Est. Cost <span class="sort-icon" id="pbsort-cost"></span></th>
      </tr></thead>
      <tbody id="project-branch-cost-body"></tbody>
    </table>
  </div>
</div>

<footer>
  <div class="footer-content">
    <p>Cost estimates based on Anthropic API pricing (<a href="https://claude.com/pricing#api" target="_blank">claude.com/pricing#api</a>) as of April 2026. Only models containing <em>opus</em>, <em>sonnet</em>, or <em>haiku</em> in the name are included in cost calculations. Actual costs for Max/Pro subscribers differ from API pricing.</p>
    <p>
      GitHub: <a href="https://github.com/phuryn/claude-usage" target="_blank">https://github.com/phuryn/claude-usage</a>
      &nbsp;&middot;&nbsp;
      Created by: <a href="https://www.productcompass.pm" target="_blank">The Product Compass Newsletter</a>
      &nbsp;&middot;&nbsp;
      License: MIT
    </p>
  </div>
</footer>

<script>
// ── Helpers ────────────────────────────────────────────────────────────────
function esc(s) {
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

// ── State ──────────────────────────────────────────────────────────────────
let rawData = null;
let selectedModels = new Set();
let selectedRange = '30d';
let charts = {};
let sessionSortCol = 'last';
let modelSortCol = 'cost';
let modelSortDir = 'desc';
let projectSortCol = 'cost';
let projectSortDir = 'desc';
let branchSortCol = 'cost';
let branchSortDir = 'desc';
let lastFilteredSessions = [];
let lastByProject = [];
let lastByProjectBranch = [];
let sessionSortDir = 'desc';
let hourlyTZ = 'local';  // 'local' or 'utc'

// ── Peak-hour config ───────────────────────────────────────────────────────
// Anthropic throttles Mon–Fri 05:00–11:00 PT. We approximate as fixed UTC hours
// 12–17 (matches PDT; during PST the window shifts by 1h — accepted simplification).
const PEAK_HOURS_UTC = new Set([12, 13, 14, 15, 16, 17]);

// Local-timezone offset in hours (signed). Fractional offsets (e.g. India UTC+5:30)
// are rounded to the nearest hour for bucket alignment.
function localOffsetHours() {
  return Math.round(-new Date().getTimezoneOffset() / 60);
}

// Return the UTC hour (0–23) corresponding to a displayed-hour bucket.
function displayHourToUTC(displayHour, tzMode) {
  if (tzMode === 'utc') return displayHour;
  return ((displayHour - localOffsetHours()) % 24 + 24) % 24;
}

// Return the displayed-hour bucket for a UTC hour.
function utcHourToDisplay(utcHour, tzMode) {
  if (tzMode === 'utc') return utcHour;
  return ((utcHour + localOffsetHours()) % 24 + 24) % 24;
}

function isPeakHour(displayHour, tzMode) {
  return PEAK_HOURS_UTC.has(displayHourToUTC(displayHour, tzMode));
}

function formatHourLabel(h) {
  return String(h).padStart(2, '0') + ':00';
}

function tzDisplayName(tzMode) {
  if (tzMode === 'utc') return 'UTC';
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'Local';
  } catch(e) {
    return 'Local';
  }
}

// ── Pricing (Anthropic API, April 2026) ────────────────────────────────────
const PRICING = {
  'claude-opus-4-7':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-6':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-opus-4-5':   { input:  5.00, output: 25.00, cache_write:  6.25, cache_read: 0.50 },
  'claude-sonnet-4-7': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-6': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-sonnet-4-5': { input:  3.00, output: 15.00, cache_write:  3.75, cache_read: 0.30 },
  'claude-haiku-4-7':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-6':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
  'claude-haiku-4-5':  { input:  1.00, output:  5.00, cache_write:  1.25, cache_read: 0.10 },
};

function isBillable(model) {
  if (!model) return false;
  const m = model.toLowerCase();
  return m.includes('opus') || m.includes('sonnet') || m.includes('haiku');
}

function getPricing(model) {
  if (!model) return null;
  if (PRICING[model]) return PRICING[model];
  for (const key of Object.keys(PRICING)) {
    if (model.startsWith(key)) return PRICING[key];
  }
  const m = model.toLowerCase();
  if (m.includes('opus'))   return PRICING['claude-opus-4-7'];
  if (m.includes('sonnet')) return PRICING['claude-sonnet-4-6'];
  if (m.includes('haiku'))  return PRICING['claude-haiku-4-5'];
  return null;
}

function calcCost(model, inp, out, cacheRead, cacheCreation) {
  if (!isBillable(model)) return 0;
  const p = getPricing(model);
  if (!p) return 0;
  return (
    inp           * p.input       / 1e6 +
    out           * p.output      / 1e6 +
    cacheRead     * p.cache_read  / 1e6 +
    cacheCreation * p.cache_write / 1e6
  );
}

// ── Formatting ─────────────────────────────────────────────────────────────
function fmt(n) {
  if (n >= 1e9) return (n/1e9).toFixed(2)+'B';
  if (n >= 1e6) return (n/1e6).toFixed(2)+'M';
  if (n >= 1e3) return (n/1e3).toFixed(1)+'K';
  return n.toLocaleString();
}
function fmtCost(c)    { return '$' + c.toFixed(4); }
function fmtCostBig(c) { return '$' + c.toFixed(2); }

// ── Chart colors ───────────────────────────────────────────────────────────
const TOKEN_COLORS = {
  input:          'rgba(79,142,247,0.8)',
  output:         'rgba(167,139,250,0.8)',
  cache_read:     'rgba(74,222,128,0.6)',
  cache_creation: 'rgba(251,191,36,0.6)',
};
const MODEL_COLORS = ['#d97757','#4f8ef7','#4ade80','#a78bfa','#fbbf24','#f472b6','#34d399','#60a5fa'];

// ── Time range ─────────────────────────────────────────────────────────────
const RANGE_LABELS = { 'week': 'This Week', 'month': 'This Month', 'prev-month': 'Previous Month', '7d': 'Last 7 Days', '30d': 'Last 30 Days', '90d': 'Last 90 Days', 'all': 'All Time' };
const RANGE_TICKS  = { 'week': 7, 'month': 15, 'prev-month': 15, '7d': 7, '30d': 15, '90d': 13, 'all': 12 };
const VALID_RANGES = Object.keys(RANGE_LABELS);

function rangeIncludesToday(range) {
  if (range === 'all') return true;
  const { start, end } = getRangeBounds(range);
  const today = new Date().toISOString().slice(0, 10);
  if (start && today < start) return false;
  if (end && today > end) return false;
  return true;
}

function getRangeBounds(range) {
  if (range === 'all') return { start: null, end: null };
  const today = new Date();
  const iso = d => d.toISOString().slice(0, 10);
  if (range === 'week') {
    const day = today.getDay();
    const diffToMon = day === 0 ? 6 : day - 1;
    const mon = new Date(today); mon.setDate(today.getDate() - diffToMon);
    const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
    return { start: iso(mon), end: iso(sun) };
  }
  if (range === 'month') {
    const start = new Date(today.getFullYear(), today.getMonth(), 1);
    const end = new Date(today.getFullYear(), today.getMonth() + 1, 0);
    return { start: iso(start), end: iso(end) };
  }
  if (range === 'prev-month') {
    const start = new Date(today.getFullYear(), today.getMonth() - 1, 1);
    const end = new Date(today.getFullYear(), today.getMonth(), 0);
    return { start: iso(start), end: iso(end) };
  }
  const days = range === '7d' ? 7 : range === '30d' ? 30 : 90;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { start: iso(d), end: null };
}

// Number of calendar days the selected range spans (used as denominator
// for "average per day" charts so inactive days still count).
function rangeCalendarDays(range, startISO, endISO) {
  if (range === 'all') return 0;  // unknown — caller falls back to active-days
  const MS = 86400000;
  const today = new Date();
  const todayISO = today.toISOString().slice(0, 10);
  const effectiveEndISO = endISO || todayISO;
  if (!startISO) return 0;
  const s = new Date(startISO + 'T00:00:00Z');
  const e = new Date(effectiveEndISO + 'T00:00:00Z');
  return Math.max(1, Math.round((e - s) / MS) + 1);
}

function readURLRange() {
  const p = new URLSearchParams(window.location.search).get('range');
  return VALID_RANGES.includes(p) ? p : '30d';
}

function setRange(range) {
  selectedRange = range;
  document.querySelectorAll('.range-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.range === range)
  );
  updateURL();
  applyFilter();
  scheduleAutoRefresh();
}

function setHourlyTZ(mode) {
  hourlyTZ = mode;
  document.querySelectorAll('.tz-btn').forEach(btn =>
    btn.classList.toggle('active', btn.dataset.tz === mode)
  );
  applyFilter();
}

// ── Model filter ───────────────────────────────────────────────────────────
function modelPriority(m) {
  const ml = m.toLowerCase();
  if (ml.includes('opus'))   return 0;
  if (ml.includes('sonnet')) return 1;
  if (ml.includes('haiku'))  return 2;
  return 3;
}

function readURLModels(allModels) {
  const param = new URLSearchParams(window.location.search).get('models');
  if (!param) return new Set(allModels.filter(m => isBillable(m)));
  const fromURL = new Set(param.split(',').map(s => s.trim()).filter(Boolean));
  return new Set(allModels.filter(m => fromURL.has(m)));
}

function isDefaultModelSelection(allModels) {
  const billable = allModels.filter(m => isBillable(m));
  if (selectedModels.size !== billable.length) return false;
  return billable.every(m => selectedModels.has(m));
}

function buildFilterUI(allModels) {
  const sorted = [...allModels].sort((a, b) => {
    const pa = modelPriority(a), pb = modelPriority(b);
    return pa !== pb ? pa - pb : a.localeCompare(b);
  });
  selectedModels = readURLModels(allModels);
  const container = document.getElementById('model-checkboxes');
  container.innerHTML = sorted.map(m => {
    const checked = selectedModels.has(m);
    return `<label class="model-cb-label ${checked ? 'checked' : ''}" data-model="${esc(m)}">
      <input type="checkbox" value="${esc(m)}" ${checked ? 'checked' : ''} onchange="onModelToggle(this)">
      ${esc(m)}
    </label>`;
  }).join('');
}

function onModelToggle(cb) {
  const label = cb.closest('label');
  if (cb.checked) { selectedModels.add(cb.value);    label.classList.add('checked'); }
  else            { selectedModels.delete(cb.value); label.classList.remove('checked'); }
  updateURL();
  applyFilter();
}

function selectAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = true; selectedModels.add(cb.value); cb.closest('label').classList.add('checked');
  });
  updateURL(); applyFilter();
}

function clearAllModels() {
  document.querySelectorAll('#model-checkboxes input').forEach(cb => {
    cb.checked = false; selectedModels.delete(cb.value); cb.closest('label').classList.remove('checked');
  });
  updateURL(); applyFilter();
}

// ── URL persistence ────────────────────────────────────────────────────────
function updateURL() {
  const allModels = Array.from(document.querySelectorAll('#model-checkboxes input')).map(cb => cb.value);
  const params = new URLSearchParams();
  if (selectedRange !== '30d') params.set('range', selectedRange);
  if (!isDefaultModelSelection(allModels)) params.set('models', Array.from(selectedModels).join(','));
  const search = params.toString() ? '?' + params.toString() : '';
  history.replaceState(null, '', window.location.pathname + search);
}

// ── Session sort ───────────────────────────────────────────────────────────
function setSessionSort(col) {
  if (sessionSortCol === col) {
    sessionSortDir = sessionSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    sessionSortCol = col;
    sessionSortDir = 'desc';
  }
  updateSortIcons();
  applyFilter();
}

function updateSortIcons() {
  document.querySelectorAll('.sort-icon').forEach(el => el.textContent = '');
  const icon = document.getElementById('sort-icon-' + sessionSortCol);
  if (icon) icon.textContent = sessionSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortSessions(sessions) {
  return [...sessions].sort((a, b) => {
    let av, bv;
    if (sessionSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else if (sessionSortCol === 'duration_min') {
      av = parseFloat(a.duration_min) || 0;
      bv = parseFloat(b.duration_min) || 0;
    } else {
      av = a[sessionSortCol] ?? 0;
      bv = b[sessionSortCol] ?? 0;
    }
    if (av < bv) return sessionSortDir === 'desc' ? 1 : -1;
    if (av > bv) return sessionSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

// ── Aggregation & filtering ────────────────────────────────────────────────
function applyFilter() {
  if (!rawData) return;

  const { start, end } = getRangeBounds(selectedRange);

  // Filter daily rows by model + date range
  const filteredDaily = rawData.daily_by_model.filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );

  // Daily chart: aggregate by day
  const dailyMap = {};
  for (const r of filteredDaily) {
    if (!dailyMap[r.day]) dailyMap[r.day] = { day: r.day, input: 0, output: 0, cache_read: 0, cache_creation: 0 };
    const d = dailyMap[r.day];
    d.input          += r.input;
    d.output         += r.output;
    d.cache_read     += r.cache_read;
    d.cache_creation += r.cache_creation;
  }
  const daily = Object.values(dailyMap).sort((a, b) => a.day.localeCompare(b.day));

  // By model: aggregate tokens + turns from daily data
  const modelMap = {};
  for (const r of filteredDaily) {
    if (!modelMap[r.model]) modelMap[r.model] = { model: r.model, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0 };
    const m = modelMap[r.model];
    m.input          += r.input;
    m.output         += r.output;
    m.cache_read     += r.cache_read;
    m.cache_creation += r.cache_creation;
    m.turns          += r.turns;
  }

  // Filter sessions by model + date range
  const filteredSessions = rawData.sessions_all.filter(s =>
    selectedModels.has(s.model) && (!start || s.last_date >= start) && (!end || s.last_date <= end)
  );

  // Add session counts into modelMap
  for (const s of filteredSessions) {
    if (modelMap[s.model]) modelMap[s.model].sessions++;
  }

  const byModel = Object.values(modelMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project: aggregate from filtered sessions
  const projMap = {};
  for (const s of filteredSessions) {
    if (!projMap[s.project]) projMap[s.project] = { project: s.project, input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const p = projMap[s.project];
    p.input          += s.input;
    p.output         += s.output;
    p.cache_read     += s.cache_read;
    p.cache_creation += s.cache_creation;
    p.turns          += s.turns;
    p.sessions++;
    p.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProject = Object.values(projMap).sort((a, b) => (b.input + b.output) - (a.input + a.output));

  // By project+branch: aggregate from filtered sessions
  const projBranchMap = {};
  for (const s of filteredSessions) {
    const key = s.project + '\x00' + (s.branch || '');
    if (!projBranchMap[key]) projBranchMap[key] = { project: s.project, branch: s.branch || '', input: 0, output: 0, cache_read: 0, cache_creation: 0, turns: 0, sessions: 0, cost: 0 };
    const pb = projBranchMap[key];
    pb.input          += s.input;
    pb.output         += s.output;
    pb.cache_read     += s.cache_read;
    pb.cache_creation += s.cache_creation;
    pb.turns          += s.turns;
    pb.sessions++;
    pb.cost += calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
  }
  const byProjectBranch = Object.values(projBranchMap).sort((a, b) => b.cost - a.cost);

  // Totals
  const totals = {
    sessions:       filteredSessions.length,
    turns:          byModel.reduce((s, m) => s + m.turns, 0),
    input:          byModel.reduce((s, m) => s + m.input, 0),
    output:         byModel.reduce((s, m) => s + m.output, 0),
    cache_read:     byModel.reduce((s, m) => s + m.cache_read, 0),
    cache_creation: byModel.reduce((s, m) => s + m.cache_creation, 0),
    cost:           byModel.reduce((s, m) => s + calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation), 0),
  };

  // Hourly aggregation (filtered by model + range, bucketed by local hour)
  const hourlySrc = (rawData.hourly_by_model || []).filter(r =>
    selectedModels.has(r.model) && (!start || r.day >= start) && (!end || r.day <= end)
  );
  const rangeDayCount = rangeCalendarDays(selectedRange, start, end);
  const hourlyAgg = aggregateHourly(hourlySrc, hourlyTZ, rangeDayCount);

  // Update daily chart title
  document.getElementById('daily-chart-title').textContent = 'Daily Token Usage \u2014 ' + RANGE_LABELS[selectedRange];
  document.getElementById('hourly-chart-title').textContent = 'Average Hourly Distribution \u2014 ' + RANGE_LABELS[selectedRange];

  renderStats(totals);
  renderDailyChart(daily);
  renderHourlyChart(hourlyAgg);
  renderEfficiencyChart(totals);
  renderProjectChart(byProject);
  lastFilteredSessions = sortSessions(filteredSessions);
  lastByProject = sortProjects(byProject);
  lastByProjectBranch = sortProjectBranch(byProjectBranch);
  renderSessionsTable(lastFilteredSessions.slice(0, 20));
  renderProjectCostTable(lastByProject.slice(0, 20));
  renderProjectBranchCostTable(lastByProjectBranch.slice(0, 20));
}

function projectLabel(proj) {
  const m = (rawData && rawData.project_labels) || {};
  return m[proj] || proj;
}

// ── Renderers ──────────────────────────────────────────────────────────────
function renderStats(t) {
  const rangeLabel = RANGE_LABELS[selectedRange].toLowerCase();
  const stats = [
    { label: 'Sessions',       value: t.sessions.toLocaleString(), sub: rangeLabel },
    { label: 'Turns',          value: fmt(t.turns),                sub: rangeLabel },
    { label: 'Input Tokens',   value: fmt(t.input),                sub: rangeLabel },
    { label: 'Output Tokens',  value: fmt(t.output),               sub: rangeLabel },
    { label: 'Cache Read',     value: fmt(t.cache_read),           sub: 'from prompt cache' },
    { label: 'Cache Creation', value: fmt(t.cache_creation),       sub: 'writes to prompt cache' },
    { label: 'Est. Cost',      value: fmtCostBig(t.cost),          sub: 'API pricing, Apr 2026', color: '#4ade80' },
  ];
  document.getElementById('stats-row').innerHTML = stats.map(s => `
    <div class="stat-card">
      <div class="label">${s.label}</div>
      <div class="value" style="${s.color ? 'color:' + s.color : ''}">${esc(s.value)}</div>
      ${s.sub ? `<div class="sub">${esc(s.sub)}</div>` : ''}
    </div>
  `).join('');
}

// Bucket rows into 24 hours (display-TZ), summing turns + output, and count
// the calendar days in the selected range so averages divide by window size,
// not by active-days-only.
//
// NOTE: server pre-applies SQL_TZ_SHIFT, so r.hour is already in local time.
// For 'local' display, use as-is. For 'utc', subtract the local offset.
function aggregateHourly(rows, tzMode, rangeDayCount) {
  const byHour = {};
  for (let h = 0; h < 24; h++) byHour[h] = { turns: 0, output: 0 };
  const days = new Set();
  for (const r of rows) {
    const displayHour = tzMode === 'local'
      ? r.hour
      : ((r.hour - localOffsetHours()) % 24 + 24) % 24;
    byHour[displayHour].turns  += r.turns  || 0;
    byHour[displayHour].output += r.output || 0;
    if (r.day) days.add(r.day);
  }
  // Prefer the range's calendar-day count; fall back to active days for 'all'.
  const dayCount = rangeDayCount && rangeDayCount > 0 ? rangeDayCount : days.size;
  const hours = [];
  for (let h = 0; h < 24; h++) {
    hours.push({
      hour:       h,
      avgTurns:   dayCount ? byHour[h].turns  / dayCount : 0,
      avgOutput:  dayCount ? byHour[h].output / dayCount : 0,
      totalTurns: byHour[h].turns,
      peak:       isPeakHour(h, tzMode),
    });
  }
  return { hours, dayCount };
}

function renderHourlyChart(agg) {
  const dayCountEl = document.getElementById('hourly-day-count');
  dayCountEl.textContent = agg.dayCount
    ? 'avg / day over ' + agg.dayCount + ' day' + (agg.dayCount === 1 ? '' : 's') + ' · ' + tzDisplayName(hourlyTZ)
    : 'No data · ' + tzDisplayName(hourlyTZ);

  const ctx = document.getElementById('chart-hourly').getContext('2d');
  if (charts.hourly) charts.hourly.destroy();

  const labels = agg.hours.map(h => (h.peak ? '⚡ ' : '') + formatHourLabel(h.hour));
  const turns  = agg.hours.map(h => h.avgTurns);
  const output = agg.hours.map(h => h.avgOutput);
  const barColors = agg.hours.map(h => h.peak ? 'rgba(248,113,113,0.8)' : TOKEN_COLORS.input);

  charts.hourly = new Chart(ctx, {
    data: {
      labels: labels,
      datasets: [
        {
          type: 'bar',
          label: 'Avg turns / hour',
          data: turns,
          backgroundColor: barColors,
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'Avg output tokens / hour',
          data: output,
          borderColor: TOKEN_COLORS.output,
          backgroundColor: 'rgba(167,139,250,0.15)',
          borderWidth: 2,
          pointRadius: 2,
          tension: 0.3,
          yAxisID: 'y1',
          order: 1,
        },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { color: '#8892a4', boxWidth: 12 } },
        tooltip: {
          callbacks: {
            title: (items) => {
              if (!items.length) return '';
              const idx = items[0].dataIndex;
              const h = agg.hours[idx];
              const base = formatHourLabel(h.hour) + ' ' + tzDisplayName(hourlyTZ);
              return h.peak ? base + ' · Peak — Anthropic US hours' : base;
            },
            label: (item) => {
              if (item.dataset.label && item.dataset.label.indexOf('turns') !== -1) {
                return ' Avg turns: ' + item.parsed.y.toFixed(2);
              }
              return ' Avg output: ' + fmt(item.parsed.y);
            },
          }
        },
      },
      scales: {
        x: { ticks: { color: '#8892a4', maxRotation: 0, autoSkip: false, font: { size: 10 } }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  beginAtZero: true, ticks: { color: '#8892a4', callback: v => v.toFixed(1) },     grid: { color: '#2a2d3a' }, title: { display: true, text: 'Avg turns / hour',         color: '#8892a4', font: { size: 11 } } },
        y1: { position: 'right', beginAtZero: true, ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { drawOnChartArea: false },   title: { display: true, text: 'Avg output tokens / hour', color: '#8892a4', font: { size: 11 } } },
      }
    }
  });
}

function renderDailyChart(daily) {
  const ctx = document.getElementById('chart-daily').getContext('2d');
  if (charts.daily) charts.daily.destroy();
  charts.daily = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: daily.map(d => d.day),
      datasets: [
        { label: 'Input',          data: daily.map(d => d.input),          backgroundColor: TOKEN_COLORS.input,          stack: 'io',    yAxisID: 'y1' },
        { label: 'Output',         data: daily.map(d => d.output),         backgroundColor: TOKEN_COLORS.output,         stack: 'io',    yAxisID: 'y1' },
        { label: 'Cache Read',     data: daily.map(d => d.cache_read),     backgroundColor: TOKEN_COLORS.cache_read,     stack: 'cache', yAxisID: 'y' },
        { label: 'Cache Creation', data: daily.map(d => d.cache_creation), backgroundColor: TOKEN_COLORS.cache_creation, stack: 'cache', yAxisID: 'y' },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', maxTicksLimit: RANGE_TICKS[selectedRange] }, grid: { color: '#2a2d3a' } },
        y:  { position: 'left',  ticks: { color: '#74de80', callback: v => fmt(v) }, grid: { color: '#2a2d3a' }, title: { display: true, text: 'Cache', color: '#74de80' } },
        y1: { position: 'right', ticks: { color: '#4f8ef7', callback: v => fmt(v) }, grid: { drawOnChartArea: false },    title: { display: true, text: 'Input / Output', color: '#4f8ef7' } },
      }
    }
  });
}

// Efficiency model (based on Anthropic prompt-caching pricing, Apr 2026):
//   cache_write = 1.25x base input price
//   cache_read  = 0.10x base input price
//   output      = ~5x input price (varies by model)
// Composite efficiency score weights 4 sub-metrics:
//   1. Cache Hit Rate     — reused / (reused + new_input)
//   2. Reuse Ratio        — cache_read / cache_creation (writes paying off)
//   3. Output Discipline  — penalize runaway output vs input
//   4. Cost Efficiency    — actual_cost / hypothetical_no_cache_cost
function computeEfficiency(t) {
  const reused        = t.cache_read || 0;
  const cacheWrite    = t.cache_creation || 0;
  const freshInput    = t.input || 0;
  const output        = t.output || 0;
  const newInput      = freshInput + cacheWrite;
  const totalInput    = reused + newInput;

  // 1. Cache hit rate (0–100)
  const cacheHitPct = totalInput > 0 ? (reused / totalInput) * 100 : 0;

  // 2. Reuse ratio: how many times each cached write gets read back.
  //   2.0+ = excellent, 1.0 = breakeven (cache cost recouped), <1 = wasted writes
  const reuseRatio = cacheWrite > 0 ? reused / cacheWrite : (reused > 0 ? 10 : 0);

  // 3. Output discipline: output should be small fraction of input you fed.
  //   Healthy range 0.05–0.3. Above 0.5 = chatty.
  const outputRatio = totalInput > 0 ? output / totalInput : 0;

  // 4. Cost vs hypothetical no-cache. Saved = cache_read * 0.9 of base input price
  //   (since cache_read = 0.1x). cache_write = 0.25x premium over base.
  //   Effective rate vs no-cache baseline:
  const noCacheCost = (reused + newInput) * 1.0;                  // all at 1.0x
  const actualCost  = reused * 0.10 + cacheWrite * 1.25 + freshInput * 1.0;
  const costEffPct  = noCacheCost > 0 ? (1 - actualCost / noCacheCost) * 100 : 0;

  // Composite score 0–100 (weighted)
  const cacheScore  = Math.min(100, cacheHitPct);                       // weight 0.40
  const reuseScore  = Math.min(100, reuseRatio * 25);                   // weight 0.25 (4x reuse=100)
  const outputScore = Math.max(0, 100 - Math.min(100, outputRatio*200));// weight 0.15
  const costScore   = Math.max(0, Math.min(100, costEffPct));           // weight 0.20
  const composite = cacheScore*0.40 + reuseScore*0.25 + outputScore*0.15 + costScore*0.20;

  let grade = 'F', color = '#ef4444';
  if (composite >= 90)      { grade = 'A+'; color = '#22c55e'; }
  else if (composite >= 80) { grade = 'A';  color = '#4ade80'; }
  else if (composite >= 70) { grade = 'B';  color = '#84cc16'; }
  else if (composite >= 60) { grade = 'C';  color = '#facc15'; }
  else if (composite >= 50) { grade = 'D';  color = '#fb923c'; }

  // $ saved estimate using blended input price ($5/M ballpark across opus/sonnet/haiku)
  const BLENDED_INPUT_PRICE_PER_M = 5;
  const savedUSD = (noCacheCost - actualCost) * BLENDED_INPUT_PRICE_PER_M / 1_000_000;

  return {
    reused, cacheWrite, freshInput, newInput, output, totalInput,
    cacheHitPct, reuseRatio, outputRatio, costEffPct,
    cacheScore, reuseScore, outputScore, costScore,
    composite, grade, color, savedUSD,
  };
}

function toggleEffHelp() {
  const el = document.getElementById('eff-help');
  if (!el) return;
  el.style.display = el.style.display === 'none' ? 'block' : 'none';
}
function renderEfficiencyChart(totals) {
  const ctx = document.getElementById('chart-efficiency').getContext('2d');
  if (charts.model) charts.model.destroy();
  const e = computeEfficiency(totals);
  const totalEff = e.reused + e.newInput + e.output;
  if (totalEff === 0) {
    charts.model = null;
    document.getElementById('efficiency-grade').textContent='';
    renderEfficiencyBreakdown(e);
    return;
  }
  document.getElementById('efficiency-grade').innerHTML =
    `<span style="color:${e.color}">Grade ${e.grade} &middot; ${e.composite.toFixed(0)}/100</span>`;
  charts.model = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Cache Reused (0.10x price)', 'Cache Write (1.25x price)', 'Fresh Input (1.0x)', 'Output (generated)'],
      datasets: [{
        data: [e.reused, e.cacheWrite, e.freshInput, e.output],
        backgroundColor: ['#22c55e', '#facc15', '#fb923c', '#4f8ef7'],
        borderWidth: 2, borderColor: '#1a1d27'
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { color: '#8892a4', boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: ctx => ` ${ctx.label}: ${fmt(ctx.raw)} (${((ctx.raw/totalEff)*100).toFixed(1)}%)` } }
      }
    }
  });
  renderEfficiencyBreakdown(e);
}

function renderEfficiencyBreakdown(e) {
  const tips = [];
  if (e.cacheHitPct < 70) tips.push(`Cache hit rate ${e.cacheHitPct.toFixed(0)}% — keep sessions running. Each new \`claude\` session restarts cache. Cache TTL ~5 min, so flurries of activity within 5 min reuse best.`);
  if (e.reuseRatio < 1.5 && e.cacheWrite > 0) tips.push(`Reuse ratio ${e.reuseRatio.toFixed(2)}x — cache writes barely paying off. Either you exit sessions too fast, or context churns (lots of edits invalidating cache).`);
  if (e.outputRatio > 0.3) tips.push(`Output/input ratio ${(e.outputRatio*100).toFixed(0)}% — model generating heavy. Add "respond in under 200 words" or "report concisely" to prompts.`);
  if (e.freshInput > 500000) tips.push(`${fmt(e.freshInput)} fresh-input tokens — files re-read often. Read specific line ranges instead of whole files.`);
  if (e.composite >= 80) tips.push(`Strong efficiency overall. Score ${e.composite.toFixed(0)}/100.`);
  if (!tips.length) tips.push('Solid efficiency — keep current habits.');

  const subScores = [
    { label: 'Cache Hit',       v: e.cacheHitPct.toFixed(1)+'%',           score: e.cacheScore,  weight: '40%', hint: 'reused / total input' },
    { label: 'Reuse Ratio',     v: e.reuseRatio.toFixed(2)+'x',            score: e.reuseScore,  weight: '25%', hint: 'reads per write (4x = 100)' },
    { label: 'Output Discipline', v: (e.outputRatio*100).toFixed(0)+'%',   score: e.outputScore, weight: '15%', hint: 'output / total input — lower better' },
    { label: 'Cost Efficiency', v: e.costEffPct.toFixed(0)+'%',            score: e.costScore,   weight: '20%', hint: '$ saved vs no-cache baseline' },
  ];

  const headerCards = [
    { label: 'Composite Grade',  value: e.grade,                          sub: `${e.composite.toFixed(0)}/100 weighted`,    color: e.color },
    { label: 'Est. $ Saved',     value: '$' + e.savedUSD.toFixed(2),      sub: 'vs running without cache',                  color: '#4ade80' },
    { label: 'Reused Tokens',    value: fmt(e.reused),                    sub: 'pulled from prompt cache' },
    { label: 'New Input Tokens', value: fmt(e.newInput),                  sub: 'fresh + cache writes (full price)' },
  ];

  const headerHTML = headerCards.map(c => `
    <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
      <div style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">${c.label}</div>
      <div style="font-size:22px;font-weight:600;margin-top:4px;color:${c.color||'var(--text)'}">${c.value}</div>
      <div style="color:var(--muted);font-size:11px;margin-top:2px">${c.sub}</div>
    </div>`).join('');

  const subScoreHTML = subScores.map(s => {
    const bar = Math.max(0, Math.min(100, s.score));
    const barColor = bar>=70?'#4ade80':bar>=50?'#facc15':'#ef4444';
    return `
    <div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <span style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.05em">${s.label}</span>
        <span class="muted" style="font-size:10px">weight ${s.weight}</span>
      </div>
      <div style="font-size:18px;font-weight:600;margin:4px 0;color:var(--text)">${s.v}</div>
      <div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin:6px 0">
        <div style="width:${bar}%;height:100%;background:${barColor}"></div>
      </div>
      <div style="color:var(--muted);font-size:11px">${s.hint}</div>
    </div>`;
  }).join('');

  document.getElementById('efficiency-body').innerHTML =
    headerHTML + subScoreHTML +
    `<div style="grid-column:1/-1;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:12px">
      <div style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px">Recommendations</div>
      <ul style="margin:0;padding-left:20px;color:var(--text);font-size:12px;line-height:1.6">${tips.map(t=>`<li>${esc(t)}</li>`).join('')}</ul>
    </div>`;
}

function renderProjectChart(byProject) {
  const top = byProject.slice(0, 10);
  const ctx = document.getElementById('chart-project').getContext('2d');
  if (charts.project) charts.project.destroy();
  if (!top.length) { charts.project = null; return; }
  charts.project = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: top.map(p => { const lbl = projectLabel(p.project); return lbl.length > 30 ? lbl.slice(0,28)+'\u2026' : lbl; }),
      datasets: [
        { label: 'Input',  data: top.map(p => p.input),  backgroundColor: TOKEN_COLORS.input },
        { label: 'Output', data: top.map(p => p.output), backgroundColor: TOKEN_COLORS.output },
      ]
    },
    options: {
      indexAxis: 'y', responsive: true, maintainAspectRatio: false,
      plugins: { legend: { labels: { color: '#8892a4', boxWidth: 12 } } },
      scales: {
        x: { ticks: { color: '#8892a4', callback: v => fmt(v) }, grid: { color: '#2a2d3a' } },
        y: { ticks: { color: '#8892a4', font: { size: 11 } }, grid: { color: '#2a2d3a' } },
      }
    }
  });
}

function renderSessionsTable(sessions) {
  document.getElementById('sessions-body').innerHTML = sessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    const costCell = isBillable(s.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    const fullId = s.session_id_full || s.session_id;
    const title = s.title || '<span class="muted">(no prompt)</span>';
    return `<tr style="cursor:pointer" onclick="startLiveSession('${esc(fullId)}')">
      <td class="muted" style="font-family:monospace">${esc(s.session_id)}&hellip;</td>
      <td>${typeof s.title === 'string' && s.title ? esc(s.title) : '<span class="muted">(no prompt)</span>'}</td>
      <td>${esc(s.project)}</td>
      <td class="muted">${esc(s.last)}</td>
      <td class="muted">${esc(s.duration_min)}m</td>
      <td><span class="model-tag">${esc(s.model)}</span></td>
      <td class="num">${s.turns}</td>
      <td class="num">${fmt(s.input)}</td>
      <td class="num">${fmt(s.output)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Live session view ────────────────────────────────────────────────────────
let liveSessionId = null;
let liveTimer = null;

function startLiveSession(sid) {
  liveSessionId = sid;
  document.getElementById('live-session-card').style.display = '';
  document.getElementById('live-session-card').scrollIntoView({behavior:'smooth', block:'start'});
  fetchLiveSession();
  if (liveTimer) clearInterval(liveTimer);
  liveTimer = setInterval(fetchLiveSession, 5000);
}

function stopLiveSession() {
  liveSessionId = null;
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  document.getElementById('live-session-card').style.display = 'none';
}

async function fetchLiveSession() {
  if (!liveSessionId) return;
  try {
    const res = await fetch('/api/session/' + encodeURIComponent(liveSessionId));
    const d = await res.json();
    if (d.error) {
      document.getElementById('live-summary').textContent = d.error;
      return;
    }
    const cost = calcCost(d.model, d.totals.input, d.totals.output, d.totals.cache_read, d.totals.cache_creation);
    document.getElementById('live-title').textContent = d.title || '(no prompt) — ' + d.session_id.slice(0,8);
    document.getElementById('live-summary').innerHTML =
      `<b>${esc(d.project)}</b> &middot; ${esc(d.model)} &middot; ${d.turn_count} turns &middot; ` +
      `Input ${fmt(d.totals.input)} &middot; Output ${fmt(d.totals.output)} &middot; ` +
      `Cache R/W ${fmt(d.totals.cache_read)}/${fmt(d.totals.cache_creation)} &middot; ` +
      `<span style="color:var(--green)">Est. ${fmtCost(cost)}</span> &middot; ` +
      `Last: ${esc((d.last||'').replace('T',' ').slice(0,19))}`;
    const msgs = d.messages || [];
    if (rawData && rawData.tz_name) {
      const tzEl = document.getElementById('live-tz-label');
      if (tzEl) tzEl.textContent = '(' + rawData.tz_name + ')';
    }
    const rows = msgs.slice().reverse().map((m, idx) => {
      const realIdx = msgs.length - idx;
      const c = calcCost(m.model || d.model, m.input, m.output, m.cache_read, m.cache_creation);
      const tools = (m.tools||[]).join(', ');
      return `<tr>
        <td class="muted num">#${realIdx}</td>
        <td class="muted" style="font-family:monospace;font-size:11px">${esc((m.timestamp||'').replace('T',' ').slice(0,19))}</td>
        <td style="max-width:400px;white-space:normal;font-size:12px">${esc(m.prompt||'(empty)')}</td>
        <td class="num">${m.turn_count}</td>
        <td class="muted num" style="font-size:11px">${esc(tools)}</td>
        <td class="num">${fmt(m.input)}</td>
        <td class="num">${fmt(m.output)}</td>
        <td class="num">${fmt(m.cache_read)}</td>
        <td class="num">${fmt(m.cache_creation)}</td>
        <td class="num cost">${fmtCost(c)}</td>
      </tr>`;
    }).join('');
    document.getElementById('live-turns-body').innerHTML = rows || '<tr><td colspan="10" class="muted" style="padding:20px;text-align:center">No messages parsed</td></tr>';
  } catch (e) {
    document.getElementById('live-summary').textContent = 'Error: ' + e.message;
  }
}

function setModelSort(col) {
  if (modelSortCol === col) {
    modelSortDir = modelSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    modelSortCol = col;
    modelSortDir = 'desc';
  }
  updateModelSortIcons();
  applyFilter();
}

function updateModelSortIcons() {
  document.querySelectorAll('[id^="msort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('msort-' + modelSortCol);
  if (icon) icon.textContent = modelSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortModels(byModel) {
  return [...byModel].sort((a, b) => {
    let av, bv;
    if (modelSortCol === 'cost') {
      av = calcCost(a.model, a.input, a.output, a.cache_read, a.cache_creation);
      bv = calcCost(b.model, b.input, b.output, b.cache_read, b.cache_creation);
    } else {
      av = a[modelSortCol] ?? 0;
      bv = b[modelSortCol] ?? 0;
    }
    if (av < bv) return modelSortDir === 'desc' ? 1 : -1;
    if (av > bv) return modelSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderModelCostTable(byModel) {
  document.getElementById('model-cost-body').innerHTML = sortModels(byModel).map(m => {
    const cost = calcCost(m.model, m.input, m.output, m.cache_read, m.cache_creation);
    const costCell = isBillable(m.model)
      ? `<td class="cost">${fmtCost(cost)}</td>`
      : `<td class="cost-na">n/a</td>`;
    return `<tr>
      <td><span class="model-tag">${esc(m.model)}</span></td>
      <td class="num">${fmt(m.turns)}</td>
      <td class="num">${fmt(m.input)}</td>
      <td class="num">${fmt(m.output)}</td>
      <td class="num">${fmt(m.cache_read)}</td>
      <td class="num">${fmt(m.cache_creation)}</td>
      ${costCell}
    </tr>`;
  }).join('');
}

// ── Project cost table sorting ────────────────────────────────────────────
function setProjectSort(col) {
  if (projectSortCol === col) {
    projectSortDir = projectSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    projectSortCol = col;
    projectSortDir = 'desc';
  }
  updateProjectSortIcons();
  applyFilter();
}

function updateProjectSortIcons() {
  document.querySelectorAll('[id^="psort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('psort-' + projectSortCol);
  if (icon) icon.textContent = projectSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjects(byProject) {
  return [...byProject].sort((a, b) => {
    const av = a[projectSortCol] ?? 0;
    const bv = b[projectSortCol] ?? 0;
    if (av < bv) return projectSortDir === 'desc' ? 1 : -1;
    if (av > bv) return projectSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectCostTable(byProject) {
  document.getElementById('project-cost-body').innerHTML = sortProjects(byProject).map(p => {
    return `<tr>
      <td>${esc(projectLabel(p.project))} <span class="muted" style="font-size:10px">${esc(p.project)}</span></td>
      <td class="num">${p.sessions}</td>
      <td class="num">${fmt(p.turns)}</td>
      <td class="num">${fmt(p.input)}</td>
      <td class="num">${fmt(p.output)}</td>
      <td class="cost">${fmtCost(p.cost)}</td>
    </tr>`;
  }).join('');
}

// ── Project+Branch cost table sorting ────────────────────────────────────
function setProjectBranchSort(col) {
  if (branchSortCol === col) {
    branchSortDir = branchSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    branchSortCol = col;
    branchSortDir = 'desc';
  }
  updateProjectBranchSortIcons();
  applyFilter();
}

function updateProjectBranchSortIcons() {
  document.querySelectorAll('[id^="pbsort-"]').forEach(el => el.textContent = '');
  const icon = document.getElementById('pbsort-' + branchSortCol);
  if (icon) icon.textContent = branchSortDir === 'desc' ? ' \u25bc' : ' \u25b2';
}

function sortProjectBranch(rows) {
  return [...rows].sort((a, b) => {
    const pa = (a.project || '').toLowerCase();
    const pb = (b.project || '').toLowerCase();
    if (pa < pb) return -1;
    if (pa > pb) return 1;
    const av = a[branchSortCol] ?? 0;
    const bv = b[branchSortCol] ?? 0;
    if (av < bv) return branchSortDir === 'desc' ? 1 : -1;
    if (av > bv) return branchSortDir === 'desc' ? -1 : 1;
    return 0;
  });
}

function renderProjectBranchCostTable(rows) {
  document.getElementById('project-branch-cost-body').innerHTML = sortProjectBranch(rows).map(pb => {
    return `<tr>
      <td>${esc(pb.project)}</td>
      <td class="muted" style="font-family:monospace">${esc(pb.branch || '\u2014')}</td>
      <td class="num">${pb.sessions}</td>
      <td class="num">${fmt(pb.turns)}</td>
      <td class="num">${fmt(pb.input)}</td>
      <td class="num">${fmt(pb.output)}</td>
      <td class="cost">${fmtCost(pb.cost)}</td>
    </tr>`;
  }).join('');
}

// ── CSV Export ────────────────────────────────────────────────────────────
function csvField(val) {
  const s = String(val);
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function csvTimestamp() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0')
    + '_' + String(d.getHours()).padStart(2,'0') + String(d.getMinutes()).padStart(2,'0');
}

function downloadCSV(reportType, header, rows) {
  const lines = [header.map(csvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(csvField).join(','));
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = reportType + '_' + csvTimestamp() + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function exportSessionsCSV() {
  const header = ['Session', 'Project', 'Last Active', 'Duration (min)', 'Model', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastFilteredSessions.map(s => {
    const cost = calcCost(s.model, s.input, s.output, s.cache_read, s.cache_creation);
    return [s.session_id, s.project, s.last, s.duration_min, s.model, s.turns, s.input, s.output, s.cache_read, s.cache_creation, cost.toFixed(4)];
  });
  downloadCSV('sessions', header, rows);
}

function exportProjectsCSV() {
  const header = ['Project', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProject.map(p => {
    return [p.project, p.sessions, p.turns, p.input, p.output, p.cache_read, p.cache_creation, p.cost.toFixed(4)];
  });
  downloadCSV('projects', header, rows);
}

function exportProjectBranchCSV() {
  const header = ['Project', 'Branch', 'Sessions', 'Turns', 'Input', 'Output', 'Cache Read', 'Cache Creation', 'Est. Cost'];
  const rows = lastByProjectBranch.map(pb => {
    return [pb.project, pb.branch, pb.sessions, pb.turns, pb.input, pb.output, pb.cache_read, pb.cache_creation, pb.cost.toFixed(4)];
  });
  downloadCSV('projects_by_branch', header, rows);
}

// ── Rescan ────────────────────────────────────────────────────────────────
async function triggerRescan() {
  const btn = document.getElementById('rescan-btn');
  btn.disabled = true;
  btn.textContent = '\u21bb Scanning...';
  try {
    const resp = await fetch('/api/rescan', { method: 'POST' });
    const d = await resp.json();
    btn.textContent = '\u21bb Rescan (' + d.new + ' new, ' + d.updated + ' updated)';
    await loadData();
  } catch(e) {
    btn.textContent = '\u21bb Rescan (error)';
    console.error(e);
  }
  setTimeout(() => { btn.textContent = '\u21bb Rescan'; btn.disabled = false; }, 3000);
}

// ── Data loading ───────────────────────────────────────────────────────────
async function loadData() {
  try {
    const resp = await fetch('/api/data');
    const d = await resp.json();
    if (d.error) {
      document.body.innerHTML = '<div style="padding:40px;color:#f87171">' + esc(d.error) + '</div>';
      return;
    }
    const refreshNote = rangeIncludesToday(selectedRange) ? ' \u00b7 Auto-refresh in 30s' : '';
    document.getElementById('meta').textContent = 'Updated: ' + d.generated_at + refreshNote;

    const isFirstLoad = rawData === null;
    rawData = d;

    if (isFirstLoad) {
      // Restore range from URL, mark active button
      selectedRange = readURLRange();
      document.querySelectorAll('.range-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.range === selectedRange)
      );
      // Mark default TZ button active
      document.querySelectorAll('.tz-btn').forEach(btn =>
        btn.classList.toggle('active', btn.dataset.tz === hourlyTZ)
      );
      // Build model filter (reads URL for model selection too)
      buildFilterUI(d.all_models);
      updateSortIcons();
      updateModelSortIcons();
      updateProjectSortIcons();
      updateProjectBranchSortIcons();
    }

    applyFilter();
  } catch(e) {
    console.error(e);
  }
}

let autoRefreshTimer = null;
function scheduleAutoRefresh() {
  if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
  if (rangeIncludesToday(selectedRange)) {
    autoRefreshTimer = setInterval(loadData, 30000);
  }
}

loadData();
scheduleAutoRefresh();

// ── Live limits banner ─────────────────────────────────────────────────────
function fmtTokens(n) {
  if (n == null) return '—';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}
function fmtCountdown(resetIso) {
  if (!resetIso) return '—';
  const ms = new Date(resetIso).getTime() - Date.now();
  if (ms <= 0) return 'resets now';
  const m = Math.floor(ms / 60000);
  const h = Math.floor(m / 60);
  const mm = m % 60;
  if (h > 0) return `resets in ${h}h ${mm}m`;
  if (m > 0) return `resets in ${m}m`;
  return `resets in <1m`;
}
function applyBar(cardId, used, cap, pct, footRight) {
  const el = document.getElementById(cardId);
  if (!el) return;
  const fill = el.querySelector('.lc-fill');
  const pctEl = el.querySelector('.lc-pct');
  const usedEl = el.querySelector('.lc-used');
  const resetEl = el.querySelector('.lc-reset');
  if (!cap) {
    el.classList.add('lc-disabled');
    fill.style.width = '0%';
    pctEl.textContent = 'n/a';
    usedEl.innerHTML = '<strong>' + fmtTokens(used) + '</strong> used';
    resetEl.textContent = 'not included in plan';
    return;
  }
  el.classList.remove('lc-disabled');
  const p = pct == null ? 0 : pct;
  fill.style.width = Math.min(100, p) + '%';
  fill.classList.remove('warn', 'danger');
  if (p >= 90) fill.classList.add('danger');
  else if (p >= 70) fill.classList.add('warn');
  pctEl.textContent = p.toFixed(1) + '%';
  usedEl.innerHTML = '<strong>' + fmtTokens(used) + '</strong> / ' + fmtTokens(cap);
  resetEl.textContent = footRight;
}
const LIMITS_MODEL_COLORS = { opus: '#d97757', sonnet: '#4f8ef7', haiku: '#4ade80', other: '#8892a4' };
function renderWeeklyBar(wk) {
  const el = document.getElementById('lc-weekly');
  if (!el) return;
  const bar = el.querySelector('.lc-bar');
  const pctEl = el.querySelector('.lc-pct');
  const usedEl = el.querySelector('.lc-used');
  const cap = wk.cap || 0;
  const used = wk.used || 0;
  const totalPct = wk.percent == null ? 0 : wk.percent;
  pctEl.textContent = totalPct.toFixed(1) + '%';
  usedEl.innerHTML = '<strong>' + fmtTokens(used) + '</strong> / ' + fmtTokens(cap);
  const order = ['opus', 'sonnet', 'haiku', 'other'];
  const byTokens = wk.by_model || {};
  const segs = order
    .filter(k => (byTokens[k] || 0) > 0)
    .map(k => {
      const pct = cap > 0 ? (byTokens[k] / cap) * 100 : 0;
      return `<div class="lc-seg" style="width:${pct}%;background:${LIMITS_MODEL_COLORS[k]}" title="${k}: ${fmtTokens(byTokens[k])} (${pct.toFixed(1)}%)"></div>`;
    });
  bar.innerHTML = segs.join('') || '<div class="lc-fill" style="width:0%"></div>';
}
function renderWeeklyModels(wk) {
  const host = document.getElementById('lc-weekly-models');
  if (!host) return;
  const byPct = wk.by_model_pct || {};
  const byTokens = wk.by_model || {};
  const order = ['opus', 'sonnet', 'haiku', 'other'];
  const parts = order
    .filter(k => (byTokens[k] || 0) > 0)
    .map(k => `<span class="lc-model"><span class="lc-swatch" style="background:${LIMITS_MODEL_COLORS[k]}"></span>${k} ${byPct[k]||0}% · ${fmtTokens(byTokens[k]||0)}</span>`);
  host.innerHTML = parts.join('');
}
function renderHealth(h) {
  const card = document.getElementById('lc-health');
  const msg = document.getElementById('health-msg');
  const sid = document.getElementById('health-session-id');
  const stats = document.getElementById('health-stats');
  if (!card || !msg) return;
  card.classList.remove('health-ok', 'health-info', 'health-warn');
  if (!h.active) {
    card.classList.add('health-ok');
    msg.textContent = 'No active session in the last 30 minutes.';
    sid.textContent = '—';
    stats.innerHTML = '';
    return;
  }
  card.classList.add('health-' + (h.level || 'ok'));
  msg.textContent = h.message || '';
  sid.textContent = h.session_id || '';
  const parts = [];
  if (h.context_size != null) parts.push(`context ${fmtTokens(h.context_size)}`);
  if (h.cache_hit_rate != null) parts.push(`cache hit ${(h.cache_hit_rate*100).toFixed(0)}%`);
  if (h.avg_billable_per_turn != null) parts.push(`avg ${fmtTokens(h.avg_billable_per_turn)}/turn`);
  if (h.session_age_hours != null) parts.push(`age ${h.session_age_hours}h`);
  stats.innerHTML = parts.map(p => `<span>${p}</span>`).join(' · ');
}
let limitsTimer = null;
let _userPlanOverride = localStorage.getItem('claudeUsagePlanOverride') || '';
function onPlanOverride() {
  const v = document.getElementById('plan-select').value;
  _userPlanOverride = v;
  if (v) localStorage.setItem('claudeUsagePlanOverride', v);
  else localStorage.removeItem('claudeUsagePlanOverride');
  loadLimits();
}
async function loadLimits() {
  try {
    const url = _userPlanOverride
      ? '/api/limits?plan=' + encodeURIComponent(_userPlanOverride)
      : '/api/limits';
    const resp = await fetch(url);
    const data = await resp.json();
    if (data.error) return;
    const sel = document.getElementById('plan-select');
    if (sel && sel.value !== _userPlanOverride) sel.value = _userPlanOverride;
    const planSrc = document.getElementById('plan-source');
    if (planSrc) {
      const label = (data.plan && data.plan.label) || '—';
      const src = (data.plan && data.plan.source) || 'default';
      planSrc.textContent = `${label} (${src})`;
    }
    const wk = data.weekly_all || {};
    renderWeeklyBar(wk);
    renderWeeklyModels(wk);
    renderHealth(data.session_health || {});
  } catch (e) { /* network blip */ }
}
function scheduleLimits() {
  if (limitsTimer) clearInterval(limitsTimer);
  limitsTimer = setInterval(loadLimits, 10000);
}
// Restore override into select before first fetch.
(function () {
  const sel = document.getElementById('plan-select');
  if (sel && _userPlanOverride) sel.value = _userPlanOverride;
})();
loadLimits();
scheduleLimits();
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

        elif self.path == "/api/data":
            # Incremental scan so data stays live without manual rescan.
            try:
                import scanner
                scanner.scan(db_path=DB_PATH, projects_dirs=scanner.DEFAULT_PROJECTS_DIRS, verbose=False)
            except Exception:
                pass
            data = get_dashboard_data()
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/api/limits"):
            override = None
            if "?" in self.path:
                from urllib.parse import parse_qs
                qs = parse_qs(self.path.split("?", 1)[1])
                override = (qs.get("plan") or [None])[0]
            try:
                import limits
                if override and override in limits.PLAN_BUDGETS:
                    os.environ["CLAUDE_USAGE_PLAN"] = override
                data = limits.get_limits(db_path=DB_PATH)
            except Exception as e:
                data = {"error": str(e)}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path.startswith("/api/session/"):
            sid = self.path[len("/api/session/"):].split("?")[0].split("/")[0]
            try:
                import scanner
                scanner.scan(db_path=DB_PATH, projects_dirs=scanner.DEFAULT_PROJECTS_DIRS, verbose=False)
            except Exception:
                pass
            data = get_session_live(sid)
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/rescan":
            # Full rebuild: delete DB and rescan from scratch.
            # Pass DB_PATH / DEFAULT_PROJECTS_DIRS explicitly so tests that
            # patch the module globals are honored (scan's defaults are
            # frozen at def time and would otherwise target the real paths).
            import scanner
            db_path = DB_PATH
            if db_path.exists():
                db_path.unlink()
            result = scanner.scan(
                db_path=db_path,
                projects_dirs=scanner.DEFAULT_PROJECTS_DIRS,
                verbose=False,
            )
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def serve(host=None, port=None):
    host = host or os.environ.get("HOST", "localhost")
    port = port or int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    serve()
