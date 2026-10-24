import asyncio
import io
import time
import zipfile
import pytest
from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


@pytest.fixture(autouse=True)
def authenticated_test_client(monkeypatch):
    """Exercise protected routes with a signed test session, not an auth bypass."""
    monkeypatch.setattr(app, "SESSION_SECRET", "forge-test-session-secret")
    client.cookies.set(
        "forge_session",
        app._session_value("test-user", "123", int(time.time()) + 3600),
    )
    yield
    client.cookies.delete("forge_session")


def test_health_check():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_serves_frontend():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert len(response.text) > 100


def test_status_endpoint_reports_configured_integrations_as_booleans():
    response = client.get("/api/status")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"xkiro", "github", "render"}
    assert all(isinstance(value, bool) for value in body.values())


def test_connections_endpoint_has_github_and_render_states():
    response = client.get("/api/connections")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"github", "render"}
    assert isinstance(body["github"]["connected"], bool)
    assert isinstance(body["render"]["connected"], bool)



def test_custom_prompt_settings_round_trip(tmp_path, monkeypatch):
    target = tmp_path / "custom_prompt.txt"
    monkeypatch.setattr(app, "CUSTOM_PROMPT", target)
    response = client.get("/api/settings/prompt")
    assert response.status_code == 200
    assert response.json() == {"prompt": ""}

    response = client.put("/api/settings/prompt", json={"prompt": "Prefer small, tested changes."})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert client.get("/api/settings/prompt").json()["prompt"] == "Prefer small, tested changes."


def test_custom_prompt_rejects_oversized_input():
    response = client.put("/api/settings/prompt", json={"prompt": "x" * 12001})
    assert response.status_code == 413


def test_custom_prompt_rejects_non_text_input():
    response = client.put("/api/settings/prompt", json={"prompt": ["not", "text"]})
    assert response.status_code == 400


def test_workspace_file_download_and_zip_are_real_artifacts(tmp_path, monkeypatch):
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    (workspace / "hello.py").write_text("print('hello')\n")
    (workspace / "notes.txt").write_text("ready")
    monkeypatch.setattr(app.tools, "use", lambda sid: workspace)

    response = client.get("/api/sandbox/download", params={"sid": "test-session", "path": "hello.py"})
    assert response.status_code == 200
    assert response.content == b"print('hello')\n"
    assert "attachment" in response.headers.get("content-disposition", "")

    response = client.get("/api/sandbox.zip", params={"sid": "test-session"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"hello.py", "notes.txt"}
        assert archive.read("hello.py") == b"print('hello')\n"


def test_workspace_download_rejects_traversal_and_secrets(tmp_path, monkeypatch):
    workspace = tmp_path / "chat-workspace"
    workspace.mkdir()
    (workspace / "safe.txt").write_text("ok")
    (workspace / ".env").write_text("TOKEN=secret")
    monkeypatch.setattr(app.tools, "use", lambda sid: workspace)

    traversal = client.get("/api/sandbox/download", params={"sid": "test-session", "path": "../outside.txt"})
    assert traversal.status_code == 404
    secret = client.get("/api/sandbox/download", params={"sid": "test-session", "path": ".env"})
    assert secret.status_code == 403


def test_workspace_zip_returns_clear_error_for_empty_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    monkeypatch.setattr(app.tools, "use", lambda sid: workspace)
    response = client.get("/api/sandbox.zip", params={"sid": "test-session"})
    assert response.status_code == 404
    assert "no downloadable files" in response.json()["error"]


def test_github_mission_control_handles_disconnected_account(monkeypatch):
    monkeypatch.setattr(app.connections, "get_access_token", lambda provider: "")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    response = client.get("/api/github/mission")
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["repositories"] == []
    assert "Connect GitHub" in body["message"]


def test_eval_run_requires_explicit_token_cost_confirmation():
    response = client.post("/api/evals/run", json={"confirm": False, "limit": 1})
    assert response.status_code == 400
    assert "token-cost warning" in response.json()["error"]


def test_interrupted_tool_batch_is_repaired_without_claiming_success():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "tool_calls": [
            {"id": "call-a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "call-b", "type": "function", "function": {"name": "run_tests", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-a", "content": "file contents"},
    ]
    repaired, changed = app._repair_pending_tool_calls(messages)
    assert changed is True
    assert [m["tool_call_id"] for m in repaired if m.get("role") == "tool"] == ["call-a", "call-b"]
    synthetic = repaired[-1]["content"]
    assert "outcome is unknown" in synthetic
    assert "Do not assume it succeeded or failed" in synthetic


def test_completed_tool_batch_is_not_rewritten():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "call-a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-a", "content": "ok"},
        {"role": "assistant", "content": "finished"},
    ]
    repaired, changed = app._repair_pending_tool_calls(messages)
    assert changed is False
    assert repaired == messages


def test_load_prefers_shared_conversation_storage(monkeypatch, tmp_path):
    messages = [{"role": "system", "content": "persisted"}, {"role": "user", "content": "hello"}]
    monkeypatch.setattr(app.tasks, "load_session", lambda sid: messages if sid == "session-shared" else None)
    monkeypatch.setattr(app, "SD", tmp_path)
    assert app.load("session-shared") == messages


def test_task_resume_continues_the_same_persisted_task(monkeypatch):
    original = {
        "id": "task-resume-123",
        "session_id": "test-session",
        "goal": "Finish the original task",
        "status": "blocked",
        "checkpoint": {"phase": "client_cancelled", "turn": 7},
        "plan": [{"label": "Implement", "status": "in_progress"}],
        "completed": [{"label": "Inspect"}],
        "current_step": "Connection interrupted",
        "last_error": "Client disconnected",
    }
    monkeypatch.setattr(app.tasks, "get", lambda task_id: original if task_id == original["id"] else None)

    async def capture_resume(request):
        return request

    monkeypatch.setattr(app, "chat", capture_resume)
    request = asyncio.run(app.task_resume(original["id"], app.ResumeReq()))

    assert request.session == original["session_id"]
    assert request.task_id == original["id"]
    assert "Finish the original task" in request.message
    assert "Client disconnected" in request.message


def test_task_resume_rejects_completed_tasks(monkeypatch):
    original = {"id": "task-done", "status": "completed"}
    monkeypatch.setattr(app.tasks, "get", lambda task_id: original)
    response = asyncio.run(app.task_resume(original["id"], app.ResumeReq()))
    assert response.status_code == 409


def test_chat_rejects_task_resume_from_another_session_before_loading_history(monkeypatch):
    monkeypatch.setattr(app.tasks, "get", lambda task_id: {
        "id": task_id, "session_id": "owner-session", "status": "blocked"
    })
    monkeypatch.setattr(app, "load", lambda sid: (_ for _ in ()).throw(AssertionError("history must not be loaded")))
    response = asyncio.run(app.chat(app.Req(
        session="different-session",
        message="continue this task",
        task_id="task-owned-by-another-session",
    )))
    assert response.status_code == 404
    assert response.body == b'{"error":"task not found for this session"}'
