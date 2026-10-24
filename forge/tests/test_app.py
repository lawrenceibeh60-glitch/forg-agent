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
