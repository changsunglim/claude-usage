"""Tests for anchored weekly reset window in limits.py."""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import limits
from scanner import get_db, init_db, insert_turns, upsert_sessions


def _mk_db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    path = Path(f.name)
    conn = get_db(path)
    init_db(conn)
    return conn, path


def _add_turns(conn, session_id, project, timestamps_with_tokens):
    upsert_sessions(conn, [{
        "session_id": session_id,
        "project_name": project,
        "first_timestamp": timestamps_with_tokens[0][0],
        "last_timestamp": timestamps_with_tokens[-1][0],
        "git_branch": "main",
        "model": "claude-sonnet-4-6",
        "total_input_tokens": 0, "total_output_tokens": 0,
        "total_cache_read": 0, "total_cache_creation": 0,
        "turn_count": len(timestamps_with_tokens),
    }])
    turns = []
    for i, (ts, toks) in enumerate(timestamps_with_tokens):
        turns.append({
            "session_id": session_id,
            "turn_index": i,
            "timestamp": ts,
            "model": "claude-sonnet-4-6",
            "input_tokens": toks,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "project_name": project,
            "git_branch": "main",
            "tool_name": None,
            "cwd": None,
            "message_id": "",
        })
    insert_turns(conn, turns)


class TestWeeklyAnchor(unittest.TestCase):
    def setUp(self):
        self.anchor_file = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False
        )
        self.anchor_file.close()
        self.anchor_path = Path(self.anchor_file.name)
        self.anchor_path.unlink()  # start fresh
        self._patch = patch.object(limits, "WEEKLY_ANCHOR_PATH", self.anchor_path)
        self._patch.start()
        self.conn, self.db_path = _mk_db()

    def tearDown(self):
        self._patch.stop()
        self.conn.close()
        self.db_path.unlink(missing_ok=True)
        self.anchor_path.unlink(missing_ok=True)

    def test_first_run_uses_earliest_turn_as_anchor(self):
        now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        _add_turns(self.conn, "s1", "p", [
            ((now - timedelta(days=3)).isoformat(), 1000),
            ((now - timedelta(days=1)).isoformat(), 2000),
        ])
        result = limits.compute_weekly(self.conn, now=now)
        self.assertEqual(result["total"], 3000)
        anchor = datetime.fromisoformat(result["anchor_at"])
        self.assertEqual(anchor, now - timedelta(days=3))
        self.assertEqual(result["anchor_source"], "auto")

    def test_manual_anchor_persists(self):
        now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        limits.set_weekly_anchor(now=now)
        _add_turns(self.conn, "s1", "p", [
            ((now - timedelta(days=2)).isoformat(), 5000),  # before anchor
            ((now + timedelta(hours=1)).isoformat(), 7000),  # after anchor
        ])
        result = limits.compute_weekly(self.conn, now=now + timedelta(hours=2))
        self.assertEqual(result["total"], 7000)
        self.assertEqual(result["anchor_source"], "manual")

    def test_auto_advance_after_expiry(self):
        t0 = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        limits.set_weekly_anchor(now=t0)
        # mark anchor as manual; auto-advance should still bump it
        # 8 days later: first turn after expiry is at t0+7d+2h
        next_turn = t0 + timedelta(days=7, hours=2)
        _add_turns(self.conn, "s2", "p", [
            (t0.isoformat(), 1000),
            (next_turn.isoformat(), 3000),
        ])
        now = t0 + timedelta(days=8)
        result = limits.compute_weekly(self.conn, now=now)
        anchor = datetime.fromisoformat(result["anchor_at"])
        self.assertEqual(anchor, next_turn)
        self.assertEqual(result["total"], 3000)

    def test_reset_at_equals_anchor_plus_7d(self):
        now = datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
        anchor = limits.set_weekly_anchor(now=now)
        result = limits.compute_weekly(self.conn, now=now)
        reset = datetime.fromisoformat(result["reset_at"])
        self.assertEqual(reset - anchor, timedelta(days=7))


if __name__ == "__main__":
    unittest.main()
