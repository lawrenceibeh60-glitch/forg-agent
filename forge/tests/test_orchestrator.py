import orchestrator


def test_seed_plan_is_task_aware_and_actionable():
    engineering = orchestrator.seed_plan("Fix the repository and run tests")
    assert [step["id"] for step in engineering] == ["inspect", "plan", "implement", "verify", "review"]
    assert "pending" == engineering[0]["status"]
    general = orchestrator.seed_plan("Explain the difference between voltage and current")
    assert len(general) == 3


def test_normalize_plan_preserves_completed_step_evidence():
    old = [{"id": "inspect", "label": "Inspect files", "status": "completed", "evidence": "tree read"}]
    plan = orchestrator.normalize_plan("- [ ] Inspect files\n- [x] Run tests", old)
    assert plan[0]["status"] == "completed"
    assert plan[0]["evidence"] == "tree read"
    assert plan[1]["status"] == "completed"


def test_record_step_keeps_only_one_active_step():
    plan = orchestrator.seed_plan("Build app")
    plan = orchestrator.record_step(plan, "Inspect the relevant files, project instructions, and current state", "in_progress")
    plan = orchestrator.record_step(plan, "Choose the smallest complete implementation and identify risks", "in_progress")
    assert sum(step["status"] == "in_progress" for step in plan) == 1
    assert plan[1]["status"] == "in_progress"


def test_tool_risk_is_advisory_and_distinguishes_mutation():
    assert orchestrator.tool_risk("github_merge_pr") == "external_mutation"
    assert orchestrator.tool_risk("read_file") == "inspection_or_verification"
    assert orchestrator.tool_risk("write_file") == "workspace_mutation_or_execution"


def test_checkpoint_does_not_store_secret_named_fields():
    value = orchestrator.checkpoint("tool_result", last_tool="read_file", api_key="never", note="ok")
    assert "api_key" not in value
    assert value["note"] == "ok"
