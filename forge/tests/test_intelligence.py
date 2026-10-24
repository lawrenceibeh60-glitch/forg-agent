import json
from pathlib import Path
import pytest
import intelligence
import tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "app.py").write_text("class Agent:\n    def run(self):\n        return 'ok'\n", encoding="utf-8")
    (root / "README.md").write_text("# Forge\nagent workspace", encoding="utf-8")
    (root / ".env").write_text("API_KEY=should-not-be-read", encoding="utf-8")
    monkeypatch.setattr(tools, "use", lambda sid: root)
    monkeypatch.setattr(intelligence, "_DB", tmp_path / "memory.sqlite3")
    return root


def test_project_map_finds_python_symbols_and_skips_secrets(workspace):
    result = intelligence.project_map("session_1", tools)
    assert result["file_count"] == 2
    assert any(s["name"] == "Agent" for s in result["python_symbols"])
    assert all(".env" not in f["path"] for f in result["files"])


def test_search_workspace_returns_line_matches(workspace):
    result = intelligence.search_workspace("session_1", "agent workspace", tools)
    assert result["results"]
    assert any(x["path"] == "README.md" for x in result["results"])


def test_project_memory_round_trip_and_scope(workspace):
    item = intelligence.save_memory("s1", "decision", "Database", "Use SQLite checkpoints")
    assert item["title"] == "Database"
    assert intelligence.list_memory("s1", "sqlite")[0]["body"] == "Use SQLite checkpoints"
    assert intelligence.list_memory("s2") == []
    assert intelligence.delete_memory("s1", item["id"])
    assert not intelligence.list_memory("s1")


@pytest.mark.parametrize("body", ["API_KEY=abcdef0123456789", "password: 'very-secret-value'"])
def test_memory_rejects_credentials(workspace, body):
    with pytest.raises(ValueError, match="credentials"):
        intelligence.save_memory("s1", "fact", "Unsafe", body)


def test_memory_validates_kind_and_lengths(workspace):
    with pytest.raises(ValueError, match="kind"):
        intelligence.save_memory("s1", "other", "title", "body")
    with pytest.raises(ValueError, match="required"):
        intelligence.save_memory("s1", "fact", "", "body")


def test_risk_report_is_heuristic_and_scoped(workspace):
    (workspace / "unsafe.py").write_text("import pickle\nvalue = pickle.loads(data)\n", encoding="utf-8")
    report = intelligence.risk_report("s1", tools)
    assert report["status"] == "findings"
    assert any(x["rule"] == "unsafe-deserialization" for x in report["findings"])
    assert "not a security certification" in report["note"]


def test_session_id_validation(workspace):
    with pytest.raises(ValueError, match="invalid session"):
        intelligence.project_map("../escape", tools)
