import os
from pathlib import Path

import render_sandbox


def test_configured_requires_render_credentials(monkeypatch):
    monkeypatch.setenv("SANDBOX_RENDER", "1")
    monkeypatch.setenv("ALLOW_SHELL", "1")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.delenv("RENDER_WORKSPACE_ID", raising=False)
    assert not render_sandbox.configured()


def test_stage_excludes_credentials_and_git_metadata(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "config").write_text("secret-ish")
    (source / "project").mkdir()
    (source / "project" / "main.py").write_text("print('ok')")
    (source / ".env").write_text("API_KEY=do-not-copy")
    (source / "id_rsa").write_text("private key")
    target.mkdir()
    count, _ = render_sandbox._stage(source, target)
    assert count == 1
    assert (target / "project" / "main.py").exists()
    assert not (target / ".env").exists()
    assert not (target / ".git" / "config").exists()
    assert not (target / "id_rsa").exists()


def test_sync_back_does_not_follow_symlinks_or_delete_files(tmp_path):
    source = tmp_path / "remote"
    target = tmp_path / "local"
    source.mkdir()
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    (source / "new.txt").write_text("new")
    (source / "link.txt").symlink_to(target / "keep.txt")
    count = render_sandbox._sync_back(source, target)
    assert count == 1
    assert (target / "keep.txt").read_text() == "keep"
    assert (target / "new.txt").read_text() == "new"



def test_stage_excludes_additional_credential_material(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / ".ssh").mkdir(parents=True)
    (source / ".ssh" / "id_ed25519").write_text("private")
    (source / "secrets").mkdir()
    (source / "secrets" / "token.txt").write_text("sensitive")
    (source / "app.py").write_text("print('safe')")
    target.mkdir()
    count, _ = render_sandbox._stage(source, target)
    assert count == 1
    assert (target / "app.py").exists()
    assert not (target / ".ssh").exists()
    assert not (target / "secrets").exists()


def test_sync_back_skips_unchanged_files_and_rejects_symlink_overwrite(tmp_path):
    source = tmp_path / "remote"
    target = tmp_path / "local"
    source.mkdir()
    target.mkdir()
    (source / "same.txt").write_text("same")
    (target / "same.txt").write_text("same")
    (source / "new.txt").write_text("new")
    (target / "link.txt").symlink_to(target / "same.txt")
    (source / "link.txt").write_text("overwrite")
    import pytest
    with pytest.raises(RuntimeError, match="symlink"):
        render_sandbox._sync_back(source, target)
    assert (target / "same.txt").read_text() == "same"
    assert not (target / "new.txt").exists()
