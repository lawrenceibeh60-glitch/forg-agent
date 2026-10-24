import pytest
import workflows


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(workflows, "_DB", tmp_path / "workflow.sqlite3")


def test_workflow_save_list_and_delete(db):
    saved = workflows.save("session1", "Audit", [
        {"action": "map_workspace", "name": "Map"},
        {"action": "security_scan", "name": "Scan"},
    ])
    assert saved["name"] == "Audit"
    assert len(workflows.list_for_session("session1")) == 1
    assert workflows.get("other-session", saved["id"]) is None
    assert workflows.delete("session1", saved["id"])
    assert workflows.list_for_session("session1") == []


def test_workflow_rejects_unsafe_steps(db):
    with pytest.raises(ValueError, match="unsupported action"):
        workflows.save("session1", "Shell", [{"action": "run_command"}])


def test_workflow_rejects_unbounded_steps_and_args(db):
    with pytest.raises(ValueError, match="between 1"):
        workflows.save("session1", "Empty", [])
    with pytest.raises(ValueError, match="unsupported arguments"):
        workflows.save("session1", "Bad args", [{"action": "map_workspace", "args": {"shell": "rm -rf /"}}])


def test_workflow_execution_checkpoints_each_step(db, monkeypatch):
    import intelligence
    class FakeTools:
        @staticmethod
        def use(sid):
            return None
    class FakeIntel:
        @staticmethod
        def project_map(sid, tools_module):
            return {"file_count": 2}
        @staticmethod
        def risk_report(sid, tools_module):
            return {"status": "no_obvious_findings"}
    saved = workflows.save("session1", "Health", [
        {"action": "map_workspace"}, {"action": "security_scan"}
    ])
    result = workflows.run("session1", saved["id"], FakeTools, FakeIntel)
    assert result["status"] == "completed"
    assert result["steps_completed"] == 2
    assert result["results"][0]["output"]["file_count"] == 2


def test_workflow_run_is_session_scoped(db):
    saved = workflows.save("session1", "Audit", [{"action": "map_workspace"}])
    with pytest.raises(ValueError, match="not found"):
        workflows.run("session2", saved["id"], object(), object())
