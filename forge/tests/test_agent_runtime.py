from pathlib import Path

import agent_runtime
import app
import render_sandbox


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



def test_failure_classification_distinguishes_capability_from_code_failure():
    assert agent_runtime.classify_failure(
        "run_command", "No isolated command runner is configured; refusing to execute on the host"
    ) == "configuration_or_capability"
    assert agent_runtime.classify_failure(
        "run_tests", "AssertionError: expected 2, got 1 [exit 1]"
    ) == "task_or_code_failure"


def test_failure_classification_identifies_authorization_and_transient_errors():
    assert agent_runtime.classify_failure("github_put_file", "403 Forbidden") == "authorization"
    assert agent_runtime.classify_failure("check_url", "HTTP 503 service unavailable") == "transient_infrastructure"


def test_recovery_policy_does_not_turn_a_blocker_into_permission_bypass():
    instruction = agent_runtime.recovery_instruction(
        "authorization", "github_merge_pr", "Action was denied by the user."
    )
    assert "Do not bypass approval" in instruction
    assert "github_merge_pr" in instruction


def test_recovery_policy_for_code_failure_requires_repair_and_reverification():
    instruction = agent_runtime.recovery_instruction(
        "task_or_code_failure", "run_tests", "AssertionError: expected 2, got 1"
    )
    assert "targeted repair" in instruction
    assert "rerun the narrowest" in instruction



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


def test_verification_requires_successful_exit_evidence():
    assert agent_runtime.verification_succeeded("run_tests", "4 passed\n[exit 0]")
    assert not agent_runtime.verification_succeeded("run_tests", "2 failed\n[exit 1]")
    assert agent_runtime.verification_succeeded("check_url", "200 in 0.12s")
    assert not agent_runtime.verification_succeeded("check_url", "503 in 0.12s")
    assert not agent_runtime.verification_succeeded("bg_start", "started")


def test_verification_policy_block_is_not_retried_blindly():
    output = "no isolated command runner is configured; refusing to execute on the host"
    assert agent_runtime.verification_unavailable(output)
    assert not agent_runtime.verification_succeeded("run_tests", output)



def test_sandbox_blocker_detects_missing_runner_and_render_access():
    assert agent_runtime.sandbox_blocker("no isolated command runner is configured")
    assert agent_runtime.sandbox_blocker("Render sandbox is not configured. Required: SANDBOX_RENDER=1")
    assert agent_runtime.sandbox_blocker("Render sandbox SDK missing; install render>=1.2")
    assert agent_runtime.sandbox_blocker("Preview failed: ClientError: create sandbox failed: not found")
    assert agent_runtime.sandbox_blocker("Render sandbox creation failed closed: Render returned 'not found' while creating the sandbox.")
    assert agent_runtime.verification_unavailable("no isolated command runner is configured")


def test_sandbox_blocker_does_not_mistake_normal_code_failure_for_infrastructure():
    assert not agent_runtime.sandbox_blocker("AssertionError: expected 4, got 3 [exit 1]")
    assert not agent_runtime.sandbox_blocker("npm test failed: one unit test failed")
    assert not agent_runtime.sandbox_blocker("Sandbox failed validation because a unit test failed [exit 1]")


def test_route_heads_selects_security_lenses_for_sandbox_tasks():
    heads = agent_runtime.route_heads("Fix the command sandbox execution loop")
    assert {"router", "analyst", "executor", "verifier", "critic"} <= set(heads)


def test_render_sandbox_not_found_error_explains_missing_workspace_group():
    class MissingGroupAPI:
        def list_groups(self):
            return type("Groups", (), {"groups": []})()

    error = type("ClientError", (Exception,), {})("create sandbox failed: not found")
    message = render_sandbox.creation_failure_diagnosis(error, MissingGroupAPI())
    assert "no sandbox group" in message.lower()
    assert "did not repeat" in message.lower()
    assert "RENDER_WORKSPACE_ID" in message


def test_render_sandbox_not_found_error_does_not_claim_group_missing_when_visible():
    class VisibleGroupAPI:
        def list_groups(self):
            return type("Groups", (), {"groups": [object()]})()

    error = type("ClientError", (Exception,), {})("create sandbox failed: not found")
    message = render_sandbox.creation_failure_diagnosis(error, VisibleGroupAPI())
    assert "sandbox group is visible" in message.lower()
    assert "api key" in message.lower()


def test_engineering_task_always_exposes_connected_sandbox_tools():
    names = {
        "list_files", "run_command", "sandbox_runtime_info", "sandbox_info",
        "ipython_eval", "linux_package_install", "bg_start", "bg_logs",
        "bg_stop", "bg_list", "preview_start", "preview_stop",
    }
    selected = agent_runtime.select_schemas([schema(name) for name in names], "Build and test this app in the Linux sandbox")
    assert names <= {x["function"]["name"] for x in selected}


def test_natural_language_sandbox_requests_route_to_execution_tools():
    available = [
        schema("sandbox_runtime_info"), schema("run_command"), schema("linux_package_install"),
        schema("ipython_eval"), schema("bg_start"), schema("preview_start"),
        schema("delete_file"),
    ]
    selected = agent_runtime.select_schemas(available, "Please use the Linux shell, install the missing packages, and show me a live preview")
    names = {x["function"]["name"] for x in selected}
    assert {"sandbox_runtime_info", "run_command", "linux_package_install", "preview_start"} <= names
    assert "delete_file" not in names



def test_agent_turn_budget_defaults_to_ten_thousand(monkeypatch):
    monkeypatch.delenv("FORGE_MAX_AGENT_TURNS", raising=False)
    assert app._max_agent_turns() == 10000


def test_agent_turn_budget_accepts_large_configured_value(monkeypatch):
    monkeypatch.setenv("FORGE_MAX_AGENT_TURNS", "9000")
    assert app._max_agent_turns() == 9000


def test_agent_turn_budget_caps_unbounded_value(monkeypatch):
    monkeypatch.setenv("FORGE_MAX_AGENT_TURNS", "999999999")
    assert app._max_agent_turns() == 10000


def test_agent_turn_budget_handles_invalid_value(monkeypatch):
    monkeypatch.setenv("FORGE_MAX_AGENT_TURNS", "not-a-number")
    assert app._max_agent_turns() == 10000



def test_vercel_sandbox_command_timeout_honors_longer_requests(monkeypatch):
    import vercel_sandbox
    monkeypatch.setenv("SANDBOX_COMMAND_TIMEOUT", "900")
    assert vercel_sandbox._command_timeout(600) == 600


def test_vercel_sandbox_command_timeout_is_capped(monkeypatch):
    import vercel_sandbox
    monkeypatch.setenv("SANDBOX_COMMAND_TIMEOUT", "99999")
    assert vercel_sandbox._command_timeout(99999) == 900


def test_vercel_sandbox_command_timeout_handles_bad_environment(monkeypatch):
    import vercel_sandbox
    monkeypatch.setenv("SANDBOX_COMMAND_TIMEOUT", "invalid")
    assert vercel_sandbox._command_timeout(600) == 600
