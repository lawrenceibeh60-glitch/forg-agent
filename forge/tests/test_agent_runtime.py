from pathlib import Path

import agent_runtime


def schema(name, description=""):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": {}}}}


def test_engineering_task_gets_core_tools_and_relevant_capabilities():
    all_tools = [
        schema("read_file"), schema("tree"), schema("run_tests"), schema("tool_catalog"),
        schema("find_symbol", "Find a function or class definition"),
        schema("github_merge_pr", "Merge pull request"), schema("sql_query", "Run SQL"),
    ]
    selected = agent_runtime.select_schemas(all_tools, "Trace references to the Agent class and run tests")
    names = {x["function"]["name"] for x in selected}
    assert {"read_file", "tree", "run_tests", "find_symbol"} <= names
    assert "github_merge_pr" not in names


def test_tool_catalog_is_selected_for_capability_discovery():
    all_tools = [schema("tool_catalog", "Search tool descriptions"), schema("read_file")]
    selected = agent_runtime.select_schemas(all_tools, "What tools are available for image generation?")
    assert "tool_catalog" in {x["function"]["name"] for x in selected}


def test_general_question_does_not_receive_every_tool():
    all_tools = [schema("read_file"), schema("run_tests"), schema("calculate", "Exact arithmetic"), schema("delete_file")]
    selected = agent_runtime.select_schemas(all_tools, "What is the difference between voltage and current?")
    assert len(selected) < len(all_tools)
    assert "delete_file" not in {x["function"]["name"] for x in selected}


def test_workspace_snapshot_includes_manifests_and_excludes_secret_values(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nA useful app")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\nAPI_KEY = \"top-secret-manifest-value\"")
    (tmp_path / "app.py").write_text("def main():\n    return 1")
    (tmp_path / ".env").write_text("API_KEY=very-secret-value")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "bad.js").write_text("vendor")
    brief = agent_runtime.workspace_snapshot(tmp_path)
    assert "README.md" in brief
    assert "pyproject.toml" in brief
    assert "app.py" in brief
    assert "very-secret-value" not in brief
    assert "top-secret-manifest-value" not in brief
    assert "node_modules" not in brief


def test_classify_task_uses_intent():
    assert agent_runtime.classify_task("Fix the API and run tests") == "engineering"
    assert agent_runtime.classify_task("Add dark mode to the interface") == "engineering"
    assert agent_runtime.classify_task("Research the latest documentation and compare sources") == "research"
    assert agent_runtime.classify_task("Explain the idea of voltage") == "general"
