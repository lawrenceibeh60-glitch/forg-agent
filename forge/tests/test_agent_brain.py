import agent_brain


def test_backend_brain_profile_defines_coordinated_specialist_heads():
    prompt = agent_brain.system_instruction()
    for role in ("ROUTER HEAD", "PLANNER HEAD", "ANALYST HEAD", "EXECUTOR HEAD", "VERIFIER HEAD", "CRITIC HEAD"):
        assert role in prompt
    assert "never repeat an identical failing action" in prompt.lower()
