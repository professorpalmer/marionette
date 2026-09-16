"""Browser bridge routing must never silently substitute a fresh Chrome page."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness import browser, desktop_browser
from harness.api.browser import post_browser_controller


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setattr(desktop_browser, "_endpoint", None)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.headers["Authorization"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(400 if requests[-1][1]["session_id"] == "wrong" else 200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": requests[-1][1]["session_id"] != "wrong", "result": "visible page", "error": "wrong session"}).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    desktop_browser.configure(server.server_port, "a" * 64)
    yield requests
    server.shutdown()
    server.server_close()
    thread.join()


def test_desktop_wrapper_routes_session_and_screenshot_without_engine(bridge, monkeypatch):
    monkeypatch.setattr(browser, "_engine", None)
    assert browser.browser_snapshot(session_id="session") == "visible page"
    assert browser.browser_screenshot(session_id="session") == "visible page"
    assert [r[1]["action"] for r in bridge] == ["snapshot", "screenshot"]
    assert all(r[0] == "Bearer " + "a" * 64 and r[1]["session_id"] == "session" for r in bridge)
    assert "wrong session" in browser.browser_snapshot(session_id="wrong")
    assert "active conversation" in browser.browser_snapshot()


def test_desktop_navigation_preserves_url_safety(bridge):
    assert "blocked" in browser.browser_navigate("file:///etc/passwd", session_id="session")
    assert "blocked" in browser.browser_navigate("http://169.254.169.254/latest/meta-data", session_id="session")
    assert not bridge


def test_controller_registration_is_validated_and_does_not_echo_token(monkeypatch):
    monkeypatch.setattr(desktop_browser, "_endpoint", None)
    for body in ({}, {"port": True, "token": "a" * 64}, {"port": 80, "token": "bad"}, []):
        assert post_browser_controller(body)[0] == 400
    assert not desktop_browser.configured()
    assert post_browser_controller({"port": 9999, "token": "a" * 64}) == (200, {"ok": True})
    assert desktop_browser.available()


def test_dead_bridge_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(desktop_browser, "_endpoint", (1, "a" * 64))
    monkeypatch.setattr(browser, "_guard", lambda: pytest.fail("standalone fallback"))
    assert "bridge failed" in browser.browser_snapshot(session_id="session")


def test_catalog_only_advertises_tabs_for_attached_desktop(monkeypatch):
    from harness.tool_discovery import ToolCatalog
    monkeypatch.setattr(desktop_browser, "_endpoint", None)
    monkeypatch.setattr(browser, "standalone_browser_available", lambda **_kwargs: True)
    catalog = ToolCatalog()
    catalog.refresh()
    assert catalog.activate(["browser_navigate"]) == ["builtin:browser_navigate"]
    assert catalog.activate(["browser_tabs", "browser_tab_activate"]) == []
    desktop_browser.configure(9999, "a" * 64)
    catalog.refresh()
    assert catalog.activate(["browser_tabs"]) == ["builtin:browser_tabs"]


def test_computer_tool_requires_attached_desktop_and_routes_session(monkeypatch, bridge):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from harness.pilot import PilotAction
    from harness.tool_discovery import ToolCatalog
    from harness.send_loop_phases import dispatch_local_action, PLAN_SKIP_KINDS
    catalog = ToolCatalog()
    catalog.refresh()
    assert catalog.activate(["computer_use"]) == ["builtin:computer_use"]
    session = SimpleNamespace(harness_session_id="owned", _append_action_result=MagicMock())
    args = {"operation": "snapshot", "app_id": "fixture.app"}
    list(dispatch_local_action(session, PilotAction(kind="computer_use", arguments=args), "call", False, [], plan=False))
    assert bridge[-1][1] == {"session_id": "owned", "action": "computer", "arguments": args}
    assert "computer_use" in PLAN_SKIP_KINDS
    monkeypatch.setattr(desktop_browser, "_endpoint", None)
    catalog.refresh()
    assert catalog.activate(["computer_use"]) == []


def test_computer_observations_are_fresh_and_inputs_never_replay():
    from harness.pilot import PilotAction
    from harness.pilot_guards import check_loop_guard, new_turn_guard_state, record_action_execution, record_successful_result
    state = new_turn_guard_state()
    observation = PilotAction(kind="computer_use", arguments={"operation": "snapshot", "app_id": "fixture"})
    for _ in range(5):
        record_action_execution(state, observation.kind, observation)
        record_successful_result(state, observation.kind, observation, "old snapshot")
        assert not check_loop_guard(state, observation.kind, observation).suppress
    action = PilotAction(kind="computer_use", arguments={"operation": "click", "snapshot_id": "old", "ref": "r1"})
    record_action_execution(state, action.kind, action)
    record_successful_result(state, action.kind, action, "old click")
    assert not check_loop_guard(state, action.kind, action).replay


def test_dispatch_carries_owned_session_and_respects_plan_mode(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from harness.pilot import PilotAction
    from harness.send_loop_phases import dispatch_local_action, PLAN_SKIP_KINDS
    session = SimpleNamespace(harness_session_id="owned", _append_action_result=MagicMock())
    for name, args in (("browser_snapshot", {}), ("browser_screenshot", {}),
                       ("browser_tabs", {}), ("browser_tab_activate", {"tab_id": "two"})):
        operation = MagicMock(return_value="ok")
        monkeypatch.setattr(browser, name, operation)
        list(dispatch_local_action(session, PilotAction(kind=name, arguments=args), "call", False, [], plan=False))
        assert operation.call_args.kwargs["session_id"] == "owned"
        assert name in PLAN_SKIP_KINDS
