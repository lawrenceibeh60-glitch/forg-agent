import json

import tools_extra


def test_tool_catalog_returns_matching_registered_capabilities():
    result = json.loads(tools_extra.tool_catalog("find symbol references", 10))
    names = {item["name"] for item in result}
    assert "find_symbol" in names
    assert "find_references" in names


def test_project_map_identifies_tests_and_entry_docs(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Demo")
    (tmp_path / "app.py").write_text("class Agent:\n    pass\n")
    (tmp_path / "test_app.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr(tools_extra, "safe", lambda path: tmp_path)
    result = json.loads(tools_extra.project_map(".", 50))
    assert "README.md" in result["manifests_and_entry_docs"]
    assert any("test_app.py" in item for item in result["test_files"])
    assert any("Agent" in item for item in result["python_symbols"])


def test_find_symbol_and_references(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("class Agent:\n    pass\n\nAgent()\n")
    monkeypatch.setattr(tools_extra, "safe", lambda path: tmp_path / path)
    monkeypatch.setattr(tools_extra, "WS", tmp_path)
    assert "Agent" in tools_extra.find_symbol("Agent")
    assert "app.py" in tools_extra.find_references("Agent", "*.py")


def test_dependency_graph_detects_local_python_import(tmp_path, monkeypatch):
    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    (tmp_path / "app.py").write_text("import helper\n")
    monkeypatch.setattr(tools_extra, "safe", lambda path: tmp_path)
    result = json.loads(tools_extra.dependency_graph("."))
    assert any(edge["from"] == "app.py" and edge["to"] == "helper.py" for edge in result["internal_edges"])
