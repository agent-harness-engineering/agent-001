"""Tests for audit.store — AuditStore and AuditEvent."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add repo root to sys.path so imports resolve without installation
sys.path.insert(0, str(Path(__file__).parents[2]))

from audit.store import AuditEvent, AuditStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_event(**overrides) -> AuditEvent:
    defaults = dict(
        event_id=str(uuid.uuid4()),
        timestamp="2026-04-29T00:00:00+00:00",
        event_type="tool_call",
        session_id="test-session",
        agent_id="agent-001",
        tool_name="read_file",
        tool_call_id="tc-1",
        decision="allow",
        outcome="success",
        details={"path": "/tmp/foo.txt"},
    )
    defaults.update(overrides)
    return AuditEvent(**defaults)


# ---------------------------------------------------------------------------
# record() writes valid JSONL
# ---------------------------------------------------------------------------

class TestRecord:
    def test_record_creates_file(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        event = make_event()
        store.record(event)
        assert store.log_path.exists()

    def test_record_writes_one_line_per_event(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        store.record(make_event(event_id="a"))
        store.record(make_event(event_id="b"))
        lines = store.log_path.read_text().splitlines()
        assert len(lines) == 2

    def test_record_valid_json(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        event = make_event()
        store.record(event)
        line = store.log_path.read_text().strip()
        data = json.loads(line)
        assert data["event_id"] == event.event_id
        assert data["event_type"] == "tool_call"
        assert data["session_id"] == "test-session"

    def test_record_appends_not_overwrites(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        for i in range(5):
            store.record(make_event(event_id=str(i)))
        lines = store.log_path.read_text().splitlines()
        assert len(lines) == 5

    def test_record_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c" / "audit.jsonl"
        store = AuditStore(deep)
        store.record(make_event())
        assert deep.exists()

    def test_record_thread_safe(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        errors = []

        def worker(n):
            try:
                for _ in range(20):
                    store.record(make_event(event_id=str(uuid.uuid4())))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        lines = store.log_path.read_text().splitlines()
        assert len(lines) == 200


# ---------------------------------------------------------------------------
# record() silently handles write failure
# ---------------------------------------------------------------------------

class TestRecordSilentFailure:
    def test_write_failure_does_not_raise(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        # Patch open to raise IOError
        with patch.object(Path, "open", side_effect=IOError("disk full")):
            # Must not raise — silent fail to stderr
            store.record(make_event())

    def test_write_failure_prints_to_stderr(self, tmp_path, capsys):
        store = AuditStore(tmp_path / "audit.jsonl")
        with patch.object(Path, "open", side_effect=IOError("disk full")):
            store.record(make_event(event_id="err-event"))
        captured = capsys.readouterr()
        assert "err-event" in captured.err
        assert "AuditStore" in captured.err


# ---------------------------------------------------------------------------
# query() filters by session_id and event_type
# ---------------------------------------------------------------------------

class TestQuery:
    def _populate(self, store: AuditStore):
        events = [
            make_event(event_id="1", session_id="s1", event_type="tool_call"),
            make_event(event_id="2", session_id="s1", event_type="permission_decision"),
            make_event(event_id="3", session_id="s2", event_type="tool_call"),
            make_event(event_id="4", session_id="s2", event_type="validation_error"),
            make_event(event_id="5", session_id="s1", event_type="tool_call"),
        ]
        for e in events:
            store.record(e)

    def test_query_all(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        self._populate(store)
        results = store.query()
        assert len(results) == 5

    def test_query_by_session_id(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        self._populate(store)
        results = store.query(session_id="s1")
        assert len(results) == 3
        assert all(e.session_id == "s1" for e in results)

    def test_query_by_event_type(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        self._populate(store)
        results = store.query(event_type="tool_call")
        assert len(results) == 3
        assert all(e.event_type == "tool_call" for e in results)

    def test_query_combined_filters(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        self._populate(store)
        results = store.query(session_id="s1", event_type="tool_call")
        assert len(results) == 2
        for e in results:
            assert e.session_id == "s1"
            assert e.event_type == "tool_call"

    def test_query_limit(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        self._populate(store)
        results = store.query(limit=2)
        assert len(results) == 2

    def test_query_returns_most_recent(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        self._populate(store)
        results = store.query(limit=2)
        # Most recent 2 are event_id "4" and "5"
        ids = {e.event_id for e in results}
        assert ids == {"4", "5"}

    def test_query_missing_file(self, tmp_path):
        store = AuditStore(tmp_path / "nonexistent.jsonl")
        results = store.query()
        assert results == []

    def test_query_returns_audit_event_objects(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        store.record(make_event())
        results = store.query()
        assert len(results) == 1
        assert isinstance(results[0], AuditEvent)


# ---------------------------------------------------------------------------
# make_callback() produces a working callable
# ---------------------------------------------------------------------------

class TestMakeCallback:
    def test_callback_records_event(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        cb = store.make_callback(
            event_type="tool_call",
            session_id="session-42",
            agent_id="agent-001",
        )
        cb({"tool_name": "write_file", "tool_call_id": "tc-99", "outcome": "success"})

        results = store.query(session_id="session-42")
        assert len(results) == 1
        e = results[0]
        assert e.event_type == "tool_call"
        assert e.session_id == "session-42"
        assert e.agent_id == "agent-001"
        assert e.tool_name == "write_file"
        assert e.tool_call_id == "tc-99"
        assert e.outcome == "success"

    def test_callback_assigns_new_uuid(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        cb = store.make_callback("session_start", "s1", "a1")
        cb({})
        cb({})
        results = store.query()
        ids = [e.event_id for e in results]
        assert ids[0] != ids[1]

    def test_callback_extra_keys_go_to_details(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        cb = store.make_callback("permission_decision", "s1", "a1")
        cb({"decision": "deny", "reason": "policy_deny", "rule": "rm_*"})
        results = store.query()
        assert len(results) == 1
        e = results[0]
        assert e.decision == "deny"
        assert e.details.get("reason") == "policy_deny"
        assert e.details.get("rule") == "rm_*"

    def test_callback_none_fields_when_not_provided(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        cb = store.make_callback("session_end", "s1", "a1")
        cb({})
        results = store.query()
        assert len(results) == 1
        e = results[0]
        assert e.tool_name is None
        assert e.tool_call_id is None
        assert e.decision is None
        assert e.outcome is None

    def test_callback_has_timestamp(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        cb = store.make_callback("tool_call", "s1", "a1")
        cb({"tool_name": "read_file"})
        results = store.query()
        assert results[0].timestamp  # non-empty string
