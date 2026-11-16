"""File-tool path arguments: ``~`` expansion and refusal text.

Each native file tool expands a leading ``~`` once at the tool boundary and
runs the scope checks on the expanded path. Before, ``~/x`` joined the
workspace, passed the scope check, and then failed (rg got the literal string)
or wrote into ``<workspace>/~/x``.
"""

import json
import os
import shutil
from dataclasses import dataclass

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.paths import tool_target_path
from harness.pilot import PilotAction
from harness.search_hints import resolve_search_paths


@pytest.fixture
def home_ws(tmp_path, monkeypatch):
    """HOME is tmp_path/home; the workspace is ~/ws; ~/other is outside it."""
    home = tmp_path / "home"
    ws = home / "ws"
    other = home / "other"
    (ws / "notes").mkdir(parents=True)
    other.mkdir()
    (ws / "notes" / "a.txt").write_text("needle in ws\n", encoding="utf-8")
    (other / "b.txt").write_text("needle in other\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cfg = HarnessConfig(repo=str(ws), swarm_adapter="demo", state_dir=str(tmp_path / "state"))
    return ConversationalSession(cfg), ws, other


def test_tool_target_path_expands_tilde(home_ws):
    _session, ws, _other = home_ws
    assert tool_target_path("~/ws/notes/a.txt", "/repo") == str(ws / "notes" / "a.txt")
    assert tool_target_path("notes/a.txt", str(ws)) == os.path.join(str(ws), "notes/a.txt")
    assert tool_target_path("/abs/x", str(ws)) == "/abs/x"


def test_resolve_search_paths_expands_tilde(home_ws):
    _session, ws, _other = home_ws
    assert resolve_search_paths("~/ws/notes", str(ws)) == ([str(ws / "notes")], [])
    assert resolve_search_paths(["~/ws/notes", "~/ws/ghost"], str(ws)) == (
        [str(ws / "notes")], [str(ws / "ghost")],
    )


def test_read_file_expands_tilde(home_ws):
    session, _ws, _other = home_ws
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path="~/ws/notes/a.txt"))
    assert ok, (status, val)
    assert "needle in ws" in val


def test_list_dir_expands_tilde(home_ws):
    session, _ws, _other = home_ws
    ok, status, val = session._do_list_dir(PilotAction(kind="list_dir", path="~/ws/notes"))
    assert ok, (status, val)
    assert "a.txt" in val[1]


@pytest.mark.parametrize("engine", ["ripgrep", "python"])
def test_search_files_expands_tilde(home_ws, monkeypatch, engine):
    session, _ws, _other = home_ws
    if engine == "ripgrep":
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
    else:
        monkeypatch.setattr(shutil, "which", lambda name: None)
    act = PilotAction(kind="search_files", query="needle", arguments={"path": "~/ws/notes"})
    ok, status, val = session._do_search_files(act)
    assert ok, (status, val)
    assert "needle in ws" in val
    assert "No such file" not in val


