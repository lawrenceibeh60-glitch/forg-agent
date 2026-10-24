import agent_runtime


def test_action_failure_detector_recognizes_execution_errors():
    assert agent_runtime.action_failed("Tool run_command failed: RuntimeError: runner unavailable")
    assert agent_runtime.action_failed("Traceback (most recent call last):\nModuleNotFoundError: no module named x")
    assert agent_runtime.action_failed("Command not found: pytest")


def test_action_failure_detector_does_not_flag_success_or_normal_text():
    assert not agent_runtime.action_failed("3 passed, 0 failed")
    assert not agent_runtime.action_failed("Found 4 references to this function")
