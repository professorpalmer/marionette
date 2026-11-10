from __future__ import annotations

import shutil
from dataclasses import dataclass, field

import pytest

from harness.conversation import ConversationalSession


@dataclass
class _Cfg:
    repo: str = ""
    driver: str = "stub-oracle-v2"
    reach: str = "local"
    state_dir: str = ""
    swarm_adapter: str = ""
    max_context_tokens: int = 96000


@dataclass
class _Act:
    query: str
    arguments: dict = field(default_factory=dict)
    kind: str = "search_files"


@pytest.fixture(params=["rg", "python"])
def session(request, tmp_path, monkeypatch):
    if request.param == "rg" and not shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    if request.param == "python":
        monkeypatch.setattr(shutil, "which", lambda name: None)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("one\nFoo = 1\nthree\nfour\n", encoding="utf-8")
    (tmp_path / "src" / "b.ts").write_text("const foo = 2;\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("call(x\n", encoding="utf-8")
    return ConversationalSession(_Cfg(repo=str(tmp_path), state_dir=str(tmp_path / "state")))


def _search(session, query, **args):
    ok, status, text = session._do_search_files(_Act(query=query, arguments=dict(args, query=query)))
    assert ok, (status, text)
    return text


def test_case_insensitive_and_glob(session):
    both = _search(session, "foo", case_insensitive=True)
    assert "src/a.py" in both and "src/b.ts" in both
    only_py = _search(session, "foo", case_insensitive=True, glob="*.py")
    assert "src/a.py" in only_py and "b.ts" not in only_py
    assert "a.py" not in _search(session, "foo")


def test_files_only(session):
    text = _search(session, "foo", case_insensitive=True, files_only=True)
    lines = sorted(line for line in text.splitlines() if line.strip())
    assert [line.lstrip("./") for line in lines] == ["src/a.py", "src/b.ts"]


def test_invalid_regex_is_searched_as_plain_text(session):
    text = _search(session, "call(x")
    assert "notes.md" in text
    assert "searched as plain text" in text


def test_context_lines_with_ripgrep(session):
    if not shutil.which("rg"):
        pytest.skip("context lines need ripgrep")
    text = _search(session, "Foo", context_lines=1)
    assert "one" in text and "three" in text