def test_view_image_expands_tilde(home_ws, monkeypatch):
    import harness.vision as vision
    from harness.vision import VisionResult

    session, ws, _other = home_ws
    (ws / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    seen = []

    def _transcribe(paths, sidecar=None):
        seen.extend(paths)
        return [VisionResult(text="pixels", model="mock-vlm")]

    monkeypatch.setattr(vision, "session_supports_native_images", lambda _s: False)
    monkeypatch.setattr(vision, "transcribe_images", _transcribe)
    ok, status, val = session._do_view_image(PilotAction(kind="view_image", path="~/ws/shot.png"))
    assert ok, (status, val)
    assert seen == [str(ws / "shot.png")]


def test_read_pdf_expands_tilde(home_ws, monkeypatch):
    import harness.web_tools as web_tools

    session, ws, _other = home_ws
    (ws / "doc.pdf").write_bytes(b"%PDF-1.4\n")
    seen = []
    monkeypatch.setattr(web_tools, "read_pdf", lambda target: seen.append(target) or "pdf text")
    ok, status, val = session._do_read_pdf(PilotAction(kind="read_pdf", path="~/ws/doc.pdf"))
    assert ok, (status, val)
    assert seen == [str(ws / "doc.pdf")]


def test_write_file_expands_tilde(home_ws):
    session, ws, _other = home_ws
    act = PilotAction(kind="write_file", path="~/ws/new.txt", content="hello\n")
    ok, status, val = session._do_write_file(act)
    assert ok, (status, val)
    assert (ws / "new.txt").read_text(encoding="utf-8") == "hello\n"
    assert not (ws / "~").exists()


def test_edit_file_expands_tilde(home_ws):
    session, ws, _other = home_ws
    act = PilotAction(
        kind="edit_file", path="~/ws/notes/a.txt", old_str="needle", new_str="pin",
    )
    ok, status, val = session._do_edit_file(act)
    assert ok, (status, val)
    assert (ws / "notes" / "a.txt").read_text(encoding="utf-8") == "pin in ws\n"


def test_hash_edit_expands_tilde(home_ws, monkeypatch):
    from harness.hash_edit import compute_range_hash

    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    session, ws, _other = home_ws
    anchor = compute_range_hash(["needle in ws"], 1, 1)
    act = PilotAction(
        kind="hash_edit",
        path="~/ws/notes/a.txt",
        arguments={"ops": [{"op": "replace", "start_line": 1, "anchor": anchor, "text": "pin in ws"}]},
    )
    ok, status, val = session._do_hash_edit(act)
    assert ok, (status, val)
    assert (ws / "notes" / "a.txt").read_text(encoding="utf-8") == "pin in ws\n"


@pytest.mark.parametrize("mode", ["off", "auto", "required"])
@pytest.mark.parametrize("kind", ["write_file", "edit_file", "hash_edit"])
def test_writes_outside_workspace_stay_refused_after_expansion(home_ws, monkeypatch, kind, mode):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    monkeypatch.setenv("HARNESS_OS_SANDBOX", mode)
    session, ws, other = home_ws
    act = PilotAction(
        kind=kind,
        path="~/other/b.txt",
        content="x",
        old_str="needle",
        new_str="pin",
        arguments={"ops": [{"op": "delete", "start_line": 1, "anchor": "x"}]},
    )
    ok, status, val = getattr(session, f"_do_{kind}")(act)
    assert not ok and status == "path_traversal"
    assert "Outside the file tool workspace" in val
    assert (other / "b.txt").read_text(encoding="utf-8") == "needle in other\n"
    assert not (ws / "~").exists()


def test_lsp_root_expands_tilde(home_ws, monkeypatch):
    import harness.lsp_code_intelligence as lsp

    session, ws, _other = home_ws
    seen = []
    monkeypatch.setattr(lsp, "discover_lsp_tools", lambda root: [])
    monkeypatch.setattr(lsp, "get_lsp_report", lambda **kw: seen.append(kw["root"]) or "report")
    ok, status, val = session._do_lsp(PilotAction(kind="lsp", arguments={"root": "~/ws/notes"}))
    assert ok, (status, val)
    assert seen == [str(ws / "notes")]

    ok, status, val = session._do_lsp(PilotAction(kind="lsp", arguments={"root": "~/other"}))
    assert not ok and status == "path_traversal"
    assert "Outside the workspace: lsp root '~/other'" in val
    assert "traversal" not in val.lower()


SANDBOX_MODES = ["off", "auto", "required"]


@pytest.mark.parametrize("mode", SANDBOX_MODES)
def test_outside_read_works_in_every_sandbox_mode(home_ws, monkeypatch, mode):
    """Read tools match the shell, which no sandbox mode limits for reads."""
    monkeypatch.setenv("HARNESS_OS_SANDBOX", mode)
    session, _ws, other = home_ws
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path=str(other / "b.txt")))
    assert ok and "needle in other" in val, (status, val)
    ok, status, val = session._do_list_dir(PilotAction(kind="list_dir", path="~/other"))
    assert ok and "b.txt" in val[1], (status, val)
    act = PilotAction(kind="search_files", query="needle", arguments={"path": "~/other"})
    ok, status, val = session._do_search_files(act)
    assert ok and "needle in other" in val, (status, val)


@pytest.mark.parametrize("mode", SANDBOX_MODES)
@pytest.mark.parametrize("location,text", [
    (".ssh/id_ed25519", "credential location"),
    (".config/gh/hosts.yml", "credential location"),
    ("project/.env", "credential location"),
    (".pmharness/state/keys.json", "Marionette or Puppetmaster state"),
    (".puppetmaster/auth_pool.json", "Marionette or Puppetmaster state"),
    ("tmp/marionette-browser-shots/browser-1.png", "desktop screenshot store"),
    ("Support/Marionette/native-computer/tmp/snapshot-1.png", "desktop screenshot store"),
])
def test_protected_locations_stay_refused(home_ws, monkeypatch, mode, location, text):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", mode)
    session, ws, _other = home_ws
    target = ws.parent / location
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("secret\n", encoding="utf-8")
    requested = "~/" + location
    acts = [
        PilotAction(kind="read_file", path=requested),
        PilotAction(kind="search_files", query="secret", arguments={"path": requested}),
        PilotAction(kind="view_image", path=requested),
        PilotAction(kind="read_pdf", path=requested),
    ]
    if not location.endswith(".env"):
        # The folder itself is protected, so a listing is refused too.
        acts.append(PilotAction(kind="list_dir", path=os.path.dirname(requested)))
    for act in acts:
        ok, status, val = getattr(session, f"_do_{act.kind}")(act)
        assert not ok and status == "forbidden", (act.kind, status, val)
        assert text in val, act.kind


