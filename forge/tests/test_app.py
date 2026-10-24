from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


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
