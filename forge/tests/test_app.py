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
