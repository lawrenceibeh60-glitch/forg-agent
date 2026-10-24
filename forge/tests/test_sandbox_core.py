"""Tests for provider selection and fail-closed sandbox execution."""
import os
from unittest.mock import patch

import sandbox_core


def clear_provider_flags(monkeypatch):
    for flag in sandbox_core.PROVIDER_FLAGS.values():
        monkeypatch.delenv(flag, raising=False)


def test_no_provider_fails_closed_even_if_legacy_host_flag_is_enabled(monkeypatch, tmp_path):
    clear_provider_flags(monkeypatch)
    monkeypatch.setenv("ALLOW_SHELL", "1")
    monkeypatch.setenv("SANDBOX_ALLOW_HOST_EXECUTION", "1")
    result = sandbox_core.execute("echo unsafe", tmp_path, tmp_path)
    assert "no isolated command runner" in result
    assert "host execution is disabled" in result


def test_multiple_providers_are_rejected(monkeypatch):
    monkeypatch.setenv("SANDBOX_VERCEL", "1")
    monkeypatch.setenv("SANDBOX_RENDER", "1")
    with __import__("pytest").raises(ValueError, match="Multiple sandbox providers"):
        sandbox_core.selected_provider()


def test_shell_policy_is_checked_before_provider(monkeypatch, tmp_path):
    clear_provider_flags(monkeypatch)
    monkeypatch.setenv("SANDBOX_DOCKER", "1")
    monkeypatch.setenv("ALLOW_SHELL", "0")
    with patch("sandbox_core.subprocess.run") as run:
        result = sandbox_core.execute("echo unsafe", tmp_path, tmp_path)
    assert "disabled by policy" in result
    run.assert_not_called()


def test_docker_exec_runs_only_in_hardened_container(monkeypatch, tmp_path):
    clear_provider_flags(monkeypatch)
    monkeypatch.setenv("SANDBOX_DOCKER", "1")
    monkeypatch.setenv("ALLOW_SHELL", "1")
    monkeypatch.setenv("SANDBOX_NETWORK", "0")
    fake = type("Completed", (), {"stdout": "ok", "stderr": "", "returncode": 0})()
    with patch("sandbox_core.shutil.which", return_value="/usr/bin/docker"), patch(
        "sandbox_core.subprocess.run", return_value=fake
    ) as run:
        result = sandbox_core.execute("echo ok", tmp_path, tmp_path, 7)
    argv = run.call_args.args[0]
    assert argv[0:4] == ["docker", "run", "--rm", "--network"]
    assert "none" in argv
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv
    assert run.call_args.kwargs["timeout"] == 7
    assert result == "ok\n[exit 0]"


def test_working_directory_escape_is_rejected(tmp_path):
    outside = tmp_path.parent / "outside"
    with __import__("pytest").raises(ValueError, match="escapes"):
        sandbox_core.execute("echo nope", tmp_path, outside)


def test_status_reports_missing_provider_without_secret_values(monkeypatch):
    clear_provider_flags(monkeypatch)
    monkeypatch.setenv("ALLOW_SHELL", "1")
    monkeypatch.setenv("RENDER_API_KEY", "do-not-report-this")
    result = sandbox_core.status()
    assert not result["ready"]
    assert result["provider"] is None
    assert "do-not-report-this" not in str(result)
