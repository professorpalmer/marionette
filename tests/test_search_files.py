import os
import tempfile
import shutil
import pytest
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction


def test_search_files_pure_python(monkeypatch):
    # Force pure-Python fallback by patching shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Create some files with known content
        os.makedirs(os.path.join(tmp_dir, "subdir"))
        os.makedirs(os.path.join(tmp_dir, ".git"))
        os.makedirs(os.path.join(tmp_dir, "node_modules"))

        # Normal text files
        with open(os.path.join(tmp_dir, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello world\nline two: some random text\n")
        with open(os.path.join(tmp_dir, "subdir", "b.py"), "w", encoding="utf-8") as f:
            f.write("def my_func():\n    print('hello python')\n")
        
        # Files in ignored directories (should be skipped)
        with open(os.path.join(tmp_dir, ".git", "index.txt"), "w", encoding="utf-8") as f:
            f.write("hello git\n")
        with open(os.path.join(tmp_dir, "node_modules", "package.json"), "w", encoding="utf-8") as f:
            f.write("hello npm\n")

        # Binary file (should be skipped)
        with open(os.path.join(tmp_dir, "binary.bin"), "wb") as f:
            f.write(b"hello \x00 world binary data")

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = tmp_dir
        s = ConversationalSession(cfg)

        # 1. Plain text search
        act = PilotAction(kind="search_files", query="hello", arguments={})
        ok, status, val = s._do_search_files(act)
        assert ok
        assert status == "success"
        lines = val.splitlines()
        # Should find "hello world" in a.txt and "hello python" in subdir/b.py,
        # but NOT in .git, node_modules, or the binary file.
        assert any("a.txt:1: hello world" in l for l in lines)
        assert any("subdir/b.py:2:     print('hello python')" in l for l in lines)
        assert not any(".git" in l for l in lines)
        assert not any("node_modules" in l for l in lines)
        assert not any("binary.bin" in l for l in lines)

        # 2. Regex search
        act = PilotAction(kind="search_files", query="some.*text", arguments={})
        ok, status, val = s._do_search_files(act)
        assert ok
        assert "a.txt:2: line two: some random text" in val

        # 3. Respect max_results
        act = PilotAction(kind="search_files", query="hello", arguments={"max_results": 1})
        ok, status, val = s._do_search_files(act)
        assert ok
        lines = [l for l in val.splitlines() if l.strip() and not l.startswith("...")]
        assert len(lines) == 1
        assert "truncated" in val

        # 4. Scoped subpath search
        act = PilotAction(kind="search_files", query="hello", arguments={"path": "subdir"})
        ok, status, val = s._do_search_files(act)
        assert ok
        assert "subdir/b.py" in val
        assert "a.txt" not in val

        # 5. Path traversal block
        act = PilotAction(kind="search_files", query="hello", arguments={"path": "../escaped"})
        ok, status, val = s._do_search_files(act)
        assert not ok
        assert status == "path_traversal"


def test_search_files_ripgrep(monkeypatch):
    # Verify ripgrep execution path if rg is installed
    rg_path = shutil.which("rg")
    if not rg_path:
        pytest.skip("ripgrep (rg) not available on system, skipping rg-specific test")

    with tempfile.TemporaryDirectory() as tmp_dir:
        os.makedirs(os.path.join(tmp_dir, "subdir"))
        with open(os.path.join(tmp_dir, "a.txt"), "w", encoding="utf-8") as f:
            f.write("hello world\nsome text here\n")
        with open(os.path.join(tmp_dir, "subdir", "b.py"), "w", encoding="utf-8") as f:
            f.write("def func():\n    print('hello python')\n")

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = tmp_dir
        s = ConversationalSession(cfg)

        # 1. Plain text search
        act = PilotAction(kind="search_files", query="hello", arguments={})
        ok, status, val = s._do_search_files(act)
        assert ok
        assert status == "success"
        lines = val.splitlines()
        assert any("a.txt:1:hello world" in l.replace(" ", "") for l in lines)
        assert any("subdir/b.py:2:print('hellopython')" in l.replace(" ", "") for l in lines)

        # 2. Respect max_results
        act = PilotAction(kind="search_files", query="hello", arguments={"max_results": 1})
        ok, status, val = s._do_search_files(act)
        assert ok
        lines = [l for l in val.splitlines() if l.strip() and not l.startswith("...")]
        assert len(lines) == 1
        assert "truncated" in val

        # 3. Scoped subpath
        act = PilotAction(kind="search_files", query="hello", arguments={"path": "subdir"})
        ok, status, val = s._do_search_files(act)
        assert ok
        assert "subdir/b.py" in val
        assert "a.txt" not in val
