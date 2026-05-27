"""Tests for governance.self_exemption — GovernanceGuard."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add repo root to sys.path so imports resolve without installation
sys.path.insert(0, str(Path(__file__).parents[2]))

from governance.self_exemption import GovernanceGuard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_guard(repo_root: Path, audit_store=None) -> GovernanceGuard:
    return GovernanceGuard(repo_root=repo_root, audit_store=audit_store)


# ---------------------------------------------------------------------------
# Protected path identification
# ---------------------------------------------------------------------------

class TestIsGovernancePath:
    def test_mission_md_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "mission.md").touch()
        assert guard.is_governance_path(tmp_path / "mission.md") is True

    def test_mind_md_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "mind.md").touch()
        assert guard.is_governance_path(tmp_path / "mind.md") is True

    def test_morals_md_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "morals.md").touch()
        assert guard.is_governance_path(tmp_path / "morals.md") is True

    def test_memory_module_md_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "memory_module.md").touch()
        assert guard.is_governance_path(tmp_path / "memory_module.md") is True

    def test_gates_dir_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        gates = tmp_path / "gates"
        gates.mkdir()
        assert guard.is_governance_path(gates) is True

    def test_file_inside_gates_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        gates = tmp_path / "gates"
        gates.mkdir()
        policy = gates / "policy.py"
        policy.touch()
        assert guard.is_governance_path(policy) is True

    def test_nested_file_inside_governance_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        gov = tmp_path / "governance"
        gov.mkdir()
        nested = gov / "subdir" / "config.json"
        nested.parent.mkdir(parents=True)
        nested.touch()
        assert guard.is_governance_path(nested) is True

    def test_audit_dir_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        assert guard.is_governance_path(audit_dir) is True

    def test_file_inside_audit_is_protected(self, tmp_path):
        guard = make_guard(tmp_path)
        audit_dir = tmp_path / "audit"
        audit_dir.mkdir()
        store_py = audit_dir / "store.py"
        store_py.touch()
        assert guard.is_governance_path(store_py) is True


# ---------------------------------------------------------------------------
# Non-governance paths are permitted
# ---------------------------------------------------------------------------

class TestPermittedPaths:
    def test_output_file_is_permitted(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "output.txt").touch()
        assert guard.is_governance_path(tmp_path / "output.txt") is False

    def test_server_py_is_permitted(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "server.py").touch()
        assert guard.is_governance_path(tmp_path / "server.py") is False

    def test_memory_dir_is_permitted(self, tmp_path):
        guard = make_guard(tmp_path)
        mem = tmp_path / "memory"
        mem.mkdir()
        assert guard.is_governance_path(mem) is False

    def test_requirements_txt_is_permitted(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "requirements.txt").touch()
        assert guard.is_governance_path(tmp_path / "requirements.txt") is False

    def test_deep_output_path_is_permitted(self, tmp_path):
        guard = make_guard(tmp_path)
        out = tmp_path / "data" / "results" / "run.json"
        out.parent.mkdir(parents=True)
        out.touch()
        assert guard.is_governance_path(out) is False


# ---------------------------------------------------------------------------
# check_write() — allow/deny integration
# ---------------------------------------------------------------------------

class TestCheckWrite:
    def test_check_write_permits_safe_path(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "output.log").touch()
        assert guard.check_write(tmp_path / "output.log") is True

    def test_check_write_denies_governance_path(self, tmp_path):
        guard = make_guard(tmp_path)
        (tmp_path / "morals.md").touch()
        assert guard.check_write(tmp_path / "morals.md") is False

    def test_check_write_logs_denial_to_audit_store(self, tmp_path):
        from audit.store import AuditStore

        log_file = tmp_path / "audit.jsonl"
        audit = AuditStore(log_file)
        guard = make_guard(tmp_path, audit_store=audit)
        (tmp_path / "mission.md").touch()

        result = guard.check_write(tmp_path / "mission.md", session_id="s-test")
        assert result is False

        events = audit.query(event_type="governance_access")
        assert len(events) == 1
        e = events[0]
        assert e.decision == "deny"
        assert e.outcome == "blocked"
        assert e.session_id == "s-test"

    def test_check_write_no_audit_store_does_not_raise(self, tmp_path):
        guard = make_guard(tmp_path, audit_store=None)
        (tmp_path / "mind.md").touch()
        # No exception even without an audit store
        result = guard.check_write(tmp_path / "mind.md")
        assert result is False


# ---------------------------------------------------------------------------
# Symlink traversal is blocked
# ---------------------------------------------------------------------------

class TestSymlinkTraversal:
    def test_symlink_to_governance_file_is_blocked(self, tmp_path):
        guard = make_guard(tmp_path)
        # Create the real governance file
        mission = tmp_path / "mission.md"
        mission.write_text("governance content")

        # Create an innocuously-named symlink pointing at it
        symlink = tmp_path / "totally_safe_output.txt"
        symlink.symlink_to(mission)

        # Must be blocked because it resolves to mission.md
        assert guard.is_governance_path(symlink) is True

    def test_symlink_to_governance_dir_is_blocked(self, tmp_path):
        guard = make_guard(tmp_path)
        gates = tmp_path / "gates"
        gates.mkdir()
        (gates / "policy.py").write_text("rules")

        # Symlink with innocent name pointing at gates/
        link = tmp_path / "definitely_user_data"
        link.symlink_to(gates)

        assert guard.is_governance_path(link) is True

    def test_symlink_to_file_inside_governance_dir_is_blocked(self, tmp_path):
        guard = make_guard(tmp_path)
        gov = tmp_path / "governance"
        gov.mkdir()
        real_file = gov / "self_exemption.py"
        real_file.write_text("code")

        # Symlink with innocent name
        link = tmp_path / "benign_looking.py"
        link.symlink_to(real_file)

        assert guard.is_governance_path(link) is True

    def test_symlink_to_safe_file_is_permitted(self, tmp_path):
        guard = make_guard(tmp_path)
        safe = tmp_path / "output.txt"
        safe.write_text("data")

        link = tmp_path / "alias.txt"
        link.symlink_to(safe)

        assert guard.is_governance_path(link) is False


# ---------------------------------------------------------------------------
# verify_read_only_mounts
# ---------------------------------------------------------------------------

class TestVerifyReadOnlyMounts:
    def test_writable_path_reported_as_not_read_only(self, tmp_path):
        p = tmp_path / "writable.txt"
        p.touch()
        # Ensure the current process can write it
        os.chmod(p, 0o644)
        result = GovernanceGuard.verify_read_only_mounts([p])
        assert result[str(p)] is False  # writable → not read-only

    def test_non_writable_path_reported_as_read_only(self, tmp_path):
        p = tmp_path / "readonly.txt"
        p.touch()
        os.chmod(p, 0o444)
        try:
            result = GovernanceGuard.verify_read_only_mounts([p])
            assert result[str(p)] is True  # not writable → read-only
        finally:
            # Restore permissions so tmp_path cleanup works
            os.chmod(p, 0o644)

    def test_nonexistent_path_reported_as_read_only(self, tmp_path):
        p = tmp_path / "does_not_exist.md"
        result = GovernanceGuard.verify_read_only_mounts([p])
        assert result[str(p)] is True  # absent ≡ cannot write → treated as protected

    def test_multiple_paths(self, tmp_path):
        w = tmp_path / "writable.txt"
        w.touch()
        os.chmod(w, 0o644)

        r = tmp_path / "readonly.txt"
        r.touch()
        os.chmod(r, 0o444)

        try:
            result = GovernanceGuard.verify_read_only_mounts([w, r])
            assert result[str(w)] is False
            assert result[str(r)] is True
        finally:
            os.chmod(r, 0o644)

    def test_returns_dict_with_string_keys(self, tmp_path):
        p = tmp_path / "f.txt"
        p.touch()
        result = GovernanceGuard.verify_read_only_mounts([p])
        assert isinstance(list(result.keys())[0], str)


# ---------------------------------------------------------------------------
# get_protected_paths returns resolved absolute paths
# ---------------------------------------------------------------------------

class TestGetProtectedPaths:
    def test_returns_list_of_paths(self, tmp_path):
        guard = make_guard(tmp_path)
        paths = guard.get_protected_paths()
        assert isinstance(paths, list)
        assert all(isinstance(p, Path) for p in paths)

    def test_count_matches_protected_list(self, tmp_path):
        guard = make_guard(tmp_path)
        assert len(guard.get_protected_paths()) == len(GovernanceGuard.PROTECTED)

    def test_paths_are_absolute(self, tmp_path):
        guard = make_guard(tmp_path)
        for p in guard.get_protected_paths():
            assert p.is_absolute()
