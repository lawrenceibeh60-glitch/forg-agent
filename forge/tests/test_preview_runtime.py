from pathlib import Path
import json

import pytest
import preview_runtime


def test_detects_static_html_preview(tmp_path):
    (tmp_path / "index.html").write_text("<h1>hello</h1>")
    result = preview_runtime.detect_preview(tmp_path, 8080)
    assert result["kind"] == "static"
    assert result["port"] == 8080
    assert "http.server 8080" in result["command"]


def test_detects_vite_dev_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    result = preview_runtime.detect_preview(tmp_path, 5173)
    assert result["kind"] == "node"
    assert result["install"] == "npm install"
    assert "--host 0.0.0.0 --port 5173" in result["command"]


def test_detects_fastapi_module(tmp_path):
    (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    result = preview_runtime.detect_preview(tmp_path, 8000)
    assert result["kind"] == "asgi"
    assert result["command"] == "python -m uvicorn main:app --host 0.0.0.0 --port 8000"
    assert "pip install" in result["install"]


def test_detects_flask_module(tmp_path):
    (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
    result = preview_runtime.detect_preview(tmp_path, 5000)
    assert result["kind"] == "flask"
    assert "flask --app app run" in result["command"]


def test_unknown_project_requires_explicit_command(tmp_path):
    with pytest.raises(ValueError, match="couldn't identify a web entry point"):
        preview_runtime.detect_preview(tmp_path)


def test_preview_path_cannot_escape_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_RENDER", "1")
    monkeypatch.setenv("ALLOW_SHELL", "1")
    monkeypatch.setenv("RENDER_API_KEY", "test")
    monkeypatch.setenv("RENDER_WORKSPACE_ID", "workspace")
    result = preview_runtime.start_preview("session", tmp_path, "../outside", "python -m http.server 5173")
    assert "escapes this chat workspace" in result
