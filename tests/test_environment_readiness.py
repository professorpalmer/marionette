"""Browser + analyzer readiness contract (optional prerequisites)."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.environment import EnvironmentServices, get_environment_readiness
from harness.environment_readiness import (
    browser_remedy,
    build_environment_readiness,
    clear_environment_readiness_cache,
    python_analyzer_remedy,
    typescript_analyzer_remedy,
)
from harness.lsp_code_intelligence import LspToolAvailability


def setup_function():
    clear_environment_readiness_cache()


def test_browser_remedy_platform_aware(monkeypatch):
    monkeypatch.setattr("harness.environment_readiness.sys.platform", "darwin")
    text = browser_remedy(available=False)
    assert "Chrome" in text
    assert "PM_BROWSER_CHROME" in text
    assert "Electron" in text

    monkeypatch.setattr("harness.environment_readiness.sys.platform", "linux")
    text = browser_remedy(available=False)
    assert "chromium" in text.lower() or "Chrome" in text
    assert "PM_BROWSER_CHROME" in text

    monkeypatch.setattr("harness.environment_readiness.sys.platform", "win32")
    text = browser_remedy(available=False)
    assert "chrome.exe" in text or "Chrome" in text
    assert "PM_BROWSER_CHROME" in text

    configured = browser_remedy(available=False, configured="/bad/Electron")
    assert "PM_BROWSER_CHROME" in configured
    assert "Electron" in configured or "standalone" in configured.lower()


def test_analyzer_remedies_mention_workspace_locations():
    py = python_analyzer_remedy(available=False)
    assert "pyright" in py
    assert ".venv" in py or "PATH" in py
    assert "auto-install" in py.lower() or "does not auto-install" in py.lower()

    ts = typescript_analyzer_remedy(available=False)
    assert "tsc" in ts or "typescript" in ts.lower()
    assert "node_modules" in ts


def test_build_readiness_uses_probes(monkeypatch, tmp_path):
    monkeypatch.setenv("PM_BROWSER_CHROME", "")
    monkeypatch.setattr(
        "harness.browser.standalone_chrome_path",
        lambda refresh=False: None,
    )
    monkeypatch.setattr(
        "harness.lsp_code_intelligence.discover_lsp_tools",
        lambda root=None, which_fn=None: LspToolAvailability(
            python_pyright=None,
            python_pyright_langserver=None,
            typescript_tsc=None,
            typescript_tsserver=None,
            typescript_typescript_language_server=None,
        ),
    )
    payload = build_environment_readiness(root=str(tmp_path), refresh=True)
    assert payload["browser"]["available"] is False
    assert payload["browser"]["remedy"]
    assert payload["python_analyzer"]["available"] is False
    assert "pyright" in payload["python_analyzer"]["remedy"]
    assert payload["typescript_analyzer"]["available"] is False
    assert payload["workspace_root"] == str(tmp_path)

    monkeypatch.setattr(
        "harness.browser.standalone_chrome_path",
        lambda refresh=False: "/usr/bin/google-chrome",
    )
    monkeypatch.setattr(
        "harness.lsp_code_intelligence.discover_lsp_tools",
        lambda root=None, which_fn=None: LspToolAvailability(
            python_pyright=str(tmp_path / "pyright"),
            python_pyright_langserver=None,
            typescript_tsc=str(tmp_path / "tsc"),
            typescript_tsserver=None,
            typescript_typescript_language_server=None,
        ),
    )
    payload = build_environment_readiness(root=str(tmp_path), refresh=True)
    assert payload["browser"]["available"] is True
    assert payload["browser"]["path"] == "/usr/bin/google-chrome"
    assert payload["browser"]["remedy"] == ""
    assert payload["python_analyzer"]["available"] is True
    assert payload["typescript_analyzer"]["available"] is True


def test_embedded_electron_not_treated_as_available(monkeypatch, tmp_path):
    """Only standalone Chrome counts — Electron embeds stay unavailable."""
    import harness.browser as browser

    monkeypatch.setenv(
        "PM_BROWSER_CHROME",
        "/Applications/Marionette.app/Contents/MacOS/Marionette",
    )
    browser._ENGINE_ERR = ""
    browser._engine = SimpleNamespace(__name__="puppetmaster.browser_cdp")
    browser._chrome_probe_cache.clear()
    payload = build_environment_readiness(root=str(tmp_path), refresh=True)
    assert payload["browser"]["available"] is False
    assert "Electron" in payload["browser"]["remedy"] or "Marionette" in payload["browser"]["remedy"]


def test_get_environment_readiness_api(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "harness.environment_readiness.build_environment_readiness",
        lambda root="", refresh=False: {
            "browser": {"available": False, "path": None, "remedy": "install Chrome"},
            "python_analyzer": {"available": True, "path": "/x/pyright", "remedy": ""},
            "typescript_analyzer": {"available": False, "path": None, "remedy": "install tsc"},
            "workspace_root": root,
        },
    )
    svc = EnvironmentServices(get_repo=lambda: str(tmp_path))
    code, body = get_environment_readiness("1", svc)
    assert code == 200
    assert body["workspace_root"] == str(tmp_path)
    assert body["browser"]["available"] is False
    assert body["python_analyzer"]["available"] is True


def test_readiness_route_registered():
    import harness.http_routes as http_routes
    import harness.server as srv

    srv._GET_ROUTES = None
    get = srv._get_routes()
    assert "/api/environment/readiness" in get
    assert callable(get["/api/environment/readiness"])
    # Direct builder stays aligned.
    assert "/api/environment/readiness" in http_routes.build_get_routes(
        srv._route_services()
    )


def test_readiness_cache_reuses_probe_until_explicit_refresh(monkeypatch, tmp_path):
    calls = {"n": 0}

    def fake_discover(*, root=None, which_fn=None):
        calls["n"] += 1
        return LspToolAvailability(
            python_pyright=None,
            python_pyright_langserver=None,
            typescript_tsc=None,
            typescript_tsserver=None,
            typescript_typescript_language_server=None,
        )

    monkeypatch.setattr(
        "harness.browser.standalone_chrome_path",
        lambda refresh=False: None,
    )
    monkeypatch.setattr(
        "harness.lsp_code_intelligence.discover_lsp_tools",
        fake_discover,
    )
    root = str(tmp_path)
    a = build_environment_readiness(root=root, refresh=False)
    b = build_environment_readiness(root=root, refresh=False)
    assert calls["n"] == 1
    assert a["workspace_root"] == b["workspace_root"]

    build_environment_readiness(root=root, refresh=True)
    assert calls["n"] == 2


def test_readiness_cache_invalidates_when_pm_browser_chrome_changes(
    monkeypatch, tmp_path,
):
    """Changing PM_BROWSER_CHROME must not serve a stale 30s readiness hit."""
    import harness.browser as browser

    chrome = tmp_path / "google-chrome"
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    chrome.chmod(0o755)
    electron = tmp_path / "Marionette.app" / "Contents" / "MacOS" / "Marionette"
    electron.parent.mkdir(parents=True)
    electron.write_text("#!/bin/sh\n", encoding="utf-8")
    electron.chmod(0o755)

    probes = {"n": 0}

    def counting_discover(*, root=None, which_fn=None):
        probes["n"] += 1
        return LspToolAvailability(
            python_pyright=None,
            python_pyright_langserver=None,
            typescript_tsc=None,
            typescript_tsserver=None,
            typescript_typescript_language_server=None,
        )

    monkeypatch.setattr(
        "harness.lsp_code_intelligence.discover_lsp_tools",
        counting_discover,
    )
    browser._ENGINE_ERR = ""
    browser._engine = SimpleNamespace(__name__="puppetmaster.browser_cdp")
    browser._chrome_probe_cache.clear()

    root = str(tmp_path)
    monkeypatch.setenv("PM_BROWSER_CHROME", str(chrome))
    first = build_environment_readiness(root=root, refresh=False)
    assert first["browser"]["available"] is True
    assert first["browser"]["path"] == str(chrome)
    assert probes["n"] == 1

    # Same env + workspace: reuse readiness cache (no second LSP discover).
    again = build_environment_readiness(root=root, refresh=False)
    assert again["browser"]["available"] is True
    assert probes["n"] == 1

    # Point at embedded Electron — must re-probe and reject, not reuse chrome hit.
    browser._chrome_probe_cache.clear()
    monkeypatch.setenv("PM_BROWSER_CHROME", str(electron))
    rejected = build_environment_readiness(root=root, refresh=False)
    assert probes["n"] == 2
    assert rejected["browser"]["available"] is False
    assert "Electron" in rejected["browser"]["remedy"] or "Marionette" in rejected[
        "browser"
    ]["remedy"]
    assert rejected["browser"]["path"] is None

    # Workspace key still partitions the cache independently of chrome env.
    other = tmp_path / "other-ws"
    other.mkdir()
    monkeypatch.setenv("PM_BROWSER_CHROME", str(chrome))
    browser._chrome_probe_cache.clear()
    other_payload = build_environment_readiness(root=str(other), refresh=False)
    assert probes["n"] == 3
    assert other_payload["workspace_root"] == str(other)
    assert other_payload["browser"]["available"] is True


def test_standalone_chrome_path_reuses_cached_probe(monkeypatch, tmp_path):
    import harness.browser as browser

    chrome = tmp_path / "google-chrome"
    chrome.write_text("#!/bin/sh\n", encoding="utf-8")
    chrome.chmod(0o755)
    finds = {"n": 0}

    def counting_find():
        finds["n"] += 1
        return str(chrome)

    monkeypatch.delenv("PM_BROWSER_CHROME", raising=False)
    monkeypatch.setattr(browser, "_find_standalone_chrome", counting_find)
    browser._chrome_probe_cache.clear()
    browser._ENGINE_ERR = ""
    browser._engine = SimpleNamespace(__name__="puppetmaster.browser_cdp")

    assert browser.standalone_browser_available(refresh=True) is True
    assert finds["n"] == 1
    assert browser.standalone_chrome_path() == str(chrome)
    assert browser.standalone_chrome_path() == str(chrome)
    # Path must not re-run _find_standalone_chrome after the shared cache hit.
    assert finds["n"] == 1