def test_session_state_dir_stays_refused(home_ws, tmp_path):
    session, _ws, _other = home_ws
    transcript = tmp_path / "state" / "sessions" / "other.json"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}", encoding="utf-8")
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path=str(transcript)))
    assert not ok and status == "forbidden"
    assert "Marionette or Puppetmaster state" in val


def test_privacy_pattern_refuses_an_outside_read(home_ws, tmp_path):
    session, _ws, other = home_ws
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "privacy.json").write_text(
        json.dumps({"forbidden_patterns": ["b.txt"]}), encoding="utf-8",
    )
    for act in (
        PilotAction(kind="read_file", path=str(other / "b.txt")),
        PilotAction(kind="search_files", query="needle", arguments={"path": str(other / "b.txt")}),
    ):
        ok, status, val = getattr(session, f"_do_{act.kind}")(act)
        assert not ok and status == "forbidden", (act.kind, status, val)


def test_missing_path_names_the_home_directory(home_ws):
    session, _ws, _other = home_ws
    home = os.path.expanduser("~")
    guessed = "/Users/no-such-user-zz/Projects/x/notes"
    for act in (
        PilotAction(kind="read_file", path=guessed + "/a.md"),
        PilotAction(kind="list_dir", path=guessed),
        PilotAction(kind="search_files", query="x", arguments={"path": guessed}),
    ):
        ok, status, val = getattr(session, f"_do_{act.kind}")(act)
        assert not ok and status == "not_found", act.kind
        assert val.startswith("Path not found: '/Users/no-such-user-zz/")
        assert "'/Users/no-such-user-zz' does not exist" in val
        assert f"The home directory on this machine is {home!r}" in val


def test_missing_tilde_path_shows_the_expansion(home_ws):
    session, _ws, _other = home_ws
    home = os.path.expanduser("~")
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path="~/nope.md"))
    assert not ok and status == "not_found"
    assert val == f"Path not found: '~/nope.md' (expanded to {os.path.join(home, 'nope.md')!r})."


def test_privacy_paths_stay_refused_after_expansion(home_ws, tmp_path):
    session, ws, _other = home_ws
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "privacy.json").write_text(json.dumps({"forbidden_patterns": ["secret.txt"]}), encoding="utf-8")
    (ws / "secret.txt").write_text("SECRET=1\n", encoding="utf-8")
    ok, status, val = session._do_read_file(PilotAction(kind="read_file", path="~/ws/secret.txt"))
    assert not ok and status == "forbidden", (status, val)
    act = PilotAction(kind="search_files", query="SECRET", arguments={"path": "~/ws/secret.txt"})
    ok, status, val = session._do_search_files(act)
    assert not ok and status == "forbidden", (status, val)


@dataclass
class _FakeResponse:
    text: str
    error: str = ""
    tokens_out: int = 0
    tokens_in: int = 0


def test_send_loop_write_file_expands_tilde(home_ws):
    """The send loop write phase uses the same expansion as the handler."""
    session, ws, _other = home_ws

    class _Pilot:
        def __init__(self):
            self.calls = 0

        def complete(self, prompt, system=None):
            self.calls += 1
            if self.calls == 1:
                return _FakeResponse(text=json.dumps({
                    "say": "Writing",
                    "actions": [{"kind": "write_file", "path": "~/ws/loop.txt", "content": "loop\n"}],
                }))
            return _FakeResponse(text=json.dumps({"say": "Done", "actions": []}))

    session.pilot = _Pilot()
    events = list(session.send("write it"))
    errors = [e.data.get("error") for e in events if e.kind == "action_result" and e.data.get("error")]
    assert not errors, errors
    assert (ws / "loop.txt").read_text(encoding="utf-8") == "loop\n"
    assert not (ws / "~").exists()
