"""Focused tests for portable repository-sync defaults."""

from rdn.handoff import sync


def test_default_repo_root_uses_current_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("RDN_REPO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    assert sync._default_repo_root() == str(tmp_path)


def test_default_repo_root_honors_environment_override(tmp_path, monkeypatch):
    configured_root = tmp_path / "repositories"
    monkeypatch.setenv("RDN_REPO_ROOT", str(configured_root))

    assert sync._default_repo_root() == str(configured_root)
