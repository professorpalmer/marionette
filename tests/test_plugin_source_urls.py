"""Plugin source URL resolve + install (git / https / github)."""

from __future__ import annotations

pytest_plugins = ["tests.test_agent_plugins"]

import shutil
from pathlib import Path

import pytest

from harness.agent_plugins import AgentPluginError
from harness.api.plugins import PluginServices, post_plugins_install
from harness.plugin_registry import (
    install_from_path,
    install_from_source,
    plugins_dir,
    resolve_plugin_source,
)
from tests.test_agent_plugins import _valid_package


def test_resolve_absolute_path() -> None:
    src = resolve_plugin_source("/abs/plugin")
    assert src.kind == "path"
    assert src.path == "/abs/plugin"


def test_resolve_github_https_and_shorthand() -> None:
    https = resolve_plugin_source("https://github.com/acme/widget")
    assert https.kind == "github"
    assert https.owner == "acme"
    assert https.repo == "widget"
    assert https.clone_url == "https://github.com/acme/widget.git"

    tagged = resolve_plugin_source("https://github.com/acme/widget.git#v1")
    assert tagged.kind == "github"
    assert tagged.ref == "v1"

    short = resolve_plugin_source("acme/widget@main")
    assert short.kind == "github"
    assert short.ref == "main"
    assert short.clone_url == "https://github.com/acme/widget.git"

    prefixed = resolve_plugin_source("github:acme/widget")
    assert prefixed.kind == "github"
    assert prefixed.clone_url == "https://github.com/acme/widget.git"


def test_resolve_git_and_https() -> None:
    git = resolve_plugin_source("git@github.com:acme/widget.git")
    assert git.kind == "git"
    assert git.clone_url == "git@github.com:acme/widget.git"
    assert git.owner == "acme"
    assert git.repo == "widget"

    git_https = resolve_plugin_source("https://gitlab.example/acme/widget.git")
    assert git_https.kind == "git"
    assert git_https.clone_url == "https://gitlab.example/acme/widget.git"

    https = resolve_plugin_source("https://example.test/plugins/widget")
    assert https.kind == "https"
    assert https.clone_url == "https://example.test/plugins/widget"


def test_resolve_rejects_relative_and_file() -> None:
    with pytest.raises(AgentPluginError, match="plugin source"):
        resolve_plugin_source("")
    with pytest.raises(AgentPluginError, match="absolute path|git URL|https URL|GitHub"):
        resolve_plugin_source("src-plugin")
    with pytest.raises(AgentPluginError, match="unsupported"):
        resolve_plugin_source("file:///tmp/plugin")


def _stub_clone(package: Path):
    def _clone(resolved, dest: Path) -> None:
        shutil.copytree(str(package), str(dest))

    return _clone


@pytest.mark.parametrize(
    "source",
    [
        "git@github.com:acme/widget.git",
        "https://example.test/plugins/widget",
        "https://github.com/acme/widget",
        "acme/widget",
        {"kind": "github", "raw": "https://github.com/acme/widget", "cloneUrl": "https://github.com/acme/widget.git"},
    ],
)
def test_install_accepts_git_https_github(
    plugins_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: object,
) -> None:
    del plugins_home
    package = _valid_package(tmp_path / "src-plugin")
    shutil.rmtree(plugins_dir(), ignore_errors=True)
    monkeypatch.setattr(
        "harness.plugin_registry._clone_resolved_source", _stub_clone(package)
    )
    record = install_from_source(source)
    assert record.name == "portable.test"
    assert record.enabled is False


def test_install_from_path_accepts_git_https_github(
    plugins_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del plugins_home
    package = _valid_package(tmp_path / "src-plugin")
    monkeypatch.setattr(
        "harness.plugin_registry._clone_resolved_source", _stub_clone(package)
    )
    for source in (
        "git@github.com:acme/widget.git",
        "https://example.test/plugins/widget",
        "https://github.com/acme/widget",
        "github:acme/widget",
    ):
        shutil.rmtree(plugins_dir(), ignore_errors=True)
        record = install_from_path(source)
        assert record.name == "portable.test"
        assert record.enabled is False


def test_post_install_accepts_resolved_git_https_github(
    plugins_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del plugins_home
    package = _valid_package(tmp_path / "src-plugin")
    monkeypatch.setattr(
        "harness.plugin_registry._clone_resolved_source", _stub_clone(package)
    )
    svc = PluginServices()
    for body in (
        {"source": "git@github.com:acme/widget.git"},
        {"source": "https://example.test/plugins/widget"},
        {
            "source": {
                "kind": "github",
                "raw": "https://github.com/acme/widget",
                "cloneUrl": "https://github.com/acme/widget.git",
            }
        },
    ):
        shutil.rmtree(plugins_dir(), ignore_errors=True)
        status, payload = post_plugins_install(body, svc)
        assert status == 200, payload
        assert payload["ok"] is True
        assert payload["plugin"]["name"] == "portable.test"


def test_post_install_still_accepts_path(
    plugins_home: Path, tmp_path: Path
) -> None:
    del plugins_home
    package = _valid_package(tmp_path / "src-plugin")
    status, payload = post_plugins_install({"path": str(package)}, PluginServices())
    assert status == 200, payload
    assert payload["ok"] is True
