"""HTTP workspace git endpoints for SourceControl read-only fallback."""
import json
import os
import subprocess
import threading
import urllib.parse
import urllib.request

import harness.server as srv


def _server(repo_path: str):
    srv._cfg.repo = repo_path
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _get(port, path, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"X-Harness-Token": token},
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=15)


def _git_env():
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }


def test_workspace_git_status_and_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tracked.txt").write_text("hello\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
        env=_git_env(),
    )
    (repo / "tracked.txt").write_text("hello world\n", encoding="utf-8")

    httpd, port = _server(str(repo))
    try:
        token = srv._TOKEN
        repo_q = urllib.parse.quote(".")
        status = json.loads(
            _get(port, f"/api/git/status?repo={repo_q}", token).read().decode()
        )
        assert status["ok"] is True
        assert any(f["path"] == "tracked.txt" for f in status["files"])

        branches = json.loads(
            _get(port, f"/api/git/branches?repo={repo_q}", token).read().decode()
        )
        assert branches["ok"] is True
        assert len(branches["branches"]) >= 1

        diff = json.loads(
            _get(
                port,
                f"/api/git/diff?repo={repo_q}&file=tracked.txt",
                token,
            ).read().decode()
        )
        assert diff["ok"] is True
        assert "hello" in diff["out"]
    finally:
        httpd.shutdown()


def test_provision_git_status_without_repo_param(tmp_path):
    httpd, port = _server(str(tmp_path))
    try:
        token = srv._TOKEN
        body = json.loads(_get(port, "/api/git/status", token).read().decode())
        assert "gh_available" in body
        assert "connected" in body
    finally:
        httpd.shutdown()
